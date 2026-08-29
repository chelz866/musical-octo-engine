# AO3 Downloads Viewer

A Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships, a best-effort fandom guess), and cross-checks against ao3downloader's
own `log.jsonl` to flag anything logged as downloaded but missing on disk, or logged as a failure.

It does not trigger downloads -- it only looks at files already on disk, the existing log, and a
small local SQLite file for manual corrections and tracked feeds you add yourself. The one
exception is the Tracked Feeds page, which does fetch AO3 Atom feed URLs you explicitly add.

Everything is cached in that same SQLite file and only updated when you click **Refresh** (top
right of every page) -- there's no background polling for the downloads folder/log. A refresh
re-scans the downloads folder/log and re-fetches every tracked feed regardless of its own
auto-refresh setting below. Until you click it, pages read the last snapshot even if files on disk
have changed since -- that's the tradeoff for pages loading instantly instead of re-walking the
filesystem and hitting AO3 on every request.

The one exception is per-feed auto-refresh on the Tracked Feeds page (opt-in, off by default only
in the sense that it's a per-feed toggle you control) -- a small background task polls feeds with
it enabled every `AUTO_REFRESH_INTERVAL_SECONDS` (default 4 hours) so new works show up without
you needing to click Refresh. Tracked feeds are handled by the
[`reader`](https://github.com/lemon24/reader) library in a separate SQLite file
(`FEEDS_DB_PATH`), which is what makes a work stay tracked even after it scrolls out of AO3's
recent-works window -- `reader` keeps every entry it has ever seen for a feed rather than mirroring
its current contents.

## Pages

- **Downloads** (`/`) -- the full list with stats, filterable by fandom (`?fandom=...`, linked
  from the Fandom column or the Fandoms page).
- **Issues** (`/issues`) -- everything with a problem: a parse error, logged as downloaded but
  missing on disk, or a logged failure. Each row can be dismissed (hidden from the default view,
  toggle "show dismissed" to see them again) and has an inline form to fix the title/author.
- **Fandoms** (`/fandoms`) -- unique fandom names with work counts; click one to filter Downloads.
- **Tags** (`/tags`) -- every untyped tag across the whole library, sorted by how many works have
  it, with a checkbox for "is this a fandom." This is the bulk tool: classifying one tag here fixes
  every work that has it in one action, instead of a per-work correction on each of them.
- **Tracked Feeds** (`/tracked`) -- add any AO3 Atom feed URL (tag, series, or user feed) and see,
  for each work in it: chapter progress, whether AO3 currently shows it as complete, whether you
  have it, and a best-effort "up to date" hint (compares the feed's last-updated time against when
  you downloaded/logged the work -- not an exact chapter-count comparison, see `app/rss.py`). Each
  feed's table is collapsible once it gets long. A real AO3 tag/series feed only shows a window of
  recent works, so tracking accumulates works seen over time rather than mirroring that window --
  a work doesn't disappear from your tracked list just because newer works pushed it off the feed.
  Each feed also has its own "Auto-refresh: on/off" toggle (see above).
- **Queue** (`/queue`) -- every tracked-feed work that isn't downloaded yet, or may have updated
  since you downloaded it, across all feeds, sorted with not-downloaded first. A first cut over
  the same status the Tracked Feeds page shows per feed; expected to grow.

Fandom is classified per *tag*, not per work (see `scanner._resolve_fandoms`). Downloads and Issues
have the same checkbox picker under the Fandom column ("edit") as a shortcut scoped to one work's
own tags, but saving it sets the same global classification the Tags page does -- checking "Torchwood"
there marks it a fandom everywhere it appears, not just on that one row.

## Running

1. Copy `.env.example` to `.env` and fill in `DOWNLOAD_DIR` (your ao3downloader downloads folder),
   `LOG_DIR` (the folder containing `log.jsonl`), and `DATA_DIR` (a writable folder for this app's
   own small SQLite file -- manual overrides and dismissals live there).
2. `docker compose up --build`
3. Open `http://<server>:8000/` (or whatever `HOST_PORT` you set).

## Scope of this version

- Only `.epub` files matching ao3downloader's `<work_id>_...epub` naming (underscore or space
  after the id -- settings.ini can customize this) are parsed for metadata; other formats it can
  produce (pdf/mobi/azw3) aren't parsed yet.
- Rating, Warnings, and Category come from AO3's fixed vocabularies and are matched reliably.
  Relationships are detected via the `/`/`&` convention in tag names. Fandom has no type label in
  the epub metadata and no consistent tag ordering across works, so it's a best-effort heuristic
  (see `app/epub_meta.py`) that can occasionally include a character name or miss a fandom -- use
  the Tags page (or the per-work picker) to correct it, which fixes every work sharing that tag.
- The downloads folder and log file are mounted read-only; only the small `/data` SQLite file
  (manual overrides/dismissals/cache/tracked feeds) is writable, and nothing here downloads or
  modifies your fics.
- Refresh of the downloads folder/log is entirely manual. Tracked feeds are the one exception:
  each has its own opt-in auto-refresh toggle, polled in the background every
  `AUTO_REFRESH_INTERVAL_SECONDS` (default 4 hours) regardless of whether anyone clicks Refresh.
- If you're upgrading from a version before the switch to `reader`, tracked feeds previously
  stored in the main SQLite file are migrated automatically into the new `FEEDS_DB_PATH` file the
  first time the app starts -- nothing to do manually.

## Development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

To run it locally against a folder of sample files before pushing a change:

```
DOWNLOAD_DIR=/path/to/test/downloads LOG_PATH=/path/to/test/log.jsonl DB_PATH=/tmp/app.db \
  uvicorn app.main:app --reload
```
