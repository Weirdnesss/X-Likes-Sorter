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
    python char_sorter.py multiples                    # which characters appear together in the _Multiple images
    python char_sorter.py suggest-map                  # learn hashtag -> character rules from your own tagged images
    python char_sorter.py suggest-map --apply          # ...and append them to config/hashtag_map.csv

Manual sorting:
    python char_sorter.py learn                        # learn from files you moved by hand (also automatic on sort/inbox)

Hydrus export (tags instead of folders):
    python char_sorter.py export-hydrus <stage_dir> --limit 300   # copy a random test batch + tag sidecars into stage_dir
    python char_sorter.py export-hydrus                           # write <image>.txt tag sidecars next to every image
    python char_sorter.py export-hydrus --clean                   # delete those sidecars again
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
        # --- sorting thresholds ---
        "confidence_threshold": 0.35,
        "review_threshold": 0.20,
        "primary_margin": 0.25,
        "min_folder_size": 3,
        # --- hints for images the tagger could not place ---
        "auto_learn_manual_moves": True,  # sort/inbox first learn from files you moved by hand into character folders
        "series_from_copyright": True,  # camie only: unplaced images go to _Series/<name> from its copyright tags
        "series_min_score": 0.5,     # minimum copyright confidence
        "series_margin": 0.2,        # skip crossover art when the top two series are closer than this
        "series_folder_min": 5,      # new copyright-based series folders need this many images
        "series_ignore": ["original"],  # copyright tags that are not real series; those images use the next fallback
        "merge_variants": True,      # outfit variants (shiroko_(swimsuit)_(blue_archive)) count as one character
        "character_aliases": {},     # {"trailblazer_(honkai:_star_rail)": "stelle_(honkai:_star_rail)"}: tag -> tag you want it merged into
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
DEST_DIR = os.path.join(PARENT_DIR, "Sorted")
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
AUTO_LEARN = bool(cfg["auto_learn_manual_moves"])
SERIES_ON = bool(cfg["series_from_copyright"]) and TAGGER_BACKEND == "camie"
SERIES_MIN_SCORE = float(cfg["series_min_score"])
SERIES_MARGIN = float(cfg["series_margin"])
SERIES_FOLDER_MIN = int(cfg["series_folder_min"])
SERIES_IGNORE = {str(x).strip().lower() for x in cfg["series_ignore"]}
MERGE_VARIANTS = bool(cfg["merge_variants"])
ALIASES = {str(k).strip().lower(): str(v).strip().lower() for k, v in (cfg.get("character_aliases") or {}).items()}
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
    have_series = {r[0] for r in con.execute("SELECT name FROM series_scores")} if series_on else set()
    tops = {r[0]: r[1] for r in con.execute("SELECT path,top FROM scores")} if series_on else {}

    todo, n_backfill = [], 0
    for p in list_images(folder):
        st = os.stat(p)
        if done.get(p) != (st.st_size, st.st_mtime):
            todo.append(p)
        elif (series_on and os.path.basename(p) not in have_series
              and decide(json.loads(tops.get(p) or "[]"))[0] != "char"):
            # cached before series scores existed, and the tagger couldn't place it: rescan to get its series
            todo.append(p)
            n_backfill += 1
    if not quiet or todo:
        extra = f", {n_backfill} of them rescanned for series" if n_backfill else ""
        print(f"{len(todo)} images to scan ({len(done)} already cached{extra}).")
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


_PAREN = re.compile(r"\(([^()]*)\)")


def character_key(tag):
    """('shiroko', 'blue_archive') for shiroko_(blue_archive) and shiroko_(swimsuit)_(blue_archive) alike."""
    groups = _PAREN.findall(tag)
    base = re.sub(r"_+", "_", _PAREN.sub("", tag)).strip("_ ")
    return base, (groups[-1] if groups else "")


KNOWN_TAGS = set()


def set_known_tags(rows):
    """Remember every character tag the tagger has confidently seen in this library."""
    KNOWN_TAGS.clear()
    for _, top in rows:
        for tag, sc in top:
            if sc >= CONFIDENT:
                KNOWN_TAGS.add(tag)


def canonical_tag(tag):
    """Apply your character_aliases, then fold a costume variant (ouro_kronii_(1st_costume)) into its plain
    tag (ouro_kronii) when that plain tag has been seen in this library."""
    tag = ALIASES.get(tag, tag)
    if MERGE_VARIANTS and tag.endswith(")") and "_(" in tag:
        plain = tag[:tag.rindex("_(")]
        if plain in KNOWN_TAGS:
            return plain
    return tag


def merge_variants(top):
    """Collapse variants of one character into a single guess (best score, plain tag name)."""
    if not top or not (MERGE_VARIANTS or ALIASES):
        return top
    best = {}
    for tag, sc in top:
        t = canonical_tag(tag)
        best[t] = max(best.get(t, 0.0), sc)
    if not MERGE_VARIANTS:
        return sorted(([t, sc] for t, sc in best.items()), key=lambda x: -x[1])
    # costume variant next to its plain tag in the same image (sakamata_chloe + sakamata_chloe_(1st_costume))
    for t in list(best):
        for o in list(best):
            if o != t and t in best and o in best and t.endswith(")") and t.startswith(o + "_("):
                best[o] = max(best[o], best.pop(t))
                break
    # series-style variants (shiroko_(swimsuit)_(blue_archive) next to shiroko_(blue_archive))
    groups = {}
    for tag, sc in best.items():
        key = character_key(tag)
        g = groups.get(key)
        if g is None:
            groups[key] = [tag, sc, tag.count("(")]
        else:
            g[1] = max(g[1], sc)
            if tag.count("(") < g[2]:
                g[0], g[2] = tag, tag.count("(")
    merged = []
    for (base, series), (tag, sc, depth) in groups.items():
        if depth > 1:  # only costume variants were seen: rebuild the plain character name
            tag = f"{base}_({series})" if series else base
        merged.append([tag, sc])
    merged.sort(key=lambda x: -x[1])
    return merged


def decide(top):
    top = merge_variants(top)
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


def load_series_scores(con):
    """filename -> [[copyright_tag, score], ...] (best first), stored by a Camie scan."""
    return {name: json.loads(top or "[]") for name, top in con.execute("SELECT name, top FROM series_scores")}


def series_guess(guesses):
    """Pick one series folder name from an image's copyright guesses, or None when unsure."""
    cands = [(t, sc) for t, sc in (guesses or [])
             if sc >= SERIES_MIN_SCORE and t.lower() not in SERIES_IGNORE]
    tags = [t for t, _ in cands]
    # collapse sub-series: blue_archive_the_animation -> blue_archive
    cands = [(t, sc) for t, sc in cands if not any(t != o and t.startswith(o + "_") for o in tags)]
    if not cands:
        return None
    cands.sort(key=lambda x: -x[1])
    if len(cands) > 1 and cands[0][1] - cands[1][1] < SERIES_MARGIN:
        return None  # crossover art: the top two series are too close
    return safe(re.sub(r"\s*\(series\)\s*$", "", pretty(cands[0][0]), flags=re.I))


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


# ==============================================================================
# MANUAL SORTING: learn from files you moved by hand
# ==============================================================================
LABELS_FILE = os.path.join(DATA_DIR, "manual_labels.csv")


def load_labels():
    """filename -> folder (relative to Sorted/, '/' separated) for images you sorted by hand."""
    out = {}
    if os.path.exists(LABELS_FILE):
        with open(LABELS_FILE, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                name, label = (r.get("file") or "").strip(), (r.get("label") or "").strip()
                if name and label:
                    out[name] = label
    return out


def save_labels(labels):
    with open(LABELS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "label"])
        for name in sorted(labels):
            w.writerow([name, labels[name]])


def rel_under_sorted(path):
    """Path below the last 'Sorted' folder in an old cached path, lowercase and '/' separated."""
    parts = os.path.normpath(path).split(os.sep)
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lower() == "sorted":
            return "/".join(parts[i + 1:]).lower()
    return None


def label_for(new_path):
    """The character folder an image now sits in, or None for root/automatic (_...) folders."""
    rel = os.path.relpath(os.path.dirname(new_path), DEST_DIR).replace("\\", "/")
    if rel == "." or rel.startswith(".."):
        return None
    if rel.split("/")[0].startswith("_"):
        return None
    return rel


def find_manual_moves(db_paths):
    """Cached files that no longer exist at their cached path but turn up exactly once elsewhere in Sorted/.
    Returns ({old_path: new_path}, number_of_ambiguous_files). The script updates the cache whenever it
    moves a file itself, so these are files you moved by hand (or a relocated library)."""
    missing = [p for p in db_paths if not os.path.exists(p)]
    if not missing:
        return {}, 0
    wanted = {os.path.basename(p) for p in missing}
    found = defaultdict(list)
    for q in list_images(DEST_DIR):
        b = os.path.basename(q)
        if b in wanted:
            found[b].append(q)
    moves, ambiguous = {}, 0
    for p in missing:
        hits = found.get(os.path.basename(p), [])
        if len(hits) == 1:
            moves[p] = hits[0]
        elif len(hits) > 1:
            ambiguous += 1
    return moves, ambiguous


def apply_manual_moves(con, moves, labels, persist):
    """Turn moves into labels (mutates `labels`). With persist=True also repoints the score cache and
    saves data/manual_labels.csv; with persist=False it is a preview only."""
    stats, per_label = Counter(), Counter()
    for old, new in moves.items():
        name = os.path.basename(new)
        rel_new = os.path.relpath(new, DEST_DIR).replace("\\", "/").lower()
        if rel_under_sorted(old) == rel_new:      # same place under Sorted/: the library was just relocated
            stats["relocated"] += 1
        else:
            label = label_for(new)
            if label:
                labels[name] = label
                stats["labeled"] += 1
                per_label[label] += 1
            elif name in labels:
                del labels[name]
                stats["unlabeled"] += 1
            else:
                stats["automatic"] += 1           # moved into an automatic folder: nothing to learn
        if persist:
            if con.execute("SELECT 1 FROM scores WHERE path=?", (new,)).fetchone():
                con.execute("DELETE FROM scores WHERE path=?", (old,))
            else:
                con.execute("UPDATE scores SET path=? WHERE path=?", (new, old))
    if persist:
        con.commit()
        save_labels(labels)
    return stats, per_label


def load_rows_with_labels(con, persist, detect=True):
    """Cached (path, top) rows for files that exist, plus manual labels. Detects files moved by hand first;
    persist=False previews that (dry runs) without writing anything."""
    all_rows = [(p, json.loads(t)) for p, t in con.execute("SELECT path, top FROM scores")]
    set_known_tags(all_rows)
    labels = load_labels()
    if detect:
        moves, ambiguous = find_manual_moves([p for p, _ in all_rows])
        if moves:
            stats, per_label = apply_manual_moves(con, moves, labels, persist)
            all_rows = [(moves.get(p, p), t) for p, t in all_rows]
            parts = []
            if stats["labeled"]:
                parts.append(f"{stats['labeled']} sorted by hand into {len(per_label)} folders")
            if stats["unlabeled"]:
                parts.append(f"{stats['unlabeled']} moved back to automatic folders (label removed)")
            if stats["automatic"]:
                parts.append(f"{stats['automatic']} moved between automatic folders")
            if stats["relocated"]:
                parts.append(f"{stats['relocated']} relocated with the library (no label)")
            print("Detected moved files: " + ", ".join(parts)
                  + ("" if persist else " [preview: saved by the next real sort or `learn`]"))
            if per_label:
                print("  Top manual folders: " + ", ".join(f"{l} {n}" for l, n in per_label.most_common(5)))
        if ambiguous:
            print(f"  [!] {ambiguous} moved files have duplicate filenames elsewhere in Sorted/ and were skipped.")
    rows = [(p, t) for p, t in all_rows if os.path.exists(p)]
    return rows, labels


def cmd_learn():
    """Learn from files moved by hand: label them by their folder, repoint the score cache, save the labels."""
    con = db_connect()
    rows, labels = load_rows_with_labels(con, persist=True)
    present = sum(1 for p, _ in rows if os.path.basename(p) in labels)
    print(f"Manual labels: {len(labels)} saved ({present} on images currently in the library) -> {LABELS_FILE}")
    for label, n in Counter(labels.values()).most_common(10):
        print(f"  {n:5d}  {label}")
    print("Labeled images stay in their folder on every sort, and count as evidence for author hints, "
          "same-tweet siblings, hashtag suggestions and folder sizes.")


def cmd_sort(dry_run, only_under=None):
    con = db_connect()
    rows, labels = load_rows_with_labels(con, persist=not dry_run, detect=AUTO_LEARN)
    series_scores = load_series_scores(con) if SERIES_ON else {}
    if not rows:
        print("Nothing to sort. Run `scan` first.")
        return

    base = [(p, *decide(t)) for p, t in rows]
    pinned_n = sum(1 for p, _, _ in base if os.path.basename(p) in labels)
    if pinned_n:
        print(f"Manual labels: {pinned_n} images stay in the folders you put them in.")

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
        label = labels.get(os.path.basename(p))
        if kind in ("char", "review"):
            canon.setdefault(norm_key(folder_name(raw)), folder_name(raw))
        if label:
            canon.setdefault(norm_key(label), label)
        m = TWEET_ID_RE.search(os.path.basename(p))
        if label and m:
            tweet_chars[m.group(1)].add(label)       # your label beats the tagger's guess
        elif kind == "char" and m:
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
        label = labels.get(os.path.basename(p))
        if kind == "char" or label:
            m = TWEET_ID_RE.search(os.path.basename(p))
            a = authors.get(m.group(1)) if m else None
            if a:
                author_votes[a][label or folder_name(raw)].add(m.group(1))
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
        pinned = labels.get(os.path.basename(p))
        if pinned:
            plan_items.append((p, kind, raw, pinned, "manual"))
            continue
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
            if not forced and len(char_f) < 2 and kind in ("review", "unsorted") and series_scores:
                guess = series_guess(series_scores.get(os.path.basename(p)))
                if guess:
                    forced, src = "_Series/" + guess, "copyright"
        plan_items.append((p, kind, raw, forced, src))

    if series_scores:
        # keep a copyright-based series folder only if it is big enough, already exists, or hashtags already use it
        wanted = Counter(f for _, _, _, f, sr in plan_items if sr == "copyright")
        known = {f for _, _, _, f, sr in plan_items if sr == "series"}
        keep = {f for f, n in wanted.items()
                if n >= SERIES_FOLDER_MIN or f in known or os.path.isdir(os.path.join(DEST_DIR, to_path(f)))}
        plan_items = [(p, k, r, None, "") if (sr == "copyright" and f not in keep) else (p, k, r, f, sr)
                      for p, k, r, f, sr in plan_items]
        if wanted:
            print(f"Series from copyright tags: {sum(n for f, n in wanted.items() if f in keep)} images into "
                  f"{len(keep)} series folders ({len(wanted) - len(keep)} small ones skipped)")

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
    manual_folders = set(labels.values())   # folders you created by hand are never demoted to _Misc
    for p, kind, raw, forced, src in plan_items:
        if forced:
            if src == "manual" or forced in manual_folders:
                folder = to_path(forced)
            elif forced.startswith("_"):
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
        f, preview_name = open_preview_file()
        with f:
            w = csv.writer(f)
            w.writerow(["file", "folder", "source"])
            w.writerows(plan)
        print(f"\nDry run only. Full plan written to {preview_name}. Nothing moved.")
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
    rows, labels = load_rows_with_labels(con, persist=False, detect=AUTO_LEARN)
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
        label = labels.get(os.path.basename(p))
        m = TWEET_ID_RE.search(os.path.basename(p))
        if label and m:
            tweet_chars[m.group(1)].add(label)       # your label beats the tagger's guess
        elif kind == "char" and m:
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


def open_preview_file():
    """Open sort_preview.csv for writing. If another program (usually Excel) has it open, Windows refuses,
    so fall back to sort_preview_1.csv, sort_preview_2.csv, ..."""
    for i in range(20):
        name = "sort_preview.csv" if i == 0 else f"sort_preview_{i}.csv"
        try:
            f = open(name, "w", newline="", encoding="utf-8")
        except PermissionError:
            continue
        if i:
            print(f"[!] sort_preview.csv is open in another program (Excel?), so the plan was written to {name}.")
        return f, name
    raise PermissionError("Could not write sort_preview.csv. Close it in Excel (or any other program) and run again.")


def latest_preview_file():
    """The most recently written sort_preview*.csv, or None."""
    import glob
    files = glob.glob("sort_preview*.csv")
    return max(files, key=os.path.getmtime) if files else None


def cmd_report(top=30):
    """Use the last `sort --dry-run` plan to show what the still-unsorted images have in common."""
    preview = latest_preview_file()
    if not preview:
        print("Run `python char_sorter.py sort --dry-run` first (it writes sort_preview.csv).")
        return
    data = load_tag_data()
    hmap = load_hashtag_map()
    n = no_data = 0
    tag_counts, author_counts = Counter(), Counter()
    with open(preview, newline="", encoding="utf-8") as f:
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


def cmd_multiples(top=40):
    """Read-only: which characters the tagger sees together in the images that end up in _Multiple."""
    con = db_connect()
    rows, labels = load_rows_with_labels(con, persist=False, detect=AUTO_LEARN)
    pairs, sizes, total = Counter(), Counter(), 0
    for p, t in rows:
        if os.path.basename(p) in labels:
            continue
        if decide(t)[0] != "multiple":
            continue
        present = [x for x in merge_variants(t) if x[1] >= CONFIDENT]
        total += 1
        sizes[min(len(present), 4)] += 1
        pairs[tuple(sorted((present[0][0], present[1][0])))] += 1
    if not total:
        print("No multiple-character images with the current settings.")
        return
    print(f"{total} images would go to {FOLDER_MULTIPLE} (characters above {CONFIDENT} with no clear leader):")
    print(f"  exactly 2 characters: {sizes[2]}, 3: {sizes[3]}, 4 or more: {sizes[4]}")
    print(f"\nTop {top} pairs (the two highest-scoring characters in each image):")
    for (a, b), n in pairs.most_common(top):
        print(f"{n:6d}  {a}  +  {b}")


def hydrus_tag(namespace, name):
    """Danbooru-style Hydrus tag: underscores become spaces, lowercase ('character:hu tao (genshin impact)')."""
    return f"{namespace}:{str(name).replace('_', ' ').strip().lower()}"


def series_tag(name):
    """series:<name> without a trailing '(series)' (honkai_(series) -> series:honkai)."""
    return re.sub(r"\s*\(series\)$", "", hydrus_tag("series", name))


def series_names(guesses):
    """All confident copyright (series) tags for one image, without sub-series and ignored tags."""
    cands = [t for t, sc in (guesses or []) if sc >= SERIES_MIN_SCORE and t.lower() not in SERIES_IGNORE]
    return [t for t in cands if not any(t != o and t.startswith(o + "_") for o in cands)]


def cmd_export_hydrus(stage_dir=None, limit=0, guesses=True, clean=False):
    """Write Hydrus sidecar files (<image>.txt, one tag per line) from everything the tool knows:
    characters (variants merged, aliases applied, several per image allowed), series, artist, hashtags,
    your manual labels, and optionally weak guesses under 'guess:'. Hydrus imports them with the images."""
    import random
    con = db_connect()
    rows, labels = load_rows_with_labels(con, persist=False, detect=AUTO_LEARN)
    if not rows:
        print("Nothing scanned yet. Run `scan` first.")
        return
    if clean:
        removed = 0
        for p, _ in rows:
            if os.path.exists(p + ".txt"):
                os.remove(p + ".txt")
                removed += 1
        print(f"Removed {removed} sidecar files.")
        return

    series_scores = load_series_scores(con)
    ttags = load_tweet_tags()
    authors = load_tweet_authors()
    rows = sorted(rows, key=lambda r: r[0])
    if limit and limit < len(rows):
        rows = random.Random(0).sample(rows, limit)   # fixed seed: the same test batch every time

    written = skipped = 0
    counts = Counter()
    for p, top in rows:
        name = os.path.basename(p)
        m = TWEET_ID_RE.search(name)
        tid = m.group(1) if m else None
        tags = set()
        for tag, sc in merge_variants(top):
            if sc >= CONFIDENT:
                tags.add(hydrus_tag("character", tag))
                series = split_tag(tag)[1]
                if series:
                    tags.add(series_tag(series))
            elif guesses and sc >= REVIEW:
                tags.add(hydrus_tag("guess", tag))
        if name in labels:
            tags.add(hydrus_tag("character", labels[name].replace("\\", "/").split("/")[-1]))
        for t in series_names(series_scores.get(name)):
            tags.add(series_tag(t))
        if tid:
            if authors.get(tid):
                tags.add(f"creator:{authors[tid]}")
            for h in ttags.get(tid, ()):
                tags.add(f"hashtag:{h}")
        if not tags:
            skipped += 1
            continue
        target = p
        if stage_dir:
            os.makedirs(stage_dir, exist_ok=True)
            target = os.path.join(stage_dir, name)
            if not os.path.exists(target):
                shutil.copy2(p, target)
        with open(target + ".txt", "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(sorted(tags)) + "\n")
        written += 1
        for t in tags:
            counts[t.split(":", 1)[0]] += 1

    where = f"in {stage_dir} (copies of the images)" if stage_dir else "next to the images"
    print(f"Wrote {written} sidecar files {where}; {skipped} images had no tags at all.")
    print("Tags by namespace: " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
    print("Sidecars are named <image>.txt (for example 123_1.jpg.txt): newline-separated tags, "
          "which is what Hydrus's default sidecar importer expects.")


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
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["scan", "sort", "undo", "bench", "inbox", "intake", "tags", "rebuild-tags", "report", "suggest-map", "learn", "multiples", "export-hydrus"])
    ap.add_argument("paths", nargs="*", help="rebuild-tags: folders of .json sidecars or an old tweet_hashtags.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=50, help="tags: how many hashtags to show")
    ap.add_argument("--all", action="store_true", help="tags: include hashtags already mapped")
    ap.add_argument("--min-tweets", type=int, default=5, help="suggest-map: minimum tweets per hashtag")
    ap.add_argument("--min-share", type=float, default=0.8, help="suggest-map: required share for the top character")
    ap.add_argument("--apply", action="store_true", help="suggest-map: append the suggestions to hashtag_map.csv")
    ap.add_argument("--limit", type=int, default=0, help="export-hydrus: only a random sample of this many images")
    ap.add_argument("--no-guesses", action="store_true", help="export-hydrus: leave out weak 'guess:' tags")
    ap.add_argument("--clean", action="store_true", help="export-hydrus: delete the sidecar files again")
    a = ap.parse_args()
    if a.command == "scan":
        cmd_scan()
    elif a.command == "bench":
        cmd_bench()
    elif a.command == "inbox":
        cmd_inbox(a.dry_run)
    elif a.command == "intake":
        cmd_intake()
    elif a.command == "learn":
        cmd_learn()
    elif a.command == "suggest-map":
        cmd_suggest_map(a.min_tweets, a.min_share, a.apply)
    elif a.command == "report":
        cmd_report(a.top)
    elif a.command == "multiples":
        cmd_multiples(a.top)
    elif a.command == "export-hydrus":
        cmd_export_hydrus(a.paths[0] if a.paths else None, a.limit, not a.no_guesses, a.clean)
    elif a.command == "tags":
        cmd_tags(a.top, a.all)
    elif a.command == "rebuild-tags":
        cmd_rebuild_tags(a.paths)
    elif a.command == "sort":
        cmd_sort(a.dry_run)
    else:
        cmd_undo()