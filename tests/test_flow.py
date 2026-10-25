"""
Tests for the intake -> push flow, against a fake Hydrus server. Nothing here touches your real config/, data/ or
library: the two scripts are copied into a temporary folder and everything runs there.

    venv\\Scripts\\activate
    python tests\\test_flow.py

Needs the same packages as the project (Pillow, numpy, pandas, onnxruntime, requests). No GPU, model or Hydrus
needed. This checks our own logic against Hydrus's documented API; it can't prove real Hydrus behaves the same.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="xls_test_")
for f in ("char_sorter.py", "push_to_hydrus.py", "export_parents.py"):
    shutil.copy(os.path.join(REPO, f), ROOT)
os.makedirs(os.path.join(ROOT, "config"))
os.chdir(ROOT)

KEY, SVC = "a" * 64, "b" * 64
calls = []
state = {"deny": set(), "badjson": set(), "seen": set(), "tags": {}, "deleted": {}}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        calls.append(("GET", u.path, q))
        if u.path in state["deny"]:
            return self._send(403, {})
        if u.path == "/verify_access_key":
            return self._send(200, {"human_description": "test key"})
        if u.path == "/get_service":
            return self._send(200, {"service": {"service_key": SVC, "type": 5}})
        if u.path == "/get_files/file_metadata":
            sha = json.loads(q["hashes"][0])[0]
            cur = sorted(state["tags"].get(sha, set()))
            return self._send(200, {"metadata": [{"hash": sha, "tags": {SVC: {"storage_tags": {"0": cur}}}}]})
        self._send(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urlparse(self.path).path
        calls.append(("POST", p, body))
        if p in state["deny"]:
            return self._send(403, {})
        if p == "/add_files/add_file":
            name = os.path.basename(body["path"])
            if name in state["badjson"]:
                return self._send(200, raw=b"<html>oops")
            st = 2 if name in state["seen"] else 1
            state["seen"].add(name)
            return self._send(200, {"status": st, "hash": hashlib.sha256(name.encode()).hexdigest()})
        if p == "/add_tags/add_tags":
            sha = body["hash"]
            cur, gone = state["tags"].setdefault(sha, set()), state["deleted"].setdefault(sha, set())
            adds = list(body.get("service_keys_to_tags", {}).get(SVC, []))
            dels = []
            acts = body.get("service_keys_to_actions_to_tags", {}).get(SVC)
            if acts:
                adds += acts.get("0", [])
                dels = acts.get("1", [])
            override = body.get("override_previously_deleted_mappings", True)
            for t in adds:
                if t in gone and not override:
                    continue
                cur.add(t)
                gone.discard(t)
            for t in dels:
                if t in cur or body.get("create_new_deleted_mappings", True):
                    gone.add(t)
                cur.discard(t)
            return self._send(200)
        self._send(200)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

json.dump({"tagger_backend": "camie", "parent_dir": "lib"}, open("config/config.json", "w"))
sys.path.insert(0, ROOT)
import char_sorter as cs  # noqa: E402

INBOX, META = cs.INBOX_DIR, cs.META_DIR
for d in (INBOX, META, "lib/Sorted/x"):
    os.makedirs(d, exist_ok=True)


def clear():
    for d in (INBOX, META):
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
    for f in os.listdir("data") if os.path.isdir("data") else []:
        if f.startswith("hydrus_"):
            os.remove(os.path.join("data", f))
    calls.clear()
    state.update(deny=set(), badjson=set(), seen=set(), tags={}, deleted={})


def cfg(**kw):
    c = {"api_url": f"http://127.0.0.1:{PORT}", "access_key": KEY}
    c.update(kw)
    json.dump(c, open("config/hydrus.json", "w"))


def run(*a):
    r = subprocess.run([sys.executable, "push_to_hydrus.py", *a], capture_output=True, text=True,
                       encoding="utf-8", cwd=ROOT)
    return r.returncode, r.stdout + r.stderr


def posts(path):
    return [b for m, p, b in calls if m == "POST" and p == path]


def sha(path):
    return hashlib.sha256(os.path.basename(path).encode()).hexdigest()


def make(path, score=None, series=None, image=False):
    if image or path.endswith((".jpg", ".png")):
        Image.new("RGB", (8, 8)).save(path)
    else:
        open(path, "wb").write(b"x")
    if score is not None:
        con = cs.db_connect()
        st = os.stat(path)
        con.execute("insert or replace into scores values (?,?,?,?)",
                    (os.path.abspath(path), st.st_size, st.st_mtime, json.dumps([["frieren", score]])))
        con.execute("insert or replace into series_scores values (?,?)",
                    (os.path.basename(path), json.dumps(series or [])))
        con.commit()
        con.close()


def sidecar(path, tid, who, tags=()):
    json.dump({"tweet_id": int(tid), "date": "2024-05-01 00:00:00", "author": {"name": who},
               "content": f"text #{tags[0] if tags else 'x'}", "hashtags": list(tags), "sensitive": False},
              open(os.path.join(META, os.path.basename(path) + ".json"), "w"))


def ok(msg):
    print("ok  " + msg)


# ---------------------------------------------------------------- videos, dry run, flags, delete
clear()
I = {k: f"18000000000000000{n:02d}" for n, k in enumerate(["img", "vid", "gif", "lib", "bad"], 1)}
P = {"img": f"{INBOX}/{I['img']}_1.jpg", "vid": f"{INBOX}/{I['vid']}_1.mp4", "gif": f"{INBOX}/{I['gif']}_1.gif",
     "lib": f"lib/Sorted/x/{I['lib']}_1.jpg", "bad": f"{INBOX}/{I['bad']}_1.mp4"}
for k, p in P.items():
    make(p, score=0.9 if k in ("img", "lib") else None)
    sidecar(p, I[k], f"artist_{k}", [f"tag{k}"])
open(f"{INBOX}/notes.txt", "w").write("ignore")

cfg()
code, out = run("--dry-run", "--show", "9")
assert code == 0 and not calls and "3 videos/GIFs" in out and "creator:artist_vid" in out and "notes.txt" not in out, out
ok("dry run counts videos, sends nothing")

code, out = run("--folder", INBOX)
assert code == 0, out
names = sorted(os.path.basename(b["path"]) for b in posts("/add_files/add_file"))
assert names == sorted(os.path.basename(P[k]) for k in ("img", "vid", "gif", "bad")), names
vt = state["tags"][sha(P["vid"])]
assert "creator:artist_vid" in vt and not any(t.startswith("character:") for t in vt)
assert all(os.path.exists(p) for p in P.values())
ok("videos and GIFs pushed with metadata only; nothing deleted by default")

calls.clear()
os.remove("data/hydrus_pushed.db")
cfg(push_videos=False)
code, out = run("--folder", INBOX)
assert [os.path.basename(b["path"]) for b in posts("/add_files/add_file")] == [os.path.basename(P["img"])]
ok("push_videos=false")

calls.clear()
os.remove("data/hydrus_pushed.db")
state["deny"] = {"/add_notes/set_notes"}
cfg(delete_after_import=True)
code, out = run()
assert code == 0 and all(os.path.exists(p) for p in P.values())
ok("delete_after_import keeps files when a step failed")

calls.clear()
state["deny"] = set()
state["badjson"] = {os.path.basename(P["bad"])}
code, out = run()
assert code == 0 and "error on" in out and "1 failed" in out, out
assert not any(os.path.exists(P[k]) for k in ("img", "vid", "gif"))
assert os.path.exists(P["lib"]) and os.path.exists(P["bad"])
assert all("delete_after_successful_import" not in b for b in posts("/add_files/add_file"))
ok("delete_after_import removes only completed files inside the Inbox; a bad reply doesn't kill the run")

# ---------------------------------------------------------------- resync and deleted tags
clear()
f = f"{INBOX}/1810000000000000001_1.jpg"
make(f, score=0.9)
sidecar(f, "1810000000000000001", "artist_r", ["oldtag"])
h = sha(f)
cfg()
assert run()[0] == 0
assert {"character:frieren", "creator:artist_r", "hashtag:oldtag"} <= state["tags"][h]
state["tags"][h].add("person:luke")                      # a tag the script doesn't own
os.remove(f"{META}/{os.path.basename(f)}.json")          # the hashtag disappears from the metadata
sidecar(f, "1810000000000000001", "artist_r", ["newtag"])
cfg(character_threshold=0.95, possible_threshold=0.3)   # character:frieren becomes only a guess

calls.clear()
code, out = run()
assert code == 0
assert {"character:frieren", "hashtag:oldtag", "possible character:frieren", "hashtag:newtag"} <= state["tags"][h]
ok("a plain re-push only adds tags")

calls.clear()
state["tags"][h] = {"character:frieren", "hashtag:oldtag", "person:luke", "creator:artist_r"}
os.remove("data/hydrus_pushed.db")
code, out = run("--resync")
assert code == 0 and "stale tags removed" in out, out
t = state["tags"][h]
assert "character:frieren" not in t and "hashtag:oldtag" not in t, t
assert {"possible character:frieren", "hashtag:newtag", "creator:artist_r", "person:luke"} <= t, t
assert not state["deleted"][h] - {"character:frieren", "hashtag:oldtag"}
ok("--resync removes stale owned tags, keeps wanted and foreign ones")

# stale tag left behind by an earlier plain push: the push DB already says "done", --resync must still clean it
clear()
make(f, score=0.9)
sidecar(f, "1810000000000000001", "artist_r", ["a"])
cfg()
run()
state["tags"][h].add("character:frieren (1st costume)")
code, out = run("--resync")
assert code == 0 and "1 stale tags removed" in out, out
assert "character:frieren (1st costume)" not in state["tags"][h] and "character:frieren" in state["tags"][h]
ok("--resync also checks files the push DB already marks as done")

clear()
make(f, score=0.9)
sidecar(f, "1810000000000000001", "artist_r", ["a"])
cfg()
run()
calls.clear()
os.remove("data/hydrus_pushed.db")
state["deny"] = {"/get_files/file_metadata"}
code, out = run("--resync")
assert code == 1 and "Search for and Fetch Files" in out, out
ok("--resync explains the missing permission")

clear()
make(f, score=0.9)
sidecar(f, "1810000000000000001", "artist_r", ["a"])
cfg()
run()
state["tags"][h].discard("character:frieren")
state["deleted"][h] = {"character:frieren"}              # you deleted it by hand in Hydrus
run("--force")
assert "character:frieren" not in state["tags"][h]
cfg(respect_deleted_tags=False)
run("--force")
assert "character:frieren" in state["tags"][h]
ok("respect_deleted_tags keeps hand-deleted tags deleted (and can be turned off)")

# ---------------------------------------------------------------- tag parents export
clear()
json.dump({"costume_terms": ["1st_costume", "swimsuit"]}, open("config/costume_terms.json", "w"))
cfg(character_threshold=0.5)
con = cs.db_connect()
for i, (name, score) in enumerate([("kazama_iroha_(1st_costume)", 0.9), ("shiroko_(swimsuit)_(blue_archive)", 0.8),
                                   ("kazama_iroha", 0.9), ("hu_tao_(genshin_impact)", 0.9),
                                   ("sakamata_chloe_(1st_costume)", 0.2)]):
    con.execute("insert or replace into scores values (?,?,?,?)", (f"/x/{i}.jpg", 1, 1.0, json.dumps([[name, score]])))
con.commit()
con.close()
r = subprocess.run([sys.executable, "export_parents.py"], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
assert r.returncode == 0, r.stdout + r.stderr
lines = open("data/tag_parents.txt", encoding="utf-8").read().split("\n")
assert lines == ["character:kazama iroha (1st costume)", "character:kazama iroha",
                 "character:shiroko (swimsuit) (blue archive)", "character:shiroko (blue archive)", ""], lines
ok("export_parents lists costume -> character pairs only (right direction, series kept, low scores skipped)")
con = cs.db_connect()
con.execute("delete from scores")
con.commit()
con.close()
os.remove("config/costume_terms.json")

# ---------------------------------------------------------------- intake
clear()
cs.cmd_scan = lambda *a, **k: None                       # no model here; test the logic around it


def intake():
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        cs.cmd_intake()
    return b.getvalue()


v = f"{INBOX}/1820000000000000001_1.mp4"
open(v, "wb").write(b"x")
json.dump({"tweet_id": 1820000000000000001, "date": "2024-01-01 00:00:00", "author": {"name": "a"},
           "content": "c", "hashtags": ["x"]}, open(v + ".json", "w"))
out = intake()
assert "sidecar" in out and "No new images to tag" in out
assert not os.path.exists(v + ".json") and os.path.exists(f"{META}/{os.path.basename(v)}.json")
ok("intake moves sidecars even when the Inbox only has videos")
Image.new("RGB", (4, 4)).save(f"{INBOX}/1820000000000000002_1.jpg")
out = intake()
assert "could not be read or tagged" in out and "1820000000000000002_1.jpg" in out
ok("intake reports images that were not tagged")
os.remove(f"{INBOX}/1820000000000000002_1.jpg")
assert "No new images" in intake()
ok("intake with nothing new")

print("\nAll tests passed.")
os.chdir(REPO)
shutil.rmtree(ROOT, ignore_errors=True)