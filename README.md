# AO3 Downloads Viewer

A Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships, a best-effort fandom guess), and cross-checks against ao3downloader's
own `log.jsonl` to flag anything logged as downloaded but missing on disk, or logged as a failure.

It does not trigger downloads or talk to AO3 over the network -- it only looks at files already
on disk, the existing log, and a small local SQLite file for manual corrections you make yourself.

## Pages

- **Downloads** (`/`) -- the full list with stats, filterable by fandom (`?fandom=...`, linked
  from the Fandom column or the Fandoms page).
- **Issues** (`/issues`) -- everything with a problem: a parse error, logged as downloaded but
  missing on disk, or a logged failure. Each row can be dismissed (hidden from the default view,
  toggle "show dismissed" to see them again) and has an inline form to manually fix the title,
  author, or fandom(s) when the parsed/logged values are wrong or missing.
- **Fandoms** (`/fandoms`) -- unique fandom names with work counts; click one to filter Downloads.

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
  (see `app/epub_meta.py`) that can occasionally include a character name or miss a fandom --
  use the Issues page's manual edit to correct it for a specific work.
- The downloads folder and log file are mounted read-only; only the small `/data` SQLite file
  (manual overrides/dismissals) is writable, and nothing here downloads or modifies your fics.

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
