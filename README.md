# X Likes & Bookmarks → Tagged → Hydrus

Downloads your liked and bookmarked media from X (Twitter) with `gallery-dl`, tags every image with the **Camie Tagger v2** anime tagger (characters and series), and pushes the images into [Hydrus Network](https://hydrusnetwork.github.io/hydrus/index.html) together with the tweet's author, hashtags, URL, text and post date. Nothing is sorted into folders: Hydrus is where you search, fix and browse.

---

## Key Features

- **One-click workflow:** `run_update.bat` fetches new likes and bookmarks, tags them and pushes them to Hydrus.
- **Character and series tags:** Camie Tagger v2 (ONNX, DirectML on AMD, Intel and Nvidia GPUs, CPU fallback) with variant folding, so costume tags like `shiroko_(swimsuit)_(blue_archive)` count as `shiroko_(blue_archive)`.
- **Tweet metadata, not just pixels:** author, hashtags, a "sensitive" flag, the tweet URL, the tweet text as a note, and the post date all go into Hydrus.
- **Separate tag service:** every automatic tag goes to its own local Hydrus tag service (`camie` by default), so it never mixes with tags you write yourself.
- **Safe to re-run:** files whose data hasn't changed are skipped, and files you deleted in Hydrus are never re-imported.
- **Fast re-tagging:** scores are cached in SQLite, and tag thresholds are applied at push time, so changing them needs no rescan.
- **Portable:** all settings live in `config/`, with no hardcoded drive letters or user names.

---

## How It Works

```text
gallery-dl  ->  Incoming/Inbox  ->  intake (tag)  ->  push_to_hydrus.py  ->  Hydrus
 likes +        images + .json      Camie scores       import + tags + URL     your library
 bookmarks      sidecars            cached in data/    + note + post date
```

1. **Fetch:** `gallery-dl` saves new images and a `.json` sidecar per image into `Incoming/Inbox`. A download archive (`data/archive.sqlite3`) stops anything being downloaded twice.
2. **Intake:** sidecars are read (hashtags and author are merged into `data/tweet_hashtags.csv`) and moved to `Meta/`. Every new image is scored by Camie and the scores are cached. Images stay where they are.
3. **Push:** each image is imported into Hydrus by path, then tagged, linked to its tweet, given a note and a post date.

Your library lives under `parent_dir`: `Incoming/Inbox` (new downloads) and `Meta/` (processed sidecars).

### Tags sent to Hydrus

| Tag | Source |
|---|---|
| `character:<name>` | Camie character score at or above `character_threshold` (default 0.5) |
| `possible character:<name>` | Score between `possible_threshold` (0.3) and `character_threshold` |
| `series:<name>` | Camie copyright score at or above `series_threshold` (0.5) |
| `creator:<handle>` | Tweet author, exactly as on X |
| `hashtag:<tag>` | Tweet hashtags (at most `max_hashtags`) |
| `x flag:sensitive` | Set when X flagged the tweet as sensitive |

No general tags (hair colour, clothing and so on) are sent. Names are Danbooru-style with underscores turned into spaces, for example `character:hu tao (genshin impact)`.

Besides tags, each image gets the tweet URL as a known URL, the tweet text as a note named `tweet` (with a header line of `@handle | date | url`), and the post date as its "modified time" for the domain `x.com`.

---

## Project Structure

```text
x-likes-sorter/
├── config/
│   ├── config.example.json   # Every option with its default (tracked in git)
│   ├── config.json           # Your settings (created by setup.bat, not tracked)
│   ├── hydrus.example.json   # Hydrus settings template (tracked in git)
│   ├── hydrus.json           # Your Hydrus settings and API key (NOT tracked)
│   └── hashtag_map.csv       # Only used by the legacy sorting commands
├── data/                     # Generated: score cache, tweet_hashtags.csv, download archive, push records
├── model/                    # Generated: auto-downloaded Camie model and metadata
├── .gitignore                # Keeps cookies, config.json, hydrus.json, data/ and model/ out of git
├── README.md
├── char_sorter.py            # Tagging and sidecar ingestion (plus the legacy sorting code)
├── push_to_hydrus.py         # Sends tagged images to Hydrus
├── requirements.txt
├── run_update.bat            # Menu and command runner
└── setup.bat                 # One-time installer
```

---

## Prerequisites

1. **Windows 10/11**
2. **Python 3.10+** added to `PATH`
3. **Hydrus Network** running on the same PC (images are imported by file path)
4. An **X/Twitter account**

---

## Quick Start (Fresh PC Setup)

1. **Get the project:** `git clone https://github.com/your-username/x-likes-sorter.git`, then `cd x-likes-sorter`.
2. **Run `setup.bat`.** It creates the virtual environment, installs the requirements, creates `config/`, `data/` and `model/`, and copies `config/config.example.json` to `config/config.json`.
3. **Export your X cookies** from a logged-in browser (for example with the *Get cookies.txt LOCALLY* extension) and save them as `cookies.txt` in the project folder.
4. **Edit `config/config.json`:** set `twitter_username` (without `@`), `parent_dir` (a full path with doubled backslashes such as `"D:\\X_Likes"`, or a plain name to keep the library next to the project) and `"tagger_backend": "camie"`.
5. **Set up Hydrus** (next section).
6. **Run `run_update.bat`** and pick option 1.

### Setting up Hydrus (once)

1. **Enable the Client API:** *services > manage services > add > client api*. Keep the default port `45869` unless you need another.
2. **Create a local tag service named `camie`:** *services > manage services > add > local tag service*. (Not *review services*, which only shows existing ones.)
3. **Create an access key:** *services > review services > client api > add > manually*. Give it these permissions:
   - Import and Edit URLs
   - Import and Delete Files
   - Edit File Tags
   - Edit File Notes
   - Edit File Times
4. **Copy `config/hydrus.example.json` to `config/hydrus.json`** and paste the 64-character key into `access_key`. You can set the `HYDRUS_ACCESS_KEY` environment variable instead.
5. **Test it:** run menu option 4. It prints the key's permissions and confirms the `camie` tag service.

**Storage tip:** keep Hydrus's database and thumbnails on your fastest drive and its media files wherever there's room (*database > migrate database*). Back up both the `db` folder and the media folder.

---

## Everyday Usage

Double-click **`run_update.bat`**:

```text
 NEW LIKES
  [1] Sync (Fetch Likes + Bookmarks + Tag + Push To Hydrus)
  [2] Tag + Push What Is Already In The Inbox
  [3] Push Preview (dry run, Hydrus not contacted)

 HYDRUS
  [4] Check Hydrus Connection (key, permissions, tag service)
  [5] Re-push Everything That Changed (after tag or threshold edits)

 TOOLS
  [6] Benchmark GPU / Batch Size

  [0] Exit
```

- **[1]** checks that Hydrus is reachable first (so you don't download for nothing), fetches Likes and Bookmarks (each can be turned off in the config), tags the new images and pushes them. **Open Hydrus before running it.**
- **[2]** does the tag and push steps for files already in the Inbox, for example after an interrupted run.
- **[3]** shows what would be sent (counts and a few example payloads) without contacting Hydrus.
- **[5]** re-pushes every image whose tags, URL, note or date changed since its last push, across the whole cache, not only the Inbox.

### Running commands without the menu

```cmd
run_update.bat sync       :: the full flow from option 1 (good for Task Scheduler)
run_update.bat push       :: tag + push the Inbox (option 2)
run_update.bat check      :: test the Hydrus connection (option 4)
run_update.bat bench      :: any char_sorter.py command
```

Or activate the environment (`call venv\Scripts\activate`) and call the scripts directly:

| Command | What it does |
|---|---|
| `python char_sorter.py intake` | Read sidecars and tag every new image in the Inbox (nothing is moved). |
| `python char_sorter.py bench` | Benchmark the tagger. |
| `python push_to_hydrus.py --check` | Test connection, key, permissions and tag service. |
| `python push_to_hydrus.py --dry-run --limit 5` | Show what would be sent. Never contacts Hydrus. |
| `python push_to_hydrus.py --limit 50` | Push a small batch first. |
| `python push_to_hydrus.py` | Push everything not yet pushed. |
| `python push_to_hydrus.py --force` | Re-send even files that look unchanged. |
| `python push_to_hydrus.py --folder "D:\X_Likes\Incoming\Inbox"` | Only images under one folder. |

### Importing an existing library

If you already have images on disk, scan them once (`python char_sorter.py scan` tags images under the sorted folder; use `--limit` on the push to try 50 first), then run `python push_to_hydrus.py`. The push can take a while on tens of thousands of files, and an interrupted run resumes where it stopped.

---

## Configuration

### `config/config.json`

Only needs the keys you want to change; anything missing uses the default. `config/config.example.json` lists every option. A misspelled key prints a warning.

```json
{
  "twitter_username": "your_twitter_handle",
  "parent_dir": "D:\\X_Likes",
  "tagger_backend": "camie",
  "batch_size": 1,
  "dml_device_id": null
}
```

| Key | Default | What it does |
|---|---|---|
| `twitter_username` | `""` | Your handle, without `@`. |
| `parent_dir` | `X_Likes` | Library root. Relative paths resolve next to the project. |
| `fetch_likes` / `fetch_bookmarks` | `true` | Which feeds option 1 downloads. |
| `gallery_dl_sleep` | `0` | Seconds between downloads. `1` helps if connections get dropped. |
| `gallery_dl_retries` | `4` | Retries per file before gallery-dl gives up on it. |
| `gallery_dl_abort` | `10` | Stop a feed after this many already-downloaded files in a row. `0` = never stop early. |
| `gallery_dl_extra_args` | `""` | Extra gallery-dl arguments, e.g. `"--sleep-request 1"`. Avoid `%` and `&`. |
| `cookies_file` | `cookies.txt` | Cookie file used for downloads. |
| `tagger_backend` | `wd14` | Set to `camie` for this workflow. Each backend keeps its own model files and score cache (`data/scores_cache_camie.db`). |
| `camie_model_url` / `camie_metadata_url` | Camie Tagger v2 | Files downloaded on first use. |
| `dml_device_id` | `null` | GPU for DirectML. `null` = auto-detect. Use `bench` to find the fastest. |
| `batch_size` | `4` | Images per tagger pass. `1` is often best on 2 GB GPUs. |
| `io_threads` | `4` | Threads that load images while the GPU works. |
| `cache_min_score` | `0.10` | Guesses below this aren't cached. Keep it at or below `possible_threshold`. |
| `image_extensions` | jpg, jpeg, png, webp | File types picked up. |
| `merge_variants` | `true` | Folds costume and form variants into the base character. |
| `character_aliases` | `{}` | Merge tags you consider the same character, e.g. `{"trailblazer_(honkai:_star_rail)": "stelle_(honkai:_star_rail)"}`. |

Camie's score thresholds for the Hydrus tags live in `hydrus.json`, not here. `confidence_threshold`, `review_threshold`, `primary_margin` and the other sorting keys only affect the legacy sorting commands.

JSON needs doubled backslashes in Windows paths, no trailing comma after the last entry, and no comments. The file may be UTF-8 with or without a BOM.

### `config/hydrus.json`

| Key | Default | What it does |
|---|---|---|
| `api_url` | `http://127.0.0.1:45869` | Where the Hydrus Client API listens. |
| `access_key` | `""` | The 64-character API key (or use `HYDRUS_ACCESS_KEY`). |
| `tag_service_name` | `camie` | The local tag service that receives the tags. |
| `tag_service_key` | `""` | Optional: use this service key instead of looking the name up. |
| `character_threshold` | `0.5` | Score for a `character:` tag. |
| `possible_threshold` | `0.3` | Score for a `possible character:` tag. |
| `series_threshold` | `0.5` | Score for a `series:` tag. |
| `add_creator` / `add_hashtags` | `true` | Send `creator:` and `hashtag:` tags. |
| `hashtag_namespace` / `max_hashtags` | `hashtag` / `30` | Namespace and cap for hashtag tags. |
| `sensitive_tag` | `x flag:sensitive` | Tag for sensitive tweets. Empty = off. |
| `add_url` / `add_note` / `set_post_date` | `true` | Send the tweet URL, the tweet text note, and the post date. |
| `note_name` / `date_domain` | `tweet` / `x.com` | Name of the note and domain of the timestamp. |
| `delete_after_import` | `false` | Hydrus deletes the original file from disk after importing it. See below. |
| `max_workers` | `24` | Parallel Hydrus API workers (1-24). Lower it if Hydrus becomes sluggish. |
| `max_consecutive_errors` | `10` | Stop the run after this many failures in a row. |

**Changing thresholds later only adds tags.** A push never removes a tag, so lowering a threshold adds more and raising it leaves the old ones in place. Fix wrong tags in Hydrus itself.

**`delete_after_import`:** Hydrus copies the files it imports, so the Inbox keeps a second copy of every image. Once a small test push looks right in Hydrus, set this to `true` and the Inbox empties itself as you go. The gallery-dl archive prevents re-downloads and `Meta/` keeps the sidecars.

---

## Working in Hydrus

- **Costume variants:** if Camie tags a costume as its own character, merge them with a tag sibling (for example `character:shiroko (swimsuit) (blue archive)` → `character:shiroko (blue archive)`). It applies retroactively to everything already imported.
- **Characters the tagger doesn't know** (new releases especially): search the `hashtag:` tag for that character, select all, and add the character tag yourself under *my tags*. Your own tags stay separate from the `camie` service.
- **Review the guesses:** search `possible character:*` to confirm or correct the weaker guesses.
- **Everything from one artist:** search `creator:<handle>`.
- **Tweet text** is stored as a note named `tweet`. Notes are shown in the media viewer and the right-hand notes panel.
- **Deleted files stay deleted.** Anything you delete in Hydrus is reported as "previously deleted" and skipped on later pushes.

---

## Hashtag Data (`data/tweet_hashtags.csv`)

Built automatically from the `.json` sidecars `gallery-dl` writes: on each intake the tweet ID, hashtags and author are merged into this file and the sidecars are moved to `Meta/`. It is machine-generated, so don't edit it. The push reads the sidecars themselves (from `Meta/` and `Inbox/`) for the tweet text, date and sensitive flag.

- **Lost or incomplete?** `python char_sorter.py rebuild-tags` re-reads every sidecar in `Meta/` and `Inbox/`. You can also point it at an old `tweet_hashtags.csv` or a folder of sidecars. Data is merged, never overwritten.
- **Authors missing for old likes?** Re-fetch metadata only: `gallery-dl --cookies cookies.txt --no-download --write-metadata "https://x.com/<handle>/likes" -d "<parent_dir>\Meta_refetch"`, then `rebuild-tags "<parent_dir>\Meta_refetch"`. Try `--range 1-20` first to confirm sidecars are written, and put the re-fetched sidecars under `Meta/` so the push can see them.
- Images are matched to tweets by the tweet ID in the filename (`<tweet_id>_<n>.<ext>`, gallery-dl's default). Renamed files lose their tweet metadata.

---

## Legacy Sorting Commands

The folder sorter this project started as is still in `char_sorter.py` (`sort`, `undo`, `inbox`, `tags`, `suggest-map`, `report`, `multiples`, `learn`, `export-hydrus`). It no longer has menu entries and isn't part of the flow above, but it keeps working through `run_update.bat <command>` if you want it. `config/hashtag_map.csv` and the sorting keys in `config.json` only matter for those commands.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| `[downloader.http][warning] ... Connection aborted ... (1/5)` | gallery-dl is retrying a dropped connection. Ignore it if the file downloads; if it keeps happening set `gallery_dl_sleep` to `1` and raise `gallery_dl_retries`. Failed files are retried on the next sync. |
| First run seems to hang | The Camie model is downloading (one time). An interrupted download is restarted safely. |
| `Could not reach Hydrus` | Open the Hydrus client, make sure the Client API service is enabled and the port matches `api_url`. |
| `Hydrus rejected the access key` | Re-copy the key into `config/hydrus.json`, or check the key still exists under *review services > client api*. |
| `Hydrus has no tag service named "camie"` | Create a local tag service with that name under *manage services*, or set `tag_service_name`. |
| `The access key lacks '...'` | Add the named permission to the key in Hydrus, then re-run. Files pushed without it aren't marked done, so they're retried. |
| Files show as "previously deleted" | You deleted them in Hydrus earlier. That's intentional. |
| 0 images with tags or no tweet data | Check that `Meta/` and `Inbox/` contain the `.json` sidecars, and run `rebuild-tags`. |
| Warning: unknown config keys | A key is misspelled. Compare it with the example file. |
| `Failed to read config.json` | The JSON is invalid (single backslashes, trailing comma, comments). |
| Tagger is slow or scanning stalls | Run `bench` and set `dml_device_id` and `batch_size` (try `1`). |
| `venv not found` | Run `setup.bat` first. |

---

## Sharing With Friends

Send the repo. `.gitignore` keeps out your `cookies.txt`, `config/config.json`, `config/hydrus.json` (which holds your API key), `data/` and `model/`, so none of your private data goes along. They run `setup.bat`, add their own `cookies.txt`, edit `config/config.json` and `config/hydrus.json`, and follow the Quick Start above.