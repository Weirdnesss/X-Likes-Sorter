"""
char_sorter.py  -  sort images into character folders using a WD14 tagger.

Everyday command (PC Direct Sync flow):
    python char_sorter.py inbox            # tag + sort files sitting in INBOX_DIR
    python char_sorter.py inbox --dry-run  # preview only

Setup / maintenance commands:
    python char_sorter.py bench            # test GPU/CPU speed
    python char_sorter.py scan             # run the model once, cache scores
    python char_sorter.py sort --dry-run   # preview folders/counts, moves nothing
    python char_sorter.py sort             # actually move files (logged)
    python char_sorter.py undo             # put everything back using the log

Hashtag tools:
    python char_sorter.py rebuild-tags                 # rebuild data/tweet_hashtags.csv from Meta/ + Inbox/ sidecars
    python char_sorter.py rebuild-tags <folder|.csv>   # ...or from any folder of .json sidecars / an old tweet_hashtags.csv
    python char_sorter.py tags                         # top hashtags NOT yet in config/hashtag_map.csv
    python char_sorter.py tags --all --top 100         # include already-mapped ones
    python char_sorter.py report                       # after `sort --dry-run`: top hashtags/artists in _Unsorted
    python char_sorter.py suggest-map                  # learn hashtag -> character rules from your own tagged images
    python char_sorter.py suggest-map --apply          # ...and append them to config/hashtag_map.csv
"""
import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
from PIL import Image

# ==============================================================================
# CONFIGURATION & DYNAMIC PATH SETUP
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)  # all relative paths (config/, data/, model/) resolve next to this script
CONFIG_FILE = os.path.join("config", "config.json")

def load_config():
    default_config = {
        # --- account / paths ---
        "twitter_username": "",
        "parent_dir": "X_Likes",     # relative paths are resolved next to this script
        # --- downloading (read by run_update.bat) ---
        "fetch_likes": True,
        "fetch_bookmarks": True,
        "gallery_dl_sleep": 0,       # seconds to wait between downloads; 1 helps with dropped connections
        "gallery_dl_retries": 4,
        "gallery_dl_abort": 10,      # stop a feed after this many already-downloaded files in a row (0 = never stop early)
        "gallery_dl_extra_args": "",  # anything else to pass to gallery-dl, e.g. "--sleep-request 1"
        "cookies_file": "cookies.txt",
        # --- tagger / hardware ---
        "dml_device_id": None,       # None = auto (tries GPU 1 then 0); set a number to force one
        "batch_size": 4,             # images per tagger pass; lower it on low-VRAM GPUs (run `bench`)
        "io_threads": 4,             # threads that load/resize images while the GPU works
        "cache_min_score": 0.10,     # guesses below this are not cached (lowering needs a rescan)
        "image_extensions": [".jpg", ".jpeg", ".png", ".webp"],
        "model_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/selected_tags.csv",
        # --- sorting thresholds ---
        "confidence_threshold": 0.35,
        "review_threshold": 0.20,
        "primary_margin": 0.25,
        "min_folder_size": 3,
        # --- hints for images the tagger could not place ---
        "use_sibling_hints": True,   # other images from the same tweet
        "use_author_hints": True,    # route by what the artist usually draws
        "author_min_tweets": 3,      # artist needs at least this many tagger-confirmed tweets
        "author_min_share": 0.8,     # ...and this share of them must be the same character
        "artist_folder_min": 8,      # leftovers go to <folder_artists>/<handle> if the artist has this many (0 = off)
        # --- folder names inside Sorted/ (renaming on an existing library moves files on the next sort) ---
        "folder_unsorted": "_Unsorted",
        "folder_multiple": "_Multiple",
        "folder_review": "_Review",
        "folder_misc": "_Misc Characters",
        "folder_artists": "_Artists",
        # --- output ---
        "summary_top_folders": 25
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                user_cfg = json.load(f)
            unknown = sorted(set(user_cfg) - set(default_config))
            if unknown:
                print(f"[!] Warning: unknown config keys ignored (typo?): {', '.join(unknown)}")
            default_config.update(user_cfg)
        except Exception as e:
            print(f"[!] Warning: Failed to read {CONFIG_FILE} ({e}). Using default settings.")
    return default_config

cfg = load_config()

# Internal directory structure
DATA_DIR = "data"
MODEL_DIR = "model"
CONFIG_DIR = "config"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# Main output directory paths
PARENT_DIR = os.path.abspath(os.path.expanduser(cfg["parent_dir"]))
DEST_DIR = os.path.join(PARENT_DIR, "Sorted")
PC_PULL_DIR = os.path.join(PARENT_DIR, "Incoming")
INBOX_DIR = os.path.join(PC_PULL_DIR, "Inbox")
META_DIR = os.path.join(PARENT_DIR, "Meta")

# Model and tag paths
MODEL_URL = cfg["model_url"]
TAGS_URL = cfg["tags_url"]
MODEL_FILE = os.path.join(MODEL_DIR, "wd14_model.onnx")
TAGS_FILE = os.path.join(MODEL_DIR, "selected_tags.csv")

# Data files
DB_FILE = os.path.join(DATA_DIR, "scores_cache.db")
LOG_FILE = os.path.join(DATA_DIR, "sort_log.csv")
TWEET_TAGS_FILE = os.path.join(DATA_DIR, "tweet_hashtags.csv")
HASHTAG_MAP_FILE = os.path.join(CONFIG_DIR, "hashtag_map.csv")

BATCH_SIZE = max(1, int(cfg.get("batch_size", 4)))
IO_THREADS = max(1, int(cfg["io_threads"]))
STORE_MIN_PROB = float(cfg["cache_min_score"])

# Sorting threshold parameters from config
CONFIDENT = float(cfg.get("confidence_threshold", 0.35))
REVIEW = float(cfg.get("review_threshold", 0.20))
PRIMARY_MARGIN = float(cfg.get("primary_margin", 0.25))
MIN_FOLDER_SIZE = int(cfg.get("min_folder_size", 3))
USE_SIBLINGS = bool(cfg["use_sibling_hints"])
DML_DEVICE = cfg.get("dml_device_id")
USE_AUTHOR_HINTS = bool(cfg.get("use_author_hints", True))
AUTHOR_MIN = int(cfg.get("author_min_tweets", 3))
AUTHOR_SHARE = float(cfg.get("author_min_share", 0.8))
ARTIST_MIN = int(cfg.get("artist_folder_min", 8))

VALID_EXT = tuple(("." + e.lstrip(".")).lower() for e in cfg["image_extensions"])
FOLDER_UNSORTED = str(cfg["folder_unsorted"])
FOLDER_MULTIPLE = str(cfg["folder_multiple"])
FOLDER_REVIEW = str(cfg["folder_review"])
FOLDER_MISC = str(cfg["folder_misc"])
FOLDER_ARTISTS = str(cfg["folder_artists"])
SUMMARY_TOP = max(1, int(cfg["summary_top_folders"]))
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ==============================================================================
# MODEL / TAG FILES & AUTO GPU DETECTION
# ==============================================================================
def download(url, dest, label):
    """Download url -> dest via a .part temp file, so an interrupted download never leaves a broken file."""
    part = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            got, next_report = 0, 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    got += len(chunk)
                    if got >= next_report:
                        if total:
                            print(f"  {label}: {got / 1e6:.0f} / {total / 1e6:.0f} MB ({got * 100 // total}%)")
                        else:
                            print(f"  {label}: {got / 1e6:.0f} MB")
                        next_report = got + (25 << 20)
        if total and got != total:
            raise IOError(f"incomplete download ({got} of {total} bytes)")
        os.replace(part, dest)
        print(f"  {label}: done.")
    except Exception:
        if os.path.exists(part):
            os.remove(part)
        raise


def ensure_files():
    if not os.path.exists(MODEL_FILE):
        print("Downloading WD14 model (a few hundred MB, one-time)...")
        download(MODEL_URL, MODEL_FILE, "model")
    if not os.path.exists(TAGS_FILE):
        print("Downloading tag list...")
        download(TAGS_URL, TAGS_FILE, "tags")


def load_session(provider=None, device_id=None):
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.enable_mem_pattern = False
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    available_providers = ort.get_available_providers()

    # Explicit provider override (used by bench)
    if provider == "cpu":
        print("Loading CPU Execution Provider...")
        return ort.InferenceSession(MODEL_FILE, sess_options=opts, providers=["CPUExecutionProvider"])

    if provider == "dml" and device_id is not None:
        if "DmlExecutionProvider" in available_providers:
            sess = ort.InferenceSession(
                MODEL_FILE, sess_options=opts,
                providers=[("DmlExecutionProvider", {"device_id": device_id}), "CPUExecutionProvider"]
            )
            print(f"Loaded DirectML GPU (device_id={device_id}).")
            return sess

    # Default auto-detection logic (used by scan/inbox)
    if "DmlExecutionProvider" in available_providers:
        for dev_id in ([DML_DEVICE] if DML_DEVICE is not None else [1, 0]):
            try:
                sess = ort.InferenceSession(
                    MODEL_FILE, sess_options=opts,
                    providers=[("DmlExecutionProvider", {"device_id": dev_id}), "CPUExecutionProvider"]
                )
                print(f"Loaded WD14 model on GPU via DirectML (device_id={dev_id}).")
                return sess
            except Exception:
                continue

    print("GPU acceleration unavailable. Loading CPU Execution Provider...")
    return ort.InferenceSession(MODEL_FILE, sess_options=opts, providers=["CPUExecutionProvider"])


def preprocess(path):
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.alpha_composite(im)
                im = bg.convert("RGB")
            else:
                im = im.convert("RGB")
            side = max(im.size)
            sq = Image.new("RGB", (side, side), (255, 255, 255))
            sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            sq = sq.resize((448, 448), Image.Resampling.BICUBIC)
        return path, np.asarray(sq, dtype=np.float32)[:, :, ::-1]
    except Exception as e:
        print(f"  unreadable, skipped: {os.path.basename(path)} ({e})")
        return path, None


# ==============================================================================
# CACHE & IMAGE LISTING
# ==============================================================================
def db_connect():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS scores ("
        "path TEXT PRIMARY KEY, size INTEGER, mtime REAL, top TEXT)"
    )
    return con


def list_images(folder=None):
    out = []
    search_root = folder or DEST_DIR
    if os.path.exists(search_root):
        for root, _, files in os.walk(search_root):
            for f in files:
                if f.lower().endswith(VALID_EXT):
                    out.append(os.path.join(root, f))
    return out


# ==============================================================================
# SCAN
# ==============================================================================
def cmd_scan(folder=None, quiet=False):
    ensure_files()
    df = pd.read_csv(TAGS_FILE)
    char_idx = np.where(df["category"] == 4)[0]
    char_names = df.loc[char_idx, "name"].values

    con = db_connect()
    done = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path,size,mtime FROM scores")}

    todo = []
    for p in list_images(folder):
        st = os.stat(p)
        if done.get(p) != (st.st_size, st.st_mtime):
            todo.append(p)
    if not quiet or todo:
        print(f"{len(todo)} images to scan ({len(done)} already cached).")
    if not todo:
        return

    sess = load_session()
    input_name = sess.get_inputs()[0].name
    pool = ThreadPoolExecutor(max_workers=IO_THREADS)

    chunks = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    pending = [pool.submit(preprocess, p) for p in chunks[0]]
    scanned = 0

    for ci, chunk in enumerate(chunks):
        loaded = [f.result() for f in pending]
        if ci + 1 < len(chunks):
            pending = [pool.submit(preprocess, p) for p in chunks[ci + 1]]

        ok = [(p, a) for p, a in loaded if a is not None]
        if ok:
            batch = np.stack([a for _, a in ok])
            try:
                probs = sess.run(None, {input_name: batch})[0]
            except Exception:
                probs = np.concatenate(
                    [sess.run(None, {input_name: a[None]})[0] for _, a in ok]
                )
            for (p, _), row in zip(ok, probs):
                cp = row[char_idx]
                order = np.argsort(-cp)[:5]
                top = [[str(char_names[i]), float(cp[i])] for i in order if cp[i] >= STORE_MIN_PROB]
                st = os.stat(p)
                con.execute(
                    "INSERT OR REPLACE INTO scores VALUES (?,?,?,?)",
                    (p, st.st_size, st.st_mtime, json.dumps(top)),
                )
        scanned += len(chunk)
        con.commit()
        if (ci + 1) % max(1, 40 // BATCH_SIZE) == 0 or ci + 1 == len(chunks):
            print(f"Scanned {scanned}/{len(todo)}")

    con.close()
    if not quiet:
        print("Scan complete. Now run:  python char_sorter.py sort --dry-run")


# ==============================================================================
# SORTING LOGIC
# ==============================================================================
def split_tag(raw):
    m = re.search(r"\((.*?)\)\s*$", raw)
    base = re.sub(r"\s*\(.*?\)\s*$", "", raw)
    series = m.group(1) if m else ""
    return base, series


def pretty(s):
    return s.replace("_", " ").title().strip()


def decide(top):
    present = [t for t in top if t[1] >= CONFIDENT]
    if len(present) == 1:
        return "char", present[0][0]
    if len(present) > 1:
        if PRIMARY_MARGIN is not None and present[0][1] - present[1][1] >= PRIMARY_MARGIN:
            return "char", present[0][0]
        return "multiple", None
    if top and top[0][1] >= REVIEW:
        return "review", top[0][0]
    return "unsorted", None


def safe(name):
    return INVALID_CHARS.sub("", name).strip(" .") or "Unknown"


def unique_path(folder, filename):
    dest = os.path.join(folder, filename)
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(filename)
    n = 1
    while os.path.exists(os.path.join(folder, f"{stem}_{n}{ext}")):
        n += 1
    return os.path.join(folder, f"{stem}_{n}{ext}")


TWEET_ID_RE = re.compile(r"(?<!\d)(\d{15,20})(?!\d)")


def norm_key(s):
    return re.sub(r"[\W_]+", "", s.lower())


def to_path(name):
    return os.path.join(*[safe(p) for p in name.replace("\\", "/").split("/") if p])


def is_series(name):
    return name.replace("\\", "/").split("/")[0].lower() == "_series"


def load_hashtag_map():
    if not os.path.exists(HASHTAG_MAP_FILE):
        return {}
    out = {}
    with open(HASHTAG_MAP_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tag = (row.get("hashtag") or "").strip().lstrip("#").lower()
            folder = (row.get("folder") or "").strip()
            if tag and folder:
                out[tag] = folder
    return out


def load_tweet_tags():
    if not os.path.exists(TWEET_TAGS_FILE):
        return {}
    out = {}
    with open(TWEET_TAGS_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tid = (row.get("tweet_id") or "").strip()
            if tid:
                out[tid] = set((row.get("hashtags") or "").split())
    return out


def load_tweet_authors():
    """tweet_id -> author (lowercase) from data/tweet_hashtags.csv."""
    if not os.path.exists(TWEET_TAGS_FILE):
        return {}
    out = {}
    with open(TWEET_TAGS_FILE, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            tid = (row.get("tweet_id") or "").strip()
            a = (row.get("author") or "").strip().lower()
            if tid and a:
                out[tid] = a
    return out


def cmd_sort(dry_run, only_under=None):
    con = db_connect()
    rows = [(p, json.loads(t)) for p, t in con.execute("SELECT path, top FROM scores")]
    rows = [(p, t) for p, t in rows if os.path.exists(p)]
    if not rows:
        print("Nothing to sort. Run `scan` first.")
        return

    base = [(p, *decide(t)) for p, t in rows]

    bases = defaultdict(set)
    for _, kind, raw in base:
        if kind in ("char", "review"):
            bases[pretty(split_tag(raw)[0])].add(raw)

    def folder_name(raw):
        b, series = split_tag(raw)
        name = pretty(b)
        if len(bases[name]) > 1 and series:
            name = f"{name} ({pretty(series)})"
        return safe(name)

    hmap = load_hashtag_map()
    ttags = load_tweet_tags()
    print(f"Hashtag rules: {len(hmap)}, tweets with hashtag data: {len(ttags)}")
    if not ttags:
        print("[!] No hashtag data found. Run `python char_sorter.py rebuild-tags` to recover it from saved sidecars.")

    canon = {}
    tweet_chars = defaultdict(set)
    for p, kind, raw in base:
        if kind in ("char", "review"):
            canon.setdefault(norm_key(folder_name(raw)), folder_name(raw))
        if kind == "char":
            m = TWEET_ID_RE.search(os.path.basename(p))
            if m:
                tweet_chars[m.group(1)].add(folder_name(raw))

    def canonical(name):
        if name.startswith("_"):
            return name
        return canon.get(norm_key(name), name)

    # Author hints: learn what each artist usually draws, using ONLY tagger-confirmed
    # images (counted per distinct tweet), so hashtag/sibling placements never feed back in.
    authors = load_tweet_authors()
    author_votes = defaultdict(lambda: defaultdict(set))
    for p, kind, raw in base:
        if kind == "char":
            m = TWEET_ID_RE.search(os.path.basename(p))
            a = authors.get(m.group(1)) if m else None
            if a:
                author_votes[a][folder_name(raw)].add(m.group(1))
    author_rule = {}
    if USE_AUTHOR_HINTS:
        for a, votes in author_votes.items():
            total = sum(len(t) for t in votes.values())
            top, tids = max(votes.items(), key=lambda kv: len(kv[1]))
            if total >= AUTHOR_MIN and len(tids) / total >= AUTHOR_SHARE:
                author_rule[a] = top
        print(f"Author hints: {len(author_rule)} artists with a clear main character "
              f"(>= {AUTHOR_MIN} tweets, >= {AUTHOR_SHARE:.0%} same character)")
        enough = sum(1 for v in author_votes.values() if sum(len(t) for t in v.values()) >= AUTHOR_MIN)
        print(f"  Author known for {len(authors)} tweets; {len(author_votes)} artists have tagger-confirmed "
              f"images, {enough} of them with >= {AUTHOR_MIN} tweets.")

    plan_items = []
    for p, kind, raw in base:
        forced, src = None, ""
        if kind != "char":
            m = TWEET_ID_RE.search(os.path.basename(p))
            tid = m.group(1) if m else None
            tags = ttags.get(tid, set()) if tid else set()
            folders = {hmap[t] for t in tags if t in hmap}
            char_f = {f for f in folders if not is_series(f)}
            series_f = {f for f in folders if is_series(f)}
            if len(char_f) == 1:
                forced, src = canonical(next(iter(char_f))), "hashtag"
            elif len(char_f) == 0:
                sib = tweet_chars.get(tid, set()) if tid else set()
                if USE_SIBLINGS and kind in ("review", "unsorted") and len(sib) == 1:
                    forced, src = next(iter(sib)), "sibling"
                elif len(series_f) == 1:
                    forced, src = next(iter(series_f)), "series"
            if not forced and len(char_f) < 2 and kind in ("review", "unsorted") and tid:
                hint = author_rule.get(authors.get(tid, ""))
                if hint:
                    forced, src = hint, "author"
        plan_items.append((p, kind, raw, forced, src))

    # Last fallback: still-unsorted images grouped by artist (only artists with enough of them)
    artist_counts = Counter()
    if ARTIST_MIN > 0:
        for p, kind, raw, forced, src in plan_items:
            if not forced and kind == "unsorted":
                m = TWEET_ID_RE.search(os.path.basename(p))
                a = authors.get(m.group(1)) if m else None
                if a:
                    artist_counts[a] += 1
        qualifying = {a: n for a, n in artist_counts.items() if n >= ARTIST_MIN}
        print(f"Artist folders: {len(qualifying)} artists with >= {ARTIST_MIN} unsorted images "
              f"({sum(qualifying.values())} images)")

    counts = Counter()
    for p, kind, raw, forced, src in plan_items:
        if forced and not forced.startswith("_"):
            counts[forced] += 1
        elif kind == "char":
            counts[folder_name(raw)] += 1

    plan = []
    src_counts = Counter()
    for p, kind, raw, forced, src in plan_items:
        if forced:
            if forced.startswith("_"):
                folder = to_path(forced)
            else:
                folder = forced if counts[forced] >= MIN_FOLDER_SIZE else FOLDER_MISC
            src_counts[src] += 1
        elif kind == "char":
            name = folder_name(raw)
            folder = name if counts[name] >= MIN_FOLDER_SIZE else FOLDER_MISC
        elif kind == "review":
            folder = os.path.join(FOLDER_REVIEW, folder_name(raw))
        elif kind == "multiple":
            folder = FOLDER_MULTIPLE
        else:
            folder = FOLDER_UNSORTED
            if ARTIST_MIN > 0:
                m = TWEET_ID_RE.search(os.path.basename(p))
                a = authors.get(m.group(1)) if m else None
                if a and artist_counts[a] >= ARTIST_MIN:
                    folder = os.path.join(FOLDER_ARTISTS, safe(a))
                    src = "artist"
                    src_counts[src] += 1
        plan.append((p, folder, src))

    if only_under:
        root = os.path.normcase(os.path.abspath(only_under)) + os.sep
        plan = [x for x in plan if os.path.normcase(os.path.abspath(x[0])).startswith(root)]
        src_counts = Counter(src for _, _, src in plan if src)

    summary = Counter(f for _, f, _ in plan)
    print(f"\n{len(plan)} images -> {len(summary)} folders. Top {SUMMARY_TOP}:")
    for f, n in summary.most_common(SUMMARY_TOP):
        print(f"  {n:6d}  {f}")
    if src_counts:
        print("\nPlaced using tweet evidence instead of the tagger:")
        for k, n in src_counts.most_common():
            print(f"  {n:6d}  by {k}")

    if dry_run:
        with open("sort_preview.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["file", "folder", "source"])
            w.writerows(plan)
        print("\nDry run only. Full plan written to sort_preview.csv. Nothing moved.")
        return

    os.makedirs(DEST_DIR, exist_ok=True)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as lf:
        log = csv.writer(lf)
        moved = 0
        for i, (p, folder, _) in enumerate(plan, 1):
            target = os.path.join(DEST_DIR, folder)
            same = os.path.normcase(os.path.abspath(os.path.dirname(p))) == os.path.normcase(os.path.abspath(target))
            if not same:
                os.makedirs(target, exist_ok=True)
                dest = unique_path(target, os.path.basename(p))
                shutil.move(p, dest)
                log.writerow([p, dest])
                con.execute("UPDATE scores SET path=? WHERE path=?", (dest, p))
                moved += 1
            if i % 200 == 0:
                lf.flush()
                con.commit()
                print(f"Moved {i}/{len(plan)}")
    con.commit()
    con.close()
    print(f"\nDone. Moved {moved} files. Undo log: {LOG_FILE}")


# ==============================================================================
# INGEST SIDECARS & INBOX PROCESSING
# ==============================================================================
HASHTAG_IN_TEXT = re.compile(r"#(\w+)")


def load_tag_data():
    """tweet_id -> [set(hashtags), author] from data/tweet_hashtags.csv."""
    data = {}
    if os.path.exists(TWEET_TAGS_FILE):
        with open(TWEET_TAGS_FILE, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                tid = (r.get("tweet_id") or "").strip()
                if tid:
                    data[tid] = [set((r.get("hashtags") or "").split()), r.get("author") or ""]
    return data


def save_tag_data(data):
    with open(TWEET_TAGS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["tweet_id", "hashtags", "author"])
        for tid, (tags, author) in data.items():
            if tags or author:
                w.writerow([tid, " ".join(sorted(tags)), author])


def read_sidecar(full):
    """Parse one gallery-dl .json sidecar -> (tweet_id, hashtags, author) or None."""
    try:
        with open(full, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    tid = str(d.get("tweet_id") or d.get("id") or "").strip()
    if not tid:
        return None
    raw = d.get("hashtags")
    if not raw:  # fall back to pulling #tags out of the tweet text
        raw = HASHTAG_IN_TEXT.findall(str(d.get("content") or d.get("text") or ""))
    tags = {str(h).lower().lstrip("#") for h in raw}
    a = d.get("author")
    if isinstance(a, dict):
        author = a.get("name") or a.get("screen_name") or a.get("nick") or ""
    else:
        author = str(a) if a else ""
    return tid, tags, author


def ingest_sidecars(folders, move=False):
    """Read gallery-dl .json sidecars under `folders` -> tweet_hashtags.csv (merged).
    If move=True, processed sidecars are moved into META_DIR."""
    data = load_tag_data()
    seen, sidecars = 0, []
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for root, _, files in os.walk(folder):
            for f in files:
                if not f.lower().endswith(".json"):
                    continue
                full = os.path.join(root, f)
                rec = read_sidecar(full)
                if not rec:
                    continue
                tid, tags, author = rec
                entry = data.setdefault(tid, [set(), ""])
                entry[0] |= tags
                if author:
                    entry[1] = author
                sidecars.append(full)
                seen += 1
    if seen:
        save_tag_data(data)
        if move:
            os.makedirs(META_DIR, exist_ok=True)
            for full in sidecars:
                shutil.move(full, unique_path(META_DIR, os.path.basename(full)))
    return seen


def merge_tag_csv(path):
    """Merge an old tweet_hashtags.csv (tweet_id,hashtags,author) into the current one."""
    data = load_tag_data()
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tid = (r.get("tweet_id") or "").strip()
            if not tid:
                continue
            entry = data.setdefault(tid, [set(), ""])
            entry[0] |= {t.lower().lstrip("#") for t in (r.get("hashtags") or "").split()}
            if r.get("author"):
                entry[1] = r["author"]
            n += 1
    save_tag_data(data)
    return n


def cmd_rebuild_tags(paths):
    """Recover hashtag data from saved sidecars (default: Meta/ and Inbox/) or an old CSV."""
    paths = paths or [META_DIR, INBOX_DIR]
    for p in paths:
        if os.path.isfile(p) and p.lower().endswith(".csv"):
            print(f"Merged {merge_tag_csv(p)} rows from {p}")
        elif os.path.isdir(p):
            print(f"Read {ingest_sidecars([p])} sidecar files from {p}")
        else:
            print(f"[!] Not found / not a folder or .csv: {p}")
    print(f"Tweets with hashtag data: {len(load_tag_data())}  ({TWEET_TAGS_FILE})")


def cmd_suggest_map(min_tweets=5, min_share=0.8, apply=False):
    """Suggest hashtag -> character rules from tweets whose image the tagger already identified.
    A hashtag qualifies if, over tweets showing exactly one tagger-confirmed character, it points to the
    same character at least `min_share` of the time, on >= `min_tweets` tweets from >= 3 different artists."""
    con = db_connect()
    rows = [(p, json.loads(t)) for p, t in con.execute("SELECT path, top FROM scores")]
    rows = [(p, t) for p, t in rows if os.path.exists(p)]
    ttags = load_tweet_tags()
    if not rows or not ttags:
        print("Need scanned images and hashtag data first (`scan`, and `rebuild-tags` if needed).")
        return
    base = [(p, *decide(t)) for p, t in rows]
    bases = defaultdict(set)
    for _, kind, raw in base:
        if kind in ("char", "review"):
            bases[pretty(split_tag(raw)[0])].add(raw)

    def folder_name(raw):
        b, series = split_tag(raw)
        name = pretty(b)
        if len(bases[name]) > 1 and series:
            name = f"{name} ({pretty(series)})"
        return safe(name)

    authors = load_tweet_authors()
    hmap = load_hashtag_map()
    tweet_chars = defaultdict(set)
    for p, kind, raw in base:
        if kind == "char":
            m = TWEET_ID_RE.search(os.path.basename(p))
            if m:
                tweet_chars[m.group(1)].add(folder_name(raw))

    votes = defaultdict(lambda: defaultdict(set))  # hashtag -> character -> tweet ids
    for tid, chars in tweet_chars.items():
        if len(chars) != 1:
            continue
        char = next(iter(chars))
        for tag in ttags.get(tid, ()):
            if tag not in hmap:
                votes[tag][char].add(tid)

    found = []
    for tag, per_char in votes.items():
        total = sum(len(t) for t in per_char.values())
        char, tids = max(per_char.items(), key=lambda kv: len(kv[1]))
        n_artists = len({authors[t] for t in tids if t in authors})
        if total >= min_tweets and len(tids) / total >= min_share and n_artists >= 3:
            found.append((tag, char, len(tids), len(tids) / total, n_artists))
    found.sort(key=lambda r: (-r[2], r[0]))

    out_file = os.path.join(CONFIG_DIR, "hashtag_map_suggested.csv")
    with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["hashtag", "folder", "tweets", "share", "artists"])
        for tag, char, n, share, na in found:
            w.writerow([tag, char, n, f"{share:.2f}", na])
    print(f"{len(found)} suggested rules (>= {min_tweets} tweets, >= {min_share:.0%} same character, >= 3 artists).")
    for tag, char, n, share, na in found[:40]:
        print(f"{n:5d}  {share:4.0%}  {tag}  ->  {char}")
    if len(found) > 40:
        print(f"  ... and {len(found) - 40} more in {out_file}")
    if not found:
        return
    if apply:
        new_file = not os.path.exists(HASHTAG_MAP_FILE) or os.path.getsize(HASHTAG_MAP_FILE) == 0
        needs_nl = False
        if not new_file:
            with open(HASHTAG_MAP_FILE, "rb") as fb:
                fb.seek(-1, os.SEEK_END)
                needs_nl = fb.read(1) not in (b"\n", b"\r")
        with open(HASHTAG_MAP_FILE, "a", newline="", encoding="utf-8") as f:
            if new_file:
                f.write("hashtag,folder\n")
            elif needs_nl:
                f.write("\n")
            w = csv.writer(f, lineterminator="\n")
            for tag, char, *_ in found:
                w.writerow([tag, char])
        print(f"Appended {len(found)} rules to {HASHTAG_MAP_FILE}. Re-run `sort --dry-run` to see the effect.")
    else:
        print(f"Review {out_file}, then copy the rows you want into {HASHTAG_MAP_FILE} (or re-run with --apply for all).")


def cmd_report(top=30):
    """Use the last `sort --dry-run` plan to show what the still-unsorted images have in common."""
    if not os.path.exists("sort_preview.csv"):
        print("Run `python char_sorter.py sort --dry-run` first (it writes sort_preview.csv).")
        return
    data = load_tag_data()
    hmap = load_hashtag_map()
    n = no_data = 0
    tag_counts, author_counts = Counter(), Counter()
    with open("sort_preview.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["folder"] != FOLDER_UNSORTED:
                continue
            n += 1
            m = TWEET_ID_RE.search(os.path.basename(r["file"]))
            entry = data.get(m.group(1)) if m else None
            if not entry:
                no_data += 1
                continue
            tags, author = entry
            for t in tags:
                if t not in hmap:
                    tag_counts[t] += 1
            if author:
                author_counts[author.lower()] += 1
    print(f"{n} images in {FOLDER_UNSORTED} ({no_data} with no tweet data).")
    print(f"\nTop {top} hashtags on them that have no rule in hashtag_map.csv:")
    for t, c in tag_counts.most_common(top):
        print(f"{c:6d}  {t}")
    print(f"\nTop {top} artists among them:")
    for a, c in author_counts.most_common(top):
        print(f"{c:6d}  {a}")


def cmd_tags(top=50, show_all=False):
    """Most common hashtags, so you know what to add to config/hashtag_map.csv."""
    ttags = load_tweet_tags()
    if not ttags:
        print("No hashtag data yet. Run `python char_sorter.py rebuild-tags` (or sync some likes first).")
        return
    hmap = load_hashtag_map()
    counts = Counter(t for tags in ttags.values() for t in tags)
    label = "all hashtags" if show_all else "hashtags NOT yet in hashtag_map.csv"
    print(f"{len(ttags)} tweets with hashtag data. Top {top} {label}:")
    shown = 0
    for tag, n in counts.most_common():
        if tag in hmap and not show_all:
            continue
        print(f"{n:6d}  {tag}" + (f"  -> {hmap[tag]}" if tag in hmap else ""))
        shown += 1
        if shown >= top:
            break


def cmd_inbox(dry_run):
    if not list_images(INBOX_DIR):
        print(f"No new images in {INBOX_DIR}.")
        return
    seen = ingest_sidecars([INBOX_DIR], move=not dry_run)
    print(f"Tweet metadata read from {seen} sidecar files.")
    cmd_scan(INBOX_DIR, quiet=True)
    cmd_sort(dry_run, only_under=INBOX_DIR)
    if not dry_run:
        for root, dirs, files in os.walk(INBOX_DIR, topdown=False):
            if root != INBOX_DIR and not os.listdir(root):
                os.rmdir(root)


# ==============================================================================
# UNDO & BENCH
# ==============================================================================
def cmd_undo():
    if not os.path.exists(LOG_FILE):
        raise SystemExit(f"No {LOG_FILE} found.")
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    restored = missing = blocked = 0
    con = db_connect()
    for old, new in reversed(rows):
        if not os.path.exists(new):
            missing += 1
        elif os.path.exists(old):
            blocked += 1
        else:
            os.makedirs(os.path.dirname(old), exist_ok=True)
            shutil.move(new, old)
            con.execute("UPDATE scores SET path=? WHERE path=?", (old, new))
            restored += 1
    con.commit()
    con.close()
    print(f"Restored {restored}, missing {missing}, blocked {blocked}.")
    if restored and not missing and not blocked:
        os.replace(LOG_FILE, LOG_FILE + ".undone")


def cmd_bench():
    import time
    ensure_files()
    dummy = np.random.rand(BATCH_SIZE, 448, 448, 3).astype(np.float32) * 255
    tests = [
        ("GPU device_id=0", "dml", 0),
        ("GPU device_id=1", "dml", 1),
        ("CPU", "cpu", None)
    ]
    results = []
    for label, provider, dev_id in tests:
        try:
            sess = load_session(provider=provider, device_id=dev_id)
        except Exception as e:
            print(f"{label}: unavailable ({e})\n")
            continue
        name = sess.get_inputs()[0].name
        
        # Warmup execution
        sess.run(None, {name: dummy})
        
        # Benchmark run
        t = time.time()
        iters = 5
        for _ in range(iters):
            sess.run(None, {name: dummy})
        elapsed = time.time() - t
        
        rate = (iters * BATCH_SIZE) / elapsed
        results.append((rate, label))
        print(f"{label}: {rate:.1f} images/sec\n")

    print("Summary:")
    for rate, label in sorted(results, reverse=True):
        print(f"  {rate:5.1f} img/s  {label}")



# ==============================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["scan", "sort", "undo", "bench", "inbox", "tags", "rebuild-tags", "report", "suggest-map"])
    ap.add_argument("paths", nargs="*", help="rebuild-tags: folders of .json sidecars or an old tweet_hashtags.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=50, help="tags: how many hashtags to show")
    ap.add_argument("--all", action="store_true", help="tags: include hashtags already mapped")
    ap.add_argument("--min-tweets", type=int, default=5, help="suggest-map: minimum tweets per hashtag")
    ap.add_argument("--min-share", type=float, default=0.8, help="suggest-map: required share for the top character")
    ap.add_argument("--apply", action="store_true", help="suggest-map: append the suggestions to hashtag_map.csv")
    a = ap.parse_args()
    if a.command == "scan":
        cmd_scan()
    elif a.command == "bench":
        cmd_bench()
    elif a.command == "inbox":
        cmd_inbox(a.dry_run)
    elif a.command == "suggest-map":
        cmd_suggest_map(a.min_tweets, a.min_share, a.apply)
    elif a.command == "report":
        cmd_report(a.top)
    elif a.command == "tags":
        cmd_tags(a.top, a.all)
    elif a.command == "rebuild-tags":
        cmd_rebuild_tags(a.paths)
    elif a.command == "sort":
        cmd_sort(a.dry_run)
    else:
        cmd_undo()