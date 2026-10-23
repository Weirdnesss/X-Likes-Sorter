# X Likes & Bookmarks Character Sorter

Downloads your liked and bookmarked media from X (Twitter), tags each image with the **WD14 ConvNeXtV2** anime tagger, and sorts everything into character, series and artist folders. Tweet hashtags and authors (saved by `gallery-dl` as `.json` sidecars) are used to place the images the tagger can't recognize.

---

## Key Features

- **One-click workflow:** `run_update.bat` has a menu for syncing, sorting, hashtag tools and undo, and also accepts commands directly.
- **Automated downloads:** `gallery-dl` fetches Likes and Bookmarks together with full JSON metadata sidecars.
- **AI character tagging:** the `wd-v1-4-convnextv2-tagger-v2` ONNX model runs on DirectML (AMD, Intel, Nvidia GPUs) and falls back to CPU if no GPU is usable.
- **Tweet evidence for what the tagger misses:** hashtag rules, same-tweet siblings, series hashtags, author hints and artist folders.
- **Fast re-sorting:** tagger scores are cached in SQLite (`data/scores_cache.db`), so changing rules or thresholds and re-sorting takes seconds.
- **Non-destructive:** every sort can be previewed with `--dry-run`, every move is logged, and `undo` reverses the log.
- **Portable:** all settings live in `config/config.json`, with no hardcoded drive letters or user names.

---

## How Images Are Sorted

Each image goes to the first rule that places it:

1. **Tagger:** a character recognized with enough confidence and a clear lead over the runner-up goes to that character's folder.
2. **Character hashtags** from `config/hashtag_map.csv`.
3. **Same-tweet siblings:** other images from the same tweet that the tagger did identify.
4. **Series hashtags** (`_Series/<name>` rules in the map).
5. **Author hints:** if an artist's confirmed images are overwhelmingly one character, their other images follow.
6. **Artist folders:** leftovers go to `_Artists/<handle>` when that artist has enough of them.
7. Everything else stays in `_Unsorted`.

Other outcomes: `_Review/<character>` holds weak guesses (between `review_threshold` and `confidence_threshold`), `_Multiple` holds images with several characters and no clear lead, and `_Misc Characters` holds characters with fewer than `min_folder_size` images. Folder names are configurable.

Expect a sizable share of a large library to end up in `_Unsorted`, `_Multiple` or artist/series folders. That's normal for art with original characters or characters the model doesn't know.

---

## Project Structure

```text
x-likes-sorter/
├── config/
│   ├── config.example.json  # Every option with its default (tracked in git)
│   ├── config.json          # Your settings (created by setup.bat, not tracked)
│   └── hashtag_map.csv      # Hashtag-to-folder rules (tracked in git)
├── data/                    # Generated: score cache, tweet_hashtags.csv, sort log, download archive
├── model/                   # Generated: auto-downloaded WD14 model and tag list
├── .gitignore               # Keeps cookies, config.json, data/ and model/ out of git
├── README.md
├── char_sorter.py           # Tagging, sidecar ingestion and sorting logic
├── requirements.txt
├── run_update.bat           # Menu and command runner
└── setup.bat                # One-time installer
```

Your library lives under `parent_dir`: `Incoming/Inbox` (new downloads), `Sorted/` (the result) and `Meta/` (processed sidecars).

---

## Prerequisites

1. **Windows 10/11**
2. **Python 3.10+** added to `PATH`
3. **Git** (only if you clone the repo)
4. An **X/Twitter account**

---

## Quick Start (Fresh PC Setup)

1. **Get the project:** `git clone https://github.com/your-username/x-likes-sorter.git`, then `cd x-likes-sorter`.
2. **Run `setup.bat`.** It creates the virtual environment, installs the requirements, creates `config/`, `data/` and `model/`, and copies `config/config.example.json` to `config/config.json` if you don't have one.
3. **Export your X cookies** from a logged-in browser (for example with the *Get cookies.txt LOCALLY* extension) and save them as `cookies.txt` in the project folder.
4. **Edit `config/config.json`:** set `twitter_username` (without `@`) and `parent_dir`. Use a full path with doubled backslashes (`"D:\\X_Likes"`) or a plain name like `"X_Likes"` to keep the library next to the project.
5. **Run `run_update.bat`** and pick option 1.

---

## Getting the Best Results (Recommended Order)

1. **Benchmark first.** Run menu option 12, then set `dml_device_id` and `batch_size` to the fastest combination. Low-VRAM GPUs are often fastest at `batch_size` 1.
2. **First full sync.** Set `"gallery_dl_abort": 0` so gallery-dl doesn't stop early, run option 1, then set it back to `10`. X may not serve your oldest likes, so the first sync might not reach all of them.
3. **Build a hashtag map.** Run option 8 (preview the suggestions), then option 9 to add them. Add series tags by hand as `_Series/<name>`; option 7 shows which hashtags are most common. Save the CSV as UTF-8.
4. **Preview, check, apply.** Run option 4 (full re-sort preview), spot-check about 20 rows in `sort_preview.csv` for each source (`hashtag`, `author`, `artist`), then run option 5.
5. **See what's left.** Run option 11 for the top hashtags and artists among unsorted images, add rules, and repeat steps 4 and 5.
6. **Settings:** an `artist_folder_min` of about 5 organizes leftovers well. Leave `review_threshold` and `primary_margin` alone unless a dry run shows a clear gain, and change one setting at a time.

Afterwards: run option 1 whenever you like new things, and a full re-sort (4, then 5) after changing the hashtag map or any sorting setting.

---

## Everyday Usage

Double-click **`run_update.bat`**:

```text
 SYNC & SORT NEW FILES
  [1] Sync (Fetch Likes + Bookmarks + Tag + Sort)
  [2] Process Inbox Only (files already in Inbox)
  [3] Inbox Dry-Run Preview (no files moved)

 WHOLE LIBRARY
  [4] Full Re-sort Preview (dry run)
  [5] Full Re-sort (moves files)
  [6] Undo Moves From The Sort Log

 HASHTAGS
  [7] Top Unmapped Hashtags
  [8] Suggest Hashtag Rules (preview)
  [9] Suggest Hashtag Rules (add to hashtag_map.csv)
  [10] Rebuild Hashtag Data (from saved sidecars)
  [11] Report On Unsorted Images (run option 4 first)

 TOOLS
  [12] Benchmark GPU / Batch Size

  [0] Exit
```

- **[1]** fetches Likes and Bookmarks (each can be turned off in the config), reads the sidecars, downloads the model on first use, tags the new images and sorts them.
- **[2] / [3]** process or preview only the files currently in the Inbox. A dry run writes `sort_preview.csv` (columns: file, folder, source) and moves nothing.
- **[4] / [5]** re-plan the *whole* library, including files that are already sorted. Options that move files ask for confirmation.
- **[6]** reverses *every* move in `data/sort_log.csv`, not just the last run.

### Running commands without the menu
`run_update.bat` also accepts a command (useful for Task Scheduler and quick one-offs):

```cmd
run_update.bat sync                   :: the full sync flow from option 1
run_update.bat sort --dry-run         :: any char_sorter.py command and options
run_update.bat suggest-map --apply
run_update.bat tags --top 100
```

Or activate the environment (`call venv\Scripts\activate`) and run `char_sorter.py` directly:

| Command | What it does |
|---|---|
| `python char_sorter.py inbox [--dry-run]` | Ingest sidecars, tag and sort the Inbox. |
| `python char_sorter.py scan` | Tag every image in `Sorted/` that isn't cached yet. |
| `python char_sorter.py sort [--dry-run]` | Re-plan and sort the whole library from cached scores. |
| `python char_sorter.py undo` | Reverse every move in `data/sort_log.csv`. |
| `python char_sorter.py tags [--all] [--top N]` | Most common hashtags (by default only those without a rule). |
| `python char_sorter.py report [--top N]` | Hashtags and artists among unsorted images (after a dry run). |
| `python char_sorter.py suggest-map [--apply] [--min-tweets N] [--min-share X]` | Suggest hashtag rules from your tagged images. |
| `python char_sorter.py rebuild-tags [folder-or-csv ...]` | Rebuild hashtag data from sidecars or an old CSV. |
| `python char_sorter.py bench` | Benchmark the tagger. |

---

## Configuration

`config/config.json` only needs the keys you want to change; anything missing uses the default. `config/config.example.json` lists every option. A misspelled key prints a warning when the script runs.

```json
{
  "twitter_username": "your_twitter_handle",
  "parent_dir": "D:\\X_Likes",
  "batch_size": 1,
  "dml_device_id": null,
  "artist_folder_min": 5
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
| `dml_device_id` | `null` | GPU for DirectML. `null` = auto-detect. Use `bench` to find the fastest. |
| `batch_size` | `4` | Images per tagger pass. Run `bench`; `1` is often best on 2 GB GPUs. |
| `io_threads` | `4` | Threads that load images while the GPU works. |
| `cache_min_score` | `0.10` | Guesses below this aren't cached. Lowering it needs a rescan; keep it at or below `review_threshold`. |
| `image_extensions` | jpg, jpeg, png, webp | File types picked up. |
| `model_url` / `tags_url` | WD14 ConvNeXtV2 v2 | Tagger files to download. Switching models also needs `model/` emptied and `data/scores_cache.db` deleted, since scores aren't comparable. |
| `confidence_threshold` | `0.35` | Minimum score to place an image by the tagger. |
| `review_threshold` | `0.20` | Guesses between this and `confidence_threshold` go to `_Review`. |
| `primary_margin` | `0.25` | How far ahead the top character must be to avoid `_Multiple`. |
| `min_folder_size` | `3` | Fewer images than this and a character goes to `_Misc Characters`. |
| `use_sibling_hints` | `true` | Place an image using other images from the same tweet. |
| `use_author_hints` | `true` | Route by what the artist usually draws. |
| `author_min_tweets` / `author_min_share` | `3` / `0.8` | An artist needs this many confirmed tweets, with this share the same character. |
| `artist_folder_min` | `8` | Leftovers go to `_Artists/<handle>` if the artist has this many. `0` = off. |
| `folder_unsorted`, `folder_multiple`, `folder_review`, `folder_misc`, `folder_artists` | `_Unsorted`, `_Multiple`, `_Review`, `_Misc Characters`, `_Artists` | Folder names inside `Sorted/`. Renaming on an existing library moves files on the next full sort. |
| `summary_top_folders` | `25` | How many folders the sort summary lists. |

JSON needs doubled backslashes in Windows paths, no trailing comma after the last entry, and no comments. The file may be UTF-8 with or without a BOM.

---

## Hashtags

### Hashtag map (`config/hashtag_map.csv`)
Maps a hashtag (lowercase, without `#`) to a folder relative to `Sorted/`:

```csv
hashtag,folder
bluearchive,_Series/Blue Archive
genshinimpact,_Series/Genshin Impact
hutao,Hu Tao
```

- Rules starting with `_Series/` group images under a franchise folder.
- Save the file as UTF-8 (Notepad: *Save As* > Encoding UTF-8; Excel: *CSV UTF-8*), or Japanese hashtags won't match. A BOM is fine. Keep the header line `hashtag,folder`.
- There is no downloadable map; which hashtags mean which character depends on what you like. Build yours with `suggest-map` and by hand.

### Hashtag data (`data/tweet_hashtags.csv`)
Built automatically from the `.json` sidecars `gallery-dl` writes: on each sync the tweet ID, hashtags and author are merged into this file, and the sidecars are then moved to `Meta/`. It is machine-generated, so don't edit it.

- **Lost or incomplete?** `rebuild-tags` (menu option 10) re-reads every sidecar in `Meta/` and `Inbox/`. You can also point it at an old `tweet_hashtags.csv` or any folder of sidecars; data is merged, never overwritten.
- **Authors missing for old likes?** Re-fetch the metadata only: `gallery-dl --cookies cookies.txt --no-download --write-metadata "https://x.com/<handle>/likes" -d "<parent_dir>\Meta_refetch"`, then `rebuild-tags "<parent_dir>\Meta_refetch"`. Try a few items first (`--range 1-20`) to confirm sidecars are written.
- Images are matched to tweets by the tweet ID in the filename (`<tweet_id>_<n>.<ext>`, gallery-dl's default). Renamed files lose hashtag and author routing.

### Suggesting rules (`suggest-map`)
Looks at tweets where the tagger identified exactly one character and suggests hashtags that point to the same character on at least 5 tweets (80% or more of the time, from at least 3 different artists). It writes `config/hashtag_map_suggested.csv`; copy the rows you want into the map or use `--apply` to append them all. The tagger's mistakes get copied into the rules too, so skim the list before applying.

---

## Author Hints and Artist Folders

**Author hints** route unrecognized images by what the artist usually draws. An artist qualifies with at least `author_min_tweets` tagger-confirmed tweets, at least `author_min_share` of them the same character. Only tagger-confirmed images count as evidence, so hashtag and sibling placements never feed back in. Hints apply only to `_Review` and `_Unsorted` images, after every other rule has failed. `_Multiple` is never touched. Check the rows with source `author` in `sort_preview.csv` before applying.

**Artist folders** group whatever is still unplaced into `Sorted/_Artists/<handle>`, for artists with at least `artist_folder_min` such images. Artists below the minimum stay in `_Unsorted`. Counts are library-wide: if you only process the Inbox, older images stay in `_Unsorted` until the next full re-sort.

---

## Good to Know

- **Don't move files by hand.** The cache is keyed by file path, so manually moved files are invisible to `sort` and aren't learned from. A later full `scan` can score them again, and the next `sort` may move them back.
- **Empty folders in `Sorted/` are not removed automatically** after a re-sort. To remove them (preview first, then run it without `-WhatIf`):
  ```powershell
  Get-ChildItem "D:\X_Likes\Sorted" -Directory -Recurse | Sort-Object { $_.FullName.Length } -Descending | Where-Object { -not (Get-ChildItem $_.FullName -Force) } | Remove-Item -WhatIf
  ```
- **Menu options 1-3 only touch the Inbox.** To re-sort files that are already sorted, use options 4 and 5.
- **Undo reverses the whole sort log**, not just the last run. To undo only a single run, rename `data/sort_log.csv` to `sort_log_old.csv` before you start it.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| `[downloader.http][warning] ... Connection aborted ... (1/5)` | gallery-dl is retrying a dropped connection. Ignore it if the file downloads; if it keeps happening set `gallery_dl_sleep` to `1` and raise `gallery_dl_retries`. Failed files are retried on the next sync. |
| First run seems to hang | The model is downloading (a few hundred MB, one time) and prints progress every 25 MB. An interrupted download leaves only a `.part` file and is safely restarted. |
| `Hashtag rules: 0` or `tweets with hashtag data: 0` | Check `config/hashtag_map.csv` (UTF-8, header `hashtag,folder`) and run `rebuild-tags`. |
| Warning: unknown config keys | A key in `config.json` is misspelled. Compare it with `config.example.json`. |
| `Failed to read config.json` | The JSON is invalid (single backslashes, trailing comma, comments). Fix it; defaults are used until then. |
| Tagger is slow or scanning stalls | Run `bench` and set `dml_device_id` and `batch_size` (try `1`). |
| `venv not found` | Run `setup.bat` first. |
| Author hints find 0 artists | Authors are missing from `tweet_hashtags.csv`. See "Authors missing for old likes?" above. |

---

## Sharing With Friends

Send the repo. `.gitignore` keeps out your `cookies.txt`, `config/config.json`, `data/` and `model/`, so none of your private data goes along. `config/hashtag_map.csv` is included, so they start with your hashtag rules and can add their own. They run `setup.bat`, add their own `cookies.txt`, edit `config/config.json`, and follow "Getting the Best Results" above.