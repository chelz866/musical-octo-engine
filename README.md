# AO3 Downloads Viewer

A Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships, a best-effort fandom guess), and cross-checks against ao3downloader's
own `log.jsonl` to flag anything logged as downloaded but missing on disk, or logged as a failure.

It does not trigger downloads -- it only looks at files already on disk, the existing log, and a
small local SQLite file for manual corrections and tracked feeds you add yourself. The one
exception is the Tracked Feeds page, which does fetch AO3 Atom feed URLs you explicitly add.

Everything is cached and only updated on request -- there's no background polling for the
downloads folder/log. The **Refresh** button (top right of every page) re-scans the downloads
folder/log only; it deliberately does not also touch tracked feeds, since walking a large
downloads folder and hitting every tracked feed on every click was too much to bundle into one
button. Tracked feeds have their own separate **Refresh feeds** button, on the Tracked Feeds and
Queue pages, that force-checks every tracked feed regardless of its own auto-refresh setting
below. Until you click the relevant button, pages read the last snapshot even if files on disk (or
a feed) have changed since -- that's the tradeoff for pages loading instantly instead of
re-walking the filesystem or hitting AO3 on every request.

The one exception is per-feed auto-refresh on the Tracked Feeds page (opt-in, off by default only
in the sense that it's a per-feed toggle you control) -- a small background task polls feeds with
it enabled every `AUTO_REFRESH_INTERVAL_SECONDS` (default 1 hour) so new works show up without
you needing to click Refresh. Tracked feeds are handled by the
[`reader`](https://github.com/lemon24/reader) library in a separate SQLite file
(`FEEDS_DB_PATH`), which is what makes a work stay tracked even after it scrolls out of AO3's
recent-works window -- `reader` keeps every entry it has ever seen for a feed rather than mirroring
its current contents.

## Optional: Audiobookshelf integration

If you also keep your downloaded fics in an [Audiobookshelf](https://www.audiobookshelf.org/)
library, matched works get two things:

- A second link (🎧) next to the AO3 link on the Downloads and Issues pages, going straight to
  that item, plus the AO3 summary as a hover tooltip on the title.
- Title, author, rating, warnings, category, relationships, fandom, and language for that work
  come from Audiobookshelf's own already-scanned metadata (`books.title`/`description`/`genres`/
  `language`) instead of this app unzipping and parsing the epub file itself -- Audiobookshelf read
  the same embedded metadata during its own library scan, so for a matched work this is the same
  data, just not re-parsed. A work whose local epub is missing or corrupted (otherwise a
  parse-error Issue) still shows correct info as long as it matches in Audiobookshelf. Word count
  and chapter progress are the exception -- those always come from the epub file itself (see the
  Downloads page entry above), matched or not, since Audiobookshelf doesn't track either.

Matching is by AO3 work id, extracted from Audiobookshelf's own `libraryItems.path` column the same
way this app reads its own downloads folder, joined to the `books` table via `libraryItems.mediaId`
-- so it only works for items whose filename still has ao3downloader's `<work_id> Title -
Author.epub` naming; older imports renamed without the id won't match (those keep using the local
epub, unaffected).

It's entirely optional and off by default; set all of `ABS_DB_HOST_PATH`, `ABS_LIBRARY_ID`, and
`ABS_BASE_URL` in `.env` (see `.env.example` for the exact meaning of each) and uncomment the
matching volume line in `docker-compose.yml` to turn it on. Matching runs as part of the regular
(manual) Refresh, reading Audiobookshelf's sqlite file read-only -- this app never writes to it.

## Pages

Nav is grouped into three: **Home** (Downloads -- the actual browsing/search surface), **Browse**
(Fandoms/Tags -- reference lists you click through from), and **Admin** (Dashboard/Issues/Tracked
Feeds/Queue -- maintenance and setup, not day-to-day browsing). Both Browse and Admin are
dropdowns in the top nav.

- **Home** (`/`) -- the full list rendered as AO3-style work blurbs (rating/category/warning/
  completion icons, tags, summary, language/words/chapters), paginated 25 per page. Chapter
  progress and completion status are real here, not guessed -- read straight from the epub's own
  preface page (see `epub_meta.parse_epub_stats`), the same page AO3 embeds a "Stats:" line into on
  every export. Word count comes from the same place; Audiobookshelf doesn't track it (confirmed
  against a real schema export), so this only ever comes from the file itself, matched or not.

  A collapsible **Search & Filter** panel above the list mirrors AO3's own sidebar, including its
  Include/Exclude split: every facet (Rating/Warning/Category/Fandom/Character/Relationship/
  Additional Tags, plus Completion and Language for Include only -- AO3 has no Exclude equivalent
  for those two) starts collapsed to just its label, same as AO3, expand whichever ones you want.
  **Include** checkboxes are AND'd -- checking both "Angst" and "Fluff" means a work needs *both*,
  not either, matching real AO3 exactly (including the quirk that checking two values of a
  single-valued facet like Rating always matches nothing, since a work only ever has one).
  **Exclude** is the opposite: OR'd, so checking any box there drops a work that has *any* of them.
  Rating/Warning/Category (fixed AO3 vocabularies) always list every option with a live count.
  Fandom/Character/Relationship/Additional Tags/Language can run into the thousands, so each shows
  only what you've already selected (so you can unselect it) plus its top 10 next-most-common
  values *given every other filter you've already applied* -- the list keeps shifting toward what's
  relevant as you narrow down, for both Include and Exclude independently.

  To reach a tag that never shows up in the top 10, Fandom/Character/Relationship/Additional Tags
  (Include and Exclude both) have their own "Find another..." typeahead box -- a self-rendered
  dropdown (not the native `<input list>`/`<datalist>` combo, which turned out to be unreliable on
  mobile Chrome/Safari and simply wouldn't show suggestions there) backed by `/tags/search`, a small
  [`fast-autocomplete`](https://github.com/seperman/fast-autocomplete)-powered index built lazily
  per facet and rebuilt only when you refresh. It matches by prefix, and for a relationship tag
  specifically also matches by either party's name (typing "Jack" finds "Ianto Jones/Jack Harkness"
  even though the tag doesn't start with "Jack"), since the two are joined by `/` or `&` and indexed
  separately from the full tag. Tapping/clicking a suggestion (or typing the exact name and hitting
  Enter) checks it, same as any other checkbox. The free-text search box above also matches tag
  text, as a further fallback.

  **More Options**: Crossovers (include/exclude/only), a word-count range, and a downloaded-date
  range (`From`/`To`, compared against the same timestamp the "newest" sort uses). Sort-by covers
  title, author, word count, and date downloaded -- not hits/kudos/comments/bookmarks, since those
  are AO3 server-side engagement numbers this file-based dashboard has no access to. Every active
  filter (Include, Exclude, crossover, word count, date range, search) shows as a removable chip,
  checking a box always jumps back to page 1, and the URL is fully shareable/bookmarkable
  (`?rating=...&fandom=...&x_character=...` etc. -- Exclude params are `x_`-prefixed; the old
  `?fandom=...` links from the Fandom column and the Fandoms page still work unchanged, they're just
  one Include facet among several now).
- **Admin Dashboard** (`/admin`) -- the mounted downloads/log paths, and the at-a-glance stats
  (works on disk, total size, logged-success-but-missing, on-disk-no-log-entry, logged failures)
  that used to sit at the top of the Downloads page -- moved here since they're a health check, not
  something you need while browsing.
- **Issues** (`/issues`) -- everything with a problem: a parse error, logged as downloaded but
  missing on disk, or a logged failure. Each row can be dismissed (hidden from the default view,
  toggle "show dismissed" to see them again) and has an inline form to fix the title/author.
- **Fandoms** (`/fandoms`) -- unique fandom names with work counts; click one to filter Downloads.
- **Tags** (`/tags`) -- every untyped tag across the whole library, sorted by how many works have
  it, classified as Fandom, Character, or Freeform, paginated 100 per page. This is the bulk tool:
  classifying one tag here fixes every work that has it in one action, instead of a per-work
  correction on each of them. Filter tabs at the top (Fandom/Character/Freeform/Unclassified, each
  with a count) narrow the list to what still needs review, and the on-page text search narrows
  further -- a real library can easily have thousands of unique tags, so checking a box per tag and
  reading through a dropdown per row doesn't scale. Instead, each row has a checkbox ("Select all"
  in the header selects only the rows currently visible, respecting both the filter tab and the text
  search), and a bar above the table applies one category to everything checked ("Set selected:
  Fandom/Character/Freeform") -- search for a name, select all, glance at the list, then set it in
  one click. Since AO3 libraries are typically mostly Freeform tags, two further bulk actions ("mark
  unclassified on this page as Freeform" / "mark ALL unclassified tags as Freeform") sweep the rest
  without needing to select anything. A tag with no explicit classification falls back to the same
  heuristic guess used elsewhere (see `app/epub_meta.py`) for Fandom, or Freeform otherwise --
  "Unclassified" specifically means no one has confirmed it either way yet.
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
  `AUTO_REFRESH_INTERVAL_SECONDS` (default 1 hour) regardless of whether anyone clicks Refresh.
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
