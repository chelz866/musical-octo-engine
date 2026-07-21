# AO3 Downloads Viewer

A read-only Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships), and cross-checks against ao3downloader's own `log.jsonl` to flag any
work that's logged as downloaded but missing on disk.

This first version is intentionally read-only: no database, no AO3 network calls, no ability to
trigger downloads. It only looks at files already on disk plus the existing log.

## Running

1. Copy `.env.example` to `.env` and fill in `DOWNLOAD_DIR` (your ao3downloader downloads folder)
   and `LOG_DIR` (the folder containing `log.jsonl`).
2. `docker compose up --build`
3. Open `http://<server>:8000/` (or whatever `HOST_PORT` you set).

## Scope of this version

- Only `.epub` files matching ao3downloader's `<work_id>_<title>__<author>.epub` naming are
  parsed for metadata; other formats it can produce (pdf/mobi/azw3) aren't parsed yet.
- Rating, Warnings, and Category come from AO3's fixed vocabularies and are matched reliably.
  Relationships are detected via the `/`/`&` convention in tag names. Fandom, Character, and
  Freeform tags can't be reliably told apart from local epub metadata alone (no consistent
  ordering), so they're intentionally left out of this version rather than guessed at.
- The downloads folder and log file are mounted read-only; nothing here downloads or modifies
  anything.

## Development

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```
