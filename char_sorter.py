"""
char_sorter.py  -  tag anime images with a character/series tagger (Camie Tagger v2 or WD14) and cache the scores.
push_to_hydrus.py then sends the images and tags to Hydrus Network. Nothing is sorted into folders.

Everyday command (run_update.bat does this for you):
    python char_sorter.py intake           # read tweet sidecars, tag every new image in the Inbox (nothing moves)

Other commands:
    python char_sorter.py scan             # tag the Inbox (same scan, without reading sidecars)
    python char_sorter.py scan <folder>    # tag any folder of images instead
    python char_sorter.py bench            # test GPU/CPU speed and pick dml_device_id / batch_size
    python char_sorter.py rebuild-tags                 # rebuild data/tweet_hashtags.csv from Meta/ + Inbox/ sidecars
    python char_sorter.py rebuild-tags <folder|.csv>   # ...or from any folder of .json sidecars / an old tweet_hashtags.csv
"""
import argparse
import csv
import json
import os
import re
import shutil
import sqlite3
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
    # Options from the old folder sorter. They no longer do anything; old config files may still contain them.
    legacy_keys = {
        "confidence_threshold", "review_threshold", "primary_margin", "min_folder_size", "auto_learn_manual_moves",
        "series_min_score", "series_margin", "series_folder_min", "series_ignore", "merge_variants",
        "use_sibling_hints", "use_author_hints", "author_min_tweets", "author_min_share", "artist_folder_min",
        "folder_unsorted", "folder_multiple", "folder_review", "folder_misc", "folder_artists", "summary_top_folders",
    }
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
        "tagger_backend": "wd14",  # "wd14" or "camie"
        "camie_model_url": "https://huggingface.co/Camais03/camie-tagger-v2/resolve/main/camie-tagger-v2.onnx",
        "camie_metadata_url": "https://huggingface.co/Camais03/camie-tagger-v2/resolve/main/camie-tagger-v2-metadata.json",
        "dml_device_id": None,       # None = auto (tries GPU 1 then 0); set a number to force one
        "batch_size": 4,             # images per tagger pass; lower it on low-VRAM GPUs (run `bench`)
        "io_threads": 4,             # threads that load/resize images while the GPU works
        "cache_min_score": 0.10,     # guesses below this are not cached (lowering needs a rescan)
        "image_extensions": [".jpg", ".jpeg", ".png", ".webp"],
        "model_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/model.onnx",
        "tags_url": "https://huggingface.co/SmilingWolf/wd-v1-4-convnextv2-tagger-v2/resolve/main/selected_tags.csv",
        # --- tags ---
        "series_from_copyright": True,  # camie only: also store each image's copyright (series) guesses, pushed as series: tags
        "character_aliases": {},     # {"trailblazer_(honkai:_star_rail)": "stelle_(honkai:_star_rail)"}: tag -> tag you want it merged into
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                user_cfg = json.load(f)
            unknown = sorted(set(user_cfg) - set(default_config) - legacy_keys)
            if unknown:
                print(f"[!] Warning: unknown config keys ignored (typo?): {', '.join(unknown)}")
            default_config.update(user_cfg)
        except Exception as e:
            print(f"[!] Warning: Failed to read {CONFIG_FILE} ({e}). Using default settings.")
    return default_config

cfg = load_config()

TAGGER_BACKEND = str(cfg.get("tagger_backend", "wd14")).strip().lower()

if TAGGER_BACKEND not in {"wd14", "camie"}:
    raise ValueError(
        f"Invalid tagger_backend: {TAGGER_BACKEND!r}. "
        "Choose 'wd14' or 'camie'."
    )

# Internal directory structure
DATA_DIR = "data"
MODEL_DIR = "model"
CONFIG_DIR = "config"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

# Main output directory paths
PARENT_DIR = os.path.abspath(os.path.expanduser(cfg["parent_dir"]))
PC_PULL_DIR = os.path.join(PARENT_DIR, "Incoming")
INBOX_DIR = os.path.join(PC_PULL_DIR, "Inbox")
META_DIR = os.path.join(PARENT_DIR, "Meta")

# Model and tag paths
MODEL_URL = cfg["model_url"]
TAGS_URL = cfg["tags_url"]

WD14_MODEL_FILE = os.path.join(MODEL_DIR, "wd14_model.onnx")
WD14_TAGS_FILE = os.path.join(MODEL_DIR, "selected_tags.csv")

CAMIE_MODEL_FILE = os.path.join(MODEL_DIR, "camie-tagger-v2.onnx")
CAMIE_METADATA_FILE = os.path.join(
    MODEL_DIR, "camie-tagger-v2-metadata.json"
)

if TAGGER_BACKEND == "camie":
    MODEL_FILE = CAMIE_MODEL_FILE
    TAGS_FILE = CAMIE_METADATA_FILE
    DB_FILE = os.path.join(DATA_DIR, "scores_cache_camie.db")
else:
    MODEL_FILE = WD14_MODEL_FILE
    TAGS_FILE = WD14_TAGS_FILE
    DB_FILE = os.path.join(DATA_DIR, "scores_cache.db")

# Data files
TWEET_TAGS_FILE = os.path.join(DATA_DIR, "tweet_hashtags.csv")

BATCH_SIZE = max(1, int(cfg.get("batch_size", 4)))
IO_THREADS = max(1, int(cfg["io_threads"]))
STORE_MIN_PROB = float(cfg["cache_min_score"])

# Sorting threshold parameters from config
SERIES_ON = bool(cfg["series_from_copyright"]) and TAGGER_BACKEND == "camie"
ALIASES = {str(k).strip().lower(): str(v).strip().lower() for k, v in (cfg.get("character_aliases") or {}).items()}
DML_DEVICE = cfg.get("dml_device_id")

VALID_EXT = tuple(("." + e.lstrip(".")).lower() for e in cfg["image_extensions"])


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
    if TAGGER_BACKEND == "camie":
        if not os.path.exists(CAMIE_MODEL_FILE):
            print("Downloading Camie Tagger v2 model (one-time)...")
            download(cfg["camie_model_url"], CAMIE_MODEL_FILE, "model")

        if not os.path.exists(CAMIE_METADATA_FILE):
            print("Downloading Camie Tagger v2 metadata...")
            download(
                cfg["camie_metadata_url"],
                CAMIE_METADATA_FILE,
                "metadata",
            )

    else:  # WD14
        if not os.path.exists(WD14_MODEL_FILE):
            print("Downloading WD14 model (a few hundred MB, one-time)...")
            download(MODEL_URL, WD14_MODEL_FILE, "model")

        if not os.path.exists(WD14_TAGS_FILE):
            print("Downloading tag list...")
            download(TAGS_URL, WD14_TAGS_FILE, "tags")

def load_camie_metadata():
    """Load and validate Camie Tagger v2 metadata."""
    with open(CAMIE_METADATA_FILE, "r", encoding="utf-8") as f:
        metadata = json.load(f)

    try:
        tag_mapping = metadata["dataset_info"]["tag_mapping"]
        idx_to_tag = tag_mapping["idx_to_tag"]
        tag_to_category = tag_mapping["tag_to_category"]
    except (KeyError, TypeError) as e:
        raise ValueError(
            "Camie metadata is missing the expected tag mapping."
        ) from e

    if not idx_to_tag or not tag_to_category:
        raise ValueError("Camie metadata contains an empty tag mapping.")

    return metadata

def load_camie_category_tags(category, required=True):
    """Return (indices, names) of Camie tags in one category, in model-output order."""
    metadata = load_camie_metadata()
    tag_mapping = metadata["dataset_info"]["tag_mapping"]
    idx_to_tag = tag_mapping["idx_to_tag"]
    tag_to_category = tag_mapping["tag_to_category"]

    pairs = sorted(
        (int(index_str), tag_name)
        for index_str, tag_name in idx_to_tag.items()
        if str(tag_to_category.get(tag_name, "")).lower() == category
    )
    if not pairs and required:
        raise ValueError(f"No {category} tags found in Camie metadata.")
    return (
        np.array([i for i, _ in pairs], dtype=np.int64),
        np.array([n for _, n in pairs]),
    )


def load_camie_character_tags():
    """Return Camie's character-tag indices and names."""
    return load_camie_category_tags("character")


def load_camie_copyright_tags():
    """Return Camie's copyright (series) tag indices and names; empty arrays if the model has none."""
    return load_camie_category_tags("copyright", required=False)


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
                print(f"Loaded {TAGGER_BACKEND} model on GPU via DirectML (device_id={dev_id}).")
                return sess
            except Exception:
                continue

    print("GPU acceleration unavailable. Loading CPU Execution Provider...")
    return ort.InferenceSession(MODEL_FILE, sess_options=opts, providers=["CPUExecutionProvider"])


def preprocess(path):
    """WD14 preprocessing. Returns an HWC float32 array (BGR), or None if the file is unreadable."""
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
        return np.asarray(sq, dtype=np.float32)[:, :, ::-1]
    except Exception as e:
        print(f"  unreadable, skipped: {os.path.basename(path)} ({e})")
        return None


CAMIE_PAD = (124, 116, 104)  # ImageNet mean as RGB: the padding colour Camie was trained with
CAMIE_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
CAMIE_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def preprocess_camie(path, img_size=512):
    """Camie Tagger v2 preprocessing. Returns a CHW float32 array, or None if the file is unreadable.
    Like the reference onnx_inference.py it scales the long side to img_size (small images are upscaled
    too; Image.thumbnail would not), pads to a square with the ImageNet mean colour, then normalises."""
    try:
        with Image.open(path) as img:
            img.load()
            rgba = img.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (*CAMIE_PAD, 255))
            background.alpha_composite(rgba)
            img = background.convert("RGB")

            w, h = img.size
            scale = img_size / max(w, h)
            new_w = max(1, min(img_size, round(w * scale)))
            new_h = max(1, min(img_size, round(h * scale)))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            canvas = Image.new("RGB", (img_size, img_size), CAMIE_PAD)
            canvas.paste(img, ((img_size - new_w) // 2, (img_size - new_h) // 2))

        array = np.asarray(canvas, dtype=np.float32) / 255.0
        array = (array - CAMIE_MEAN) / CAMIE_STD
        return np.ascontiguousarray(np.transpose(array, (2, 0, 1)), dtype=np.float32)
    except Exception as e:
        print(f"  unreadable, skipped: {os.path.basename(path)} ({e})")
        return None

# ==============================================================================
# CACHE & IMAGE LISTING
# ==============================================================================
def db_connect():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS scores ("
        "path TEXT PRIMARY KEY, size INTEGER, mtime REAL, top TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS series_scores (name TEXT PRIMARY KEY, top TEXT)")
    return con


def list_images(folder=None):
    out = []
    search_root = folder or INBOX_DIR
    if os.path.exists(search_root):
        for root, _, files in os.walk(search_root):
            for f in files:
                if f.lower().endswith(VALID_EXT):
                    out.append(os.path.join(root, f))
    return out


# ==============================================================================
# SCAN
# ==============================================================================
_OUTPUT_NOTE_SHOWN = False
_SANITY_SHOWN = [False]


def pick_output(sess):
    """Index of the model output to use. Camie's ONNX can have several heads; prefer the refined one."""
    names = [o.name for o in sess.get_outputs()]
    idx = 0
    if TAGGER_BACKEND == "camie":
        by_name = [i for i, n in enumerate(names) if "refined" in n.lower()]
        if by_name:
            idx = by_name[0]
        elif len(names) >= 2:
            idx = 1  # Camie's reference script: output 0 = initial logits, output 1 = refined logits
    print(f"Model outputs: {names}; using '{names[idx]}'.")
    return idx


def to_probs(scores):
    """The WD models return probabilities; Camie's ONNX may return raw logits. Convert only when needed,
    so every threshold in the config keeps meaning a 0-1 probability."""
    global _OUTPUT_NOTE_SHOWN
    scores = np.asarray(scores, dtype=np.float32)
    needs_sigmoid = bool(scores.size) and (float(scores.min()) < 0.0 or float(scores.max()) > 1.0)
    if not _OUTPUT_NOTE_SHOWN:
        print("Model output looks like " + ("raw logits: applying sigmoid." if needs_sigmoid
                                            else "probabilities: used as is."))
        _OUTPUT_NOTE_SHOWN = True
    if needs_sigmoid:
        scores = 1.0 / (1.0 + np.exp(-np.clip(scores, -30.0, 30.0)))
    if not _SANITY_SHOWN[0] and scores.ndim == 2 and scores.shape[0]:
        _SANITY_SHOWN[0] = True
        n = float((scores >= 0.5).sum(axis=1).mean())
        print(f"Sanity check: {n:.0f} tags per image score >= 0.5 on the first batch "
              f"(of {scores.shape[1]}). Typically a few dozen; thousands would mean the wrong output is in use.")
    return scores


def cmd_scan(folder=None, quiet=False):
    ensure_files()
    if TAGGER_BACKEND == "camie":
        char_idx, char_names = load_camie_character_tags()
        preprocess_fn = preprocess_camie
    else:
        df = pd.read_csv(TAGS_FILE)
        char_idx = np.where(df["category"] == 4)[0]
        char_names = df.loc[char_idx, "name"].values
        preprocess_fn = preprocess

    series_on = SERIES_ON
    cr_idx = cr_names = None
    if series_on:
        cr_idx, cr_names = load_camie_copyright_tags()
        if len(cr_idx) == 0:
            series_on, cr_idx, cr_names = False, None, None

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
    out_idx = pick_output(sess)
    pool = ThreadPoolExecutor(max_workers=IO_THREADS)

    chunks = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    pending = [pool.submit(preprocess_fn, p) for p in chunks[0]]
    scanned = 0
    single_only = BATCH_SIZE == 1

    for ci, chunk in enumerate(chunks):
        loaded = [f.result() for f in pending]
        if ci + 1 < len(chunks):
            pending = [pool.submit(preprocess_fn, p) for p in chunks[ci + 1]]

        ok = [
            (p, result)
            for p, result in zip(chunk, loaded)
            if result is not None
        ]
        if ok:
            if single_only:
                probs = np.concatenate([sess.run(None, {input_name: a[None]})[out_idx] for _, a in ok])
            else:
                batch = np.stack([a for _, a in ok])
                try:
                    probs = sess.run(None, {input_name: batch})[out_idx]
                except Exception as e:
                    if len(ok) == 1:
                        raise
                    # Batch failed (often GPU out-of-memory): remember it and go one image at a time from
                    # now on, instead of paying for a failed batch before every retry.
                    single_only = True
                    print(f"  batch of {len(ok)} failed ({type(e).__name__}: {str(e)[:120]}); "
                          f"continuing one image at a time. Set batch_size to 1 in config.json to skip this.")
                    probs = np.concatenate([sess.run(None, {input_name: a[None]})[out_idx] for _, a in ok])
            probs = to_probs(probs)
            for (p, _), row in zip(ok, probs):
                cp = row[char_idx]
                order = np.argsort(-cp)[:5]
                top = [[str(char_names[i]), float(cp[i])] for i in order if cp[i] >= STORE_MIN_PROB]
                st = os.stat(p)
                con.execute(
                    "INSERT OR REPLACE INTO scores VALUES (?,?,?,?)",
                    (p, st.st_size, st.st_mtime, json.dumps(top)),
                )
                if series_on:
                    sp = row[cr_idx]
                    sorder = np.argsort(-sp)[:3]
                    stop = [[str(cr_names[i]), float(sp[i])] for i in sorder if sp[i] >= STORE_MIN_PROB]
                    con.execute(
                        "INSERT OR REPLACE INTO series_scores VALUES (?,?)",
                        (os.path.basename(p), json.dumps(stop)),
                    )
        scanned += len(chunk)
        con.commit()
        if (ci + 1) % max(1, 40 // BATCH_SIZE) == 0 or ci + 1 == len(chunks):
            print(f"Scanned {scanned}/{len(todo)}")

    con.close()
    if not quiet:
        print("Scan complete. Now run:  python push_to_hydrus.py")


# ==============================================================================
# SMALL HELPERS
# ==============================================================================


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


# ==============================================================================
# INGEST SIDECARS & INTAKE
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


def cmd_intake():
    """No sorting: read sidecars (moved to Meta/), tag every new image in the Inbox, leave the files where they are.
    push_to_hydrus.py then sends them (and any videos/GIFs) to Hydrus."""
    seen = ingest_sidecars([INBOX_DIR], move=True)
    if seen:
        print(f"Tweet metadata read from {seen} sidecar files.")
    if not list_images(INBOX_DIR):
        print(f"No new images to tag in {INBOX_DIR}.")
        return
    cmd_scan(INBOX_DIR, quiet=True)
    # An image the tagger couldn't read (corrupt download, odd format) is skipped by the scan; say so instead of
    # letting it silently never reach Hydrus.
    con = db_connect()
    cached = {r[0] for r in con.execute("SELECT path FROM scores")}
    con.close()
    bad = [p for p in list_images(INBOX_DIR) if p not in cached]
    if bad:
        print(f"[!] {len(bad)} Inbox image(s) could not be read or tagged, so they will NOT be pushed "
              f"(corrupt download?). First few:")
        for p in bad[:5]:
            print("    " + p)


# ==============================================================================
# BENCH
# ==============================================================================


def cmd_bench():
    import time

    ensure_files()

    if TAGGER_BACKEND == "camie":
        dummy = np.random.rand(
            BATCH_SIZE, 3, 512, 512
        ).astype(np.float32)
    else:
        dummy = (
            np.random.rand(BATCH_SIZE, 448, 448, 3)
            .astype(np.float32) * 255
        )

    tests = [
        ("GPU device_id=0", "dml", 0),
        ("GPU device_id=1", "dml", 1),
        ("CPU", "cpu", None),
    ]

    results = []

    print(f"Backend: {TAGGER_BACKEND}")
    print(f"Batch size: {BATCH_SIZE}")
    print("Benchmarking model inference only (preprocessing excluded).\n")

    for label, provider, dev_id in tests:
        try:
            sess = load_session(
                provider=provider,
                device_id=dev_id,
            )
        except Exception as e:
            print(f"{label}: unavailable ({e})\n")
            continue

        name = sess.get_inputs()[0].name

        try:
            # Warmup
            sess.run(None, {name: dummy})

            # Short benchmark
            iters = 5
            start = time.perf_counter()

            for _ in range(iters):
                sess.run(None, {name: dummy})

            elapsed = time.perf_counter() - start
            rate = (iters * BATCH_SIZE) / elapsed

            results.append((rate, label))
            print(
                f"{label}: {rate:.2f} images/sec "
                f"({elapsed:.2f}s for {iters * BATCH_SIZE} images)\n"
            )

        except Exception as e:
            print(f"{label}: benchmark failed ({e})\n")

        finally:
            del sess

    print("Summary:")
    for rate, label in sorted(results, reverse=True):
        print(f"  {rate:7.2f} img/s  {label}")


# ==============================================================================
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tag images for Hydrus. See the top of this file.")
    ap.add_argument("command", choices=["intake", "scan", "bench", "rebuild-tags"])
    ap.add_argument("paths", nargs="*", help="scan: a folder to tag; rebuild-tags: folders of .json sidecars or an old tweet_hashtags.csv")
    a = ap.parse_args()
    if a.command == "intake":
        cmd_intake()
    elif a.command == "scan":
        cmd_scan(a.paths[0] if a.paths else None)
    elif a.command == "bench":
        cmd_bench()
    else:
        cmd_rebuild_tags(a.paths)