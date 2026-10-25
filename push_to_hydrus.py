#!/usr/bin/env python3
"""
push_to_hydrus.py - send your tagged X likes to Hydrus Network through its Client API.
For every image in the Camie score cache that still exists on disk, it:
   imports the file into Hydrus (add_file, by path - so Hydrus must run on this PC),
   adds tags to ONE local tag service (default name "camie"), so they stay apart from your own tags:
        character:<name>            Camie character score >= character_threshold
        possible character:<name>   score between possible_threshold and character_threshold
        series:<name>               Camie copyright score >= series_threshold
        creator:<handle>            tweet author          (from the gallery-dl sidecar)
        hashtag:<tag>               tweet hashtags        (from the sidecar)
        x flag:sensitive            if X flagged the tweet as sensitive
   adds the tweet URL as a known URL,
   stores the tweet text as a note,
   sets the tweet's post date as the file's "modified time" for the domain x.com.
Safe to re-run: tag, URL and note calls are idempotent, and files whose data has not changed since the last
push are skipped (data/hydrus_pushed.db). Changing a threshold later only ADDS tags - it never removes the old ones.
Setup:  copy config/hydrus.example.json to config/hydrus.json and fill in access_key
        (or set the HYDRUS_ACCESS_KEY environment variable). Add config/hydrus.json to .gitignore.
        In Hydrus: services > manage services > add a Client API service, create a local tag service called
        "camie", then services > review services > Client API > add an access key with these permissions:
        Import and Edit URLs, Import and Delete Files, Edit File Tags, Edit File Notes, Edit File Times.
Usage:
    python push_to_hydrus.py --check                 # test connection, key and tag service; sends nothing
    python push_to_hydrus.py --dry-run --limit 5     # show what would be sent; never contacts Hydrus
    python push_to_hydrus.py --limit 50              # try a small batch first
    python push_to_hydrus.py                         # everything not yet pushed
    python push_to_hydrus.py --force                 # re-send even files that look unchanged
    python push_to_hydrus.py --folder "D:/X_Likes/Incoming/Inbox"   # only images under a folder
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
import requests
import char_sorter as cs
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass
HYDRUS_CONFIG_FILE = os.path.join(cs.CONFIG_DIR, "hydrus.json")
PUSHED_DB = os.path.join(cs.DATA_DIR, "hydrus_pushed.db")
SIDECAR_CACHE_DB = os.path.join(cs.DATA_DIR, "hydrus_sidecars.db")
DEFAULTS = {
    "api_url": "http://127.0.0.1:45869",
    "access_key": "",                 # or the HYDRUS_ACCESS_KEY environment variable
    "tag_service_name": "camie",      # a LOCAL tag service in Hydrus
    "tag_service_key": "",            # optional: overrides the name lookup
    "character_threshold": 0.5,
    "possible_threshold": 0.3,
    "series_threshold": 0.5,
    "add_creator": True,
    "add_hashtags": True,
    "hashtag_namespace": "hashtag",
    "max_hashtags": 30,
    "sensitive_tag": "x flag:sensitive",
    "add_url": True,
    "add_note": True,
    "note_name": "tweet",
    "set_post_date": True,
    "date_domain": "x.com",
    "delete_after_import": False,     # True deletes the file from disk once Hydrus has it. Leave off until you trust it.
    "max_consecutive_errors": 10,
    "max_workers":24,  # conservative concurrent Hydrus API workers
}
class Fatal(Exception):
    pass
# ------------------------------------------------------------------------------
# config
# ------------------------------------------------------------------------------
def load_config():
    c = dict(DEFAULTS)
    if os.path.exists(HYDRUS_CONFIG_FILE):
        try:
            with open(HYDRUS_CONFIG_FILE, encoding="utf-8-sig") as f:
                user = json.load(f)
        except Exception as e:
            raise SystemExit(f"Could not read {HYDRUS_CONFIG_FILE}: {e}")
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            print(f"[!] Unknown keys in {HYDRUS_CONFIG_FILE} ignored (typo?): {', '.join(unknown)}")
        c.update(user)
    env = os.environ.get("HYDRUS_ACCESS_KEY", "").strip()
    if env:
        c["access_key"] = env
    c["api_url"] = str(c["api_url"]).rstrip("/")
    return c
# ------------------------------------------------------------------------------
# tweet sidecars (gallery-dl .json files)
# ------------------------------------------------------------------------------
def parse_date(v):
    """gallery-dl dates are UTC. Returns epoch seconds or None."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%a %b %d %H:%M:%S %z %Y"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                pass
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
def parse_sidecar(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    tid = str(d.get("tweet_id") or d.get("id") or "").strip()
    if not tid.isdigit():
        return None
    content = str(d.get("content") or d.get("text") or "").strip()
    raw = d.get("hashtags") or re.findall(r"#(\w+)", content)
    hashtags = [str(h).strip().lstrip("#") for h in raw if str(h).strip().lstrip("#")]
    a = d.get("author")
    # only the account handle: a display name (nick) can contain spaces and is no use in a URL
    author = (a.get("name") or a.get("screen_name") or "") if isinstance(a, dict) else (str(a) if a else "")
    return {
        "tweet_id": tid,
        "author": author.strip().lstrip("@"),
        "hashtags": hashtags,
        "content": content,
        "ts": parse_date(d.get("date")),
        "sensitive": bool(d.get("sensitive")) if d.get("sensitive") is not None else None,
    }
def index_sidecars(folders):
    """Index gallery-dl JSON sidecars, reusing parsed records when files are unchanged."""
    os.makedirs(os.path.dirname(SIDECAR_CACHE_DB), exist_ok=True)
    cache = sqlite3.connect(SIDECAR_CACHE_DB)
    cache.execute("""CREATE TABLE IF NOT EXISTS sidecars (
        path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL,
        tweet_id TEXT NOT NULL, record_json TEXT NOT NULL
    )""")
    cached = {row[0]: (row[1], row[2], row[3], row[4])
              for row in cache.execute("SELECT path, size, mtime, tweet_id, record_json FROM sidecars")}
    out = {}
    seen_paths = set()
    n = 0
    changed_rows = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for root, _, files in os.walk(folder):
            for filename in files:
                if not filename.lower().endswith(".json"):
                    continue
                path = os.path.abspath(os.path.join(root, filename))
                seen_paths.add(path)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                old = cached.get(path)
                if old and old[0] == st.st_size and old[1] == st.st_mtime:
                    tid, rec_json = old[2], old[3]
                    try:
                        rec = json.loads(rec_json)
                    except (TypeError, json.JSONDecodeError):
                        rec = None
                else:
                    rec = parse_sidecar(path)
                    if rec:
                        tid = rec["tweet_id"]
                        rec_json = json.dumps(rec, ensure_ascii=False)
                        changed_rows.append((path, st.st_size, st.st_mtime, tid, rec_json))
                    else:
                        # Cache invalid JSON too, so it is not repeatedly opened on every run.
                        tid = ""
                        rec_json = "null"
                        changed_rows.append((path, st.st_size, st.st_mtime, tid, rec_json))
                if not rec:
                    continue
                n += 1
                existing = out.get(rec["tweet_id"])
                if existing is None:
                    out[rec["tweet_id"]] = rec
                else:
                    for h in rec["hashtags"]:
                        if h not in existing["hashtags"]:
                            existing["hashtags"].append(h)
                    for key in ("author", "content", "ts", "sensitive"):
                        if existing[key] in (None, ""):
                            existing[key] = rec[key]
    if changed_rows:
        cache.executemany("INSERT OR REPLACE INTO sidecars VALUES (?, ?, ?, ?, ?)", changed_rows)
    # Remove entries for sidecars that no longer exist, preventing unbounded stale cache growth.
    stale = set(cached) - seen_paths
    if stale:
        cache.executemany("DELETE FROM sidecars WHERE path = ?", ((path,) for path in stale))
    cache.commit()
    cache.close()
    return out, n

# ------------------------------------------------------------------------------
# turning cache rows + sidecars into what gets sent
# ------------------------------------------------------------------------------
def clean(s):
    return re.sub(r"\s+", " ", str(s).replace("_", " ")).strip().lower()
def build_item(path, top, series, rec, c):
    base = os.path.basename(path)
    m = cs.TWEET_ID_RE.search(base)
    tid = m.group(1) if m else (rec["tweet_id"] if rec else None)
    tags = set()
    for name, score in top or []:
        if score >= c["character_threshold"]:
            tags.add("character:" + clean(name))
        elif score >= c["possible_threshold"]:
            tags.add("possible character:" + clean(name))
    for name, score in series or []:
        if score >= c["series_threshold"]:
            tags.add("series:" + clean(name))
    if rec:
        if c["add_creator"] and rec["author"]:
            tags.add("creator:" + rec["author"].lower())   # the handle exactly: underscores are part of it
        if c["add_hashtags"]:
            for h in rec["hashtags"][: int(c["max_hashtags"])]:
                tags.add(f"{c['hashtag_namespace']}:{clean(h)}")
        if rec["sensitive"] and c["sensitive_tag"]:
            tags.add(c["sensitive_tag"])
    url = None
    if tid and c["add_url"]:
        who = rec["author"] if rec and rec["author"] else "i"
        url = f"https://x.com/{who}/status/{tid}"
    note = None
    ts = rec["ts"] if rec else None
    if rec and c["add_note"] and (rec["content"] or rec["author"]):
        when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
        head = " | ".join(x for x in (f"@{rec['author']}" if rec["author"] else "", when, url or "") if x)
        note = (head + "\n\n" + rec["content"]).strip()
    item = {
        "path": path,
        "name": base,
        "tags": sorted(tags),
        "url": url,
        "note": note,
        "ts": ts if c["set_post_date"] else None,
    }
    item["sig"] = hashlib.sha1(
        json.dumps([item["tags"], url, note, item["ts"], c["note_name"], c["date_domain"]],
                   ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return item
def load_items(c, folder=None):
    con = cs.db_connect()
    series = {}
    try:
        for name, top in con.execute("SELECT name, top FROM series_scores"):
            series[name] = json.loads(top or "[]")
    except sqlite3.Error:
        pass
    rows = sorted(con.execute("SELECT path, top FROM scores"))
    con.close()
    root = None
    if folder:
        root = os.path.normcase(os.path.abspath(folder)) + os.sep
    sidecars, n_sidecars = index_sidecars([cs.META_DIR, cs.INBOX_DIR])
    items, missing = [], 0
    for path, top_json in rows:
        if root and not os.path.normcase(os.path.abspath(path)).startswith(root):
            continue
        if not os.path.exists(path):
            missing += 1
            continue
        top = json.loads(top_json or "[]")
        m = cs.TWEET_ID_RE.search(os.path.basename(path))
        rec = sidecars.get(m.group(1)) if m else None
        items.append(build_item(path, top, series.get(os.path.basename(path)), rec, c))
    return items, {"sidecars": n_sidecars, "tweets": len(sidecars), "missing": missing}
# ------------------------------------------------------------------------------
# Hydrus Client API
# ------------------------------------------------------------------------------
class Hydrus:
    def __init__(self, base, key):
        self.base = base
        self.s = requests.Session()
        self.s.headers["Hydrus-Client-API-Access-Key"] = key
    def request(self, method, path, **kw):
        try:
            r = self.s.request(method, self.base + path, timeout=300, **kw)
        except requests.RequestException as e:
            raise Fatal(f"Could not reach Hydrus at {self.base} ({type(e).__name__}). Is the client open, and is "
                        f"the Client API service enabled (services > manage services)?") from e
        if r.status_code in (401, 419):
            raise Fatal("Hydrus rejected the access key (HTTP %d). Check access_key." % r.status_code)
        return r
def resolve_service_key(h, c):
    if c["tag_service_key"]:
        return c["tag_service_key"]
    r = h.request("GET", "/get_service", params={"service_name": c["tag_service_name"]})
    if r.status_code == 404:
        raise Fatal(f'Hydrus has no tag service named "{c["tag_service_name"]}". Create a local tag service with '
                    f"that name (services > manage services), or set tag_service_key.")
    if r.status_code == 403:
        raise Fatal("The access key may not look up services (needs Edit File Tags or Import permission).")
    r.raise_for_status()
    svc = r.json().get("service") or {}
    key = svc.get("service_key")
    if not key:
        raise Fatal(f"Unexpected /get_service reply: {r.text[:200]}")
    if svc.get("type") not in (5, None):
        raise Fatal(f'"{c["tag_service_name"]}" is a {svc.get("type_pretty", svc.get("type"))}; '
                    f"it must be a local tag service (type 5).")
    return key
def check(h, c):
    r = h.request("GET", "/verify_access_key")
    if r.status_code >= 400:
        raise Fatal(f"/verify_access_key failed: HTTP {r.status_code} {r.text[:200]}")
    j = r.json()
    print(f"Connected. Key: {j.get('human_description', j)}")
    key = resolve_service_key(h, c)
    print(f'Tag service "{c["tag_service_name"]}" found ({key}).')
    return key
def optional_call(h, feats, feature, label, path, body):
    """A call whose failure should not stop the run. A 403 or 400 switches that feature off, with one warning."""
    if not feats[feature]:
        return False
    r = h.request("POST", path, json=body)
    if r.status_code == 403:
        feats[feature] = False
        print(f"[!] The access key lacks the permission for {label}; skipping it. Fix it in Hydrus "
              f"and re-run (files pushed without it are not marked as done).")
        return False
    if r.status_code in (400, 404, 405):
        feats[feature] = False
        print(f"[!] Hydrus refused {path} ({r.status_code}: {r.text[:160]}). Skipping {label} from now on; "
              f"this Hydrus version may not support it.")
        return False
    if r.status_code >= 400:
        return False
    return True
def push_item(h, service_key, it, c, feats):
    """Returns (outcome, complete). Raises Fatal for problems that make every file fail."""
    body = {"path": os.path.abspath(it["path"])}
    if c["delete_after_import"]:
        body["delete_after_successful_import"] = True
    r = h.request("POST", "/add_files/add_file", json=body)
    if r.status_code == 403:
        raise Fatal("The access key lacks 'Import and Delete Files'.")
    if r.status_code >= 400:
        return "failed", False
    j = r.json()
    status = j.get("status")
    if status == 3:
        return "previously_deleted", False   # you deleted it from Hydrus before; respect that
    if status not in (1, 2):
        return ("vetoed" if status == 7 else "failed"), False
    sha = j.get("hash")
    if not sha:
        return "failed", False
    complete = True
    if it["tags"]:
        r = h.request("POST", "/add_tags/add_tags",
                      json={"hash": sha, "service_keys_to_tags": {service_key: it["tags"]}})
        if r.status_code == 403:
            raise Fatal("The access key lacks 'Edit File Tags'.")
        if r.status_code >= 400:
            return "failed", False
    if it["url"] and c["add_url"]:
        ok = optional_call(h, feats, "url", "'Import and Edit URLs'", "/add_urls/associate_url",
                           {"hash": sha, "url_to_add": it["url"]})
        complete = complete and ok
    if it["note"] and c["add_note"]:
        ok = optional_call(h, feats, "note", "'Edit File Notes'", "/add_notes/set_notes",
                           {"hash": sha, "notes": {c["note_name"]: it["note"]}})
        complete = complete and ok
    if it["ts"] is not None and c["set_post_date"]:
        ok = optional_call(h, feats, "time", "'Edit File Times'", "/edit_times/set_time",
                           {"hash": sha, "timestamp": it["ts"], "timestamp_type": 0, "domain": c["date_domain"]})
        complete = complete and ok
    return ("imported" if status == 1 else "already_in_hydrus"), complete
# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------
def pushed_db():
    con = sqlite3.connect(PUSHED_DB)
    con.execute("CREATE TABLE IF NOT EXISTS pushed (name TEXT PRIMARY KEY, sig TEXT)")
    return con
def main():
    ap = argparse.ArgumentParser(description="Push tagged X likes to Hydrus Network.")
    ap.add_argument("--check", action="store_true", help="test connection, key and tag service; send nothing")
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent; never contacts Hydrus")
    ap.add_argument("--limit", type=int, default=0, help="push at most N files that need pushing")
    ap.add_argument("--force", action="store_true", help="re-send files even if unchanged since the last push")
    ap.add_argument("--folder", help="only images under this folder")
    ap.add_argument("--show", type=int, default=3, help="dry-run: how many example payloads to print")
    a = ap.parse_args()
    c = load_config()
    if not a.dry_run and not c["access_key"]:
        raise SystemExit(f"No access key. Set access_key in {HYDRUS_CONFIG_FILE} or the HYDRUS_ACCESS_KEY "
                         f"environment variable (see the top of this script).")
    h = Hydrus(c["api_url"], c["access_key"])
    try:
        service_key = None
        if a.check:
            check(h, c)
            return
        if not a.dry_run:
            service_key = check(h, c)
        items, info = load_items(c, a.folder)
        print(f"{len(items)} images in the cache exist on disk ({info['missing']} cached files are gone). "
              f"Sidecars: {info['sidecars']} files covering {info['tweets']} tweets.")
        con = pushed_db()
        done = dict(con.execute("SELECT name, sig FROM pushed"))
        todo = [it for it in items if a.force or done.get(it["name"]) != it["sig"]]
        print(f"{len(items) - len(todo)} unchanged since the last push (skipped), {len(todo)} to push.")
        if a.limit:
            todo = todo[:a.limit]
            print(f"--limit: pushing {len(todo)}.")
        if not todo:
            return
        if a.dry_run:
            stats = Counter()
            for it in todo:
                stats["with character tag"] += any(t.startswith("character:") for t in it["tags"])
                stats["with possible character"] += any(t.startswith("possible character:") for t in it["tags"])
                stats["with series"] += any(t.startswith("series:") for t in it["tags"])
                stats["with creator"] += any(t.startswith("creator:") for t in it["tags"])
                stats["with note"] += bool(it["note"])
                stats["with post date"] += it["ts"] is not None
                stats["no tags at all"] += not it["tags"]
            print("Of the files that would be pushed:")
            for k, v in stats.items():
                print(f"  {v:6d}  {k}")
            for it in todo[:a.show]:
                print("\n" + it["path"])
                print("  tags:", ", ".join(it["tags"]) or "(none)")
                print("  url :", it["url"])
                print("  time:", it["ts"])
                print("  note:", (it["note"] or "").replace("\n", " / ")[:200])
            print("\nDry run only. Nothing was sent.")
            return
        feats = {"url": True, "note": True, "time": True}
        counts = Counter()
        errors_in_a_row = 0
        start = time.time()
        workers = max(1, min(24, int(c.get("max_workers", 2))))
        thread_state = threading.local()

        def process_one(it):
            # requests.Session is kept per worker thread rather than shared between threads.
            if not hasattr(thread_state, "hydrus"):
                thread_state.hydrus = Hydrus(c["api_url"], c["access_key"])
            return push_item(thread_state.hydrus, service_key, it, c, feats)

        completed = 0
        # Submit only a small batch at a time so the script can stop cleanly after repeated failures.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for offset in range(0, len(todo), workers):
                batch = todo[offset:offset + workers]
                futures = {pool.submit(process_one, it): it for it in batch}
                for future in as_completed(futures):
                    it = futures[future]
                    outcome, complete = future.result()
                    completed += 1
                    counts[outcome] += 1
                    if outcome in ("imported", "already_in_hydrus"):
                        errors_in_a_row = 0
                        if complete:
                            con.execute("INSERT OR REPLACE INTO pushed VALUES (?,?)", (it["name"], it["sig"]))
                    elif outcome == "failed":
                        errors_in_a_row += 1
                        print(f"  failed: {it['name']}")
                        if errors_in_a_row >= int(c["max_consecutive_errors"]):
                            raise Fatal(f"{errors_in_a_row} failures in a row; stopping. Check Hydrus.")
                    if completed % 100 == 0 or completed == len(todo):
                        con.commit()
                        rate = completed / max(time.time() - start, 1e-6)
                        eta = (len(todo) - completed) / rate
                        print(f"{completed}/{len(todo)}  ({rate:.1f}/s, about {eta / 60:.0f} min left; {workers} workers)")
        con.commit()
        con.commit()
        print("\nDone: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.most_common()))
        if counts["previously_deleted"]:
            print("  'previously deleted' files were skipped because you deleted them in Hydrus earlier.")
    except Fatal as e:
        print(f"\n[!] {e}\nProgress so far is saved; fix the problem and run again.")
        sys.exit(1)
if __name__ == "__main__":
    main()
