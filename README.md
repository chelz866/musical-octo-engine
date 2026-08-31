# AO3 Downloads Viewer

A Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships, a best-effort fandom guess), and cross-checks against ao3downloader's
own `log.jsonl` to flag anything logged as downloaded but missing on disk, or logged as a failure.

It does not trigger downloads -- it only looks at files already on disk, the existing log, and a
small local SQLite file for manual corrections and tracked feeds you add yourself. The one
exception is the Tracked Feeds page, which does fetch AO3 Atom feed URLs you explicitly add.

Everything is cached and only updated on request -- there's no background polling for the
downloads folder/log. The **Refresh** button (on the Admin Dashboard) re-scans the downloads
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
- Series membership -- AO3's own "Part N of &lt;series&gt;" line -- comes from Audiobookshelf's
  `series`/`bookSeries` tables (its own series management, separate from `books`) rather than the
  epub file, which usually doesn't carry this at all. A book Audiobookshelf has filed under more
  than one series shows whichever it was added to first. It appears on the Downloads blurb between
  the summary and the stats line, and is a link to `/series/<name>` -- a local series view listing
  every other downloaded work in that series, in series order -- rather than to AO3 itself, since
  this app has no AO3 series id to link to (only Audiobookshelf's own internal one).
- Read status, once an account pairs itself with an Audiobookshelf username (Account page --
  entirely optional, and separate from that account's login). Audiobookshelf's `mediaProgresses`
  table tracks finished/unfinished per its own user, so pairing tells this app whose progress to
  read at the next Refresh -- one household's accounts can each pair their own Audiobookshelf
  username, pair none at all, or share one, independently of each other. A work Audiobookshelf
  reports finished shows a plain "✓ Read" badge (not editable here -- Audiobookshelf is the
  source of truth for it); everything else gets a manual Mark Read / Mark Unread toggle instead,
  which never touches or overrides the Audiobookshelf-derived status. "Show only unread" (More
  Options) hides anything either source marks read.

Matching is by AO3 work id, extracted from Audiobookshelf's own `libraryItems.path` column the same
way this app reads its own downloads folder, joined to the `books` table via `libraryItems.mediaId`
-- so it only works for items whose filename still has ao3downloader's `<work_id> Title -
Author.epub` naming; older imports renamed without the id won't match (those keep using the local
epub, unaffected).

It's entirely optional and off by default; set all of `ABS_DB_HOST_PATH`, `ABS_LIBRARY_ID`, and
`ABS_BASE_URL` in `.env` (see `.env.example` for the exact meaning of each) and uncomment the
matching volume line in `docker-compose.yml` to turn it on. Matching runs as part of the regular
(manual) Refresh, reading Audiobookshelf's sqlite file read-only -- this app never writes to it.

## Accounts, roles, and bookmarks

Every page requires logging in. The first time the app starts, it seeds one admin account --
username `admin`, password `admin` -- change that password immediately from the Account page
(top right, next to Log Out) once you're in. There are two roles:

- **Admin** -- everything, including the Admin Dashboard, Issues, Tracked Feeds, Queue, tag
  classification (`/tags/classify`), and user management (`/admin/users`, to create additional
  accounts or reset anyone's password).
- **User** -- Home and Browse only (Downloads, Fandoms, the read-only Tags list). No Admin nav
  item at all, and the per-work fandom-picker ("edit") is hidden too, since it's a tag
  classification shortcut -- meant for sharing the dashboard with someone who should be able to
  search/browse/bookmark but not touch the shared library's classification.

**Bookmarks** are per-user: the star next to a work's title on Downloads toggles it, and "Show
only bookmarked" (under More Options in the filter panel) narrows the list to just yours -- your
bookmarks are invisible to other accounts and vice versa. Everything else (the library itself,
tag classifications, tracked feeds) stays shared across every account, unchanged from before
logins existed. A bookmarked work also gets an "add note" toggle underneath it, for a short
private note to yourself (a reminder of why you saved it, where you left off, etc.) -- also
per-user, and cleared automatically if you remove the bookmark.

Sessions are opaque server-side tokens (a `sessions` table, not a signed cookie) and don't expire
on a timer -- they last until you log out. That's a deliberate simplification for a small,
private-network deployment; if that's ever not the case for your setup, keep that in mind.

**Themes**: the Account page lets you save any number of named themes -- each a "Custom CSS" box
where you can paste your own stylesheet, including a real AO3 skin -- and pick one to actually use.
Adding a theme applies it right away; switching back to an earlier saved one is a single "Use this
theme" click on its row, no need to re-paste its CSS, and deleting or editing one never disturbs the
others. "Switch to Default" turns your view back to the app's own look without deleting anything.
Only you see this; it has no effect on anyone else's account. Rather than guessing at AO3's markup,
this app's own page structure reuses AO3's real ids and classes directly, based on an actual
saved AO3 page: `#outer.wrapper` / `#header` / `#inner.wrapper` / `#main`, the nav's
`.primary.navigation.actions` / `.dropdown` classes, `li.blurb`, and -- the part that matters most
for how a skin actually looks -- tags render as real `<a class="tag">` links inside
`<li class="warnings">` / `.relationships` / `.characters` / `.freeforms`, and stats as a real
`dl.stats`. That means most AO3 skins apply directly, no translation needed, including the
tag-category coloring and nav gradients most skins define. A small fallback map in
`translate_ao3_skin_selectors` (`app/main.py`) rewrites a couple of AO3 selectors with no
equivalent here (`.splash`, `#stat_chart` -- this app has no homepage module or hits/kudos chart)
onto the closest stand-in. `#dashboard` (AO3's small personal-dashboard widget) is deliberately
left unmapped rather than redirected onto the main content area -- an earlier attempt at that
painted every work blurb solid gold instead of just giving it AO3's actual bordered look, since
`#dashboard`'s own background rule was never meant to cover that much surface. Tag links double as
real Downloads filters now too
(clicking a character/relationship/freeform tag on a blurb filters by it, same as fandom already did).

## Pages

Nav is grouped into three: **Home** (Downloads -- the actual browsing/search surface), **Browse**
(Fandoms/Tags -- reference lists you click through from), and **Admin**, visible only to admin
accounts (Dashboard/Issues/Tracked Feeds/Queue/Classify Tags/Users -- maintenance and setup, not
day-to-day browsing). Both Browse and Admin are dropdowns in the top nav.

- **Home** (`/`) -- the full list rendered as AO3-style work blurbs (rating/category/warning/
  completion icons, tags, summary, language/words/chapters), a bookmark star per work (see
  Accounts above), paginated 25 per page. Chapter progress and completion status are real here,
  not guessed -- read straight from the epub's own preface page (see `epub_meta.parse_epub_stats`),
  the same page AO3 embeds a "Stats:" line into on every export. Word count comes from the same
  place; Audiobookshelf doesn't track it (confirmed
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

  Once one or more Fandoms are checked (Include), the Character/Relationship/Additional Tags
  suggestions and "Find another..." search both narrow to only tags that make sense for that
  fandom: a tag with no fandom link at all (an unwrangled, universal trope like "Coffee Shops")
  always stays available, and one wrangled to the selected fandom(s) -- directly, or several
  wrangling hops down a chain (e.g. a Character wrangled under a Relationship wrangled under the
  Fandom) -- stays available too, but a tag wrangled under a *different* fandom (e.g. "The Doctor"
  under "Doctor Who") disappears from both while browsing Harry Potter -- see Tag wrangling under
  Classify Tags below for how that fandom link gets set.

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
  A sort dropdown (Name A-Z/Z-A, Most/Fewest Works -- defaulting to Most Works) reorders the list,
  and an A-Z letter strip (plus "#" for names starting with anything else) jumps straight to fandoms
  starting with that letter. A fandom wrangled as a child of another (set up on the admin Classify
  Tags page) nests under its parent here too, collapsed behind the same &#9656; toggle as the Tags pages.
- **Series** (`/series/<name>`, not in the nav -- reached by clicking a work's series line) -- every
  downloaded work in that series, in series order, reusing the same blurb rendering as Downloads.
- **Tags** (`/tags`, under Browse, any logged-in user) -- every tag across the whole library, with
  its Fandom/Character/Relationship/Freeform classification shown read-only. Filter tabs at the top
  (Fandom/Character/Relationship/Freeform/Unclassified, each with a count) narrow the list, a sort
  dropdown (Name A-Z/Z-A, Most/Fewest Works) reorders it, an A-Z/"#" letter strip narrows it further
  by first letter, and clicking a tag filters Downloads to just those works -- same idea as the
  Fandoms page, just for every tag instead of only fandoms. Filter, sort, and letter all combine and
  persist across pagination. A tag with no explicit
  classification falls back to a heuristic guess (see `app/epub_meta.py`): `_guess_fandoms` for
  Fandom, the "/" or "&" convention between names for Relationship, or Freeform otherwise --
  "Unclassified" specifically means no one has confirmed it either way yet. The relationship guess
  can misfire on an ordinary tag that happens to use the same punctuation (e.g. "Hurt/Comfort") --
  that's exactly what Classify Tags below is for fixing.
- **Classify Tags** (`/tags/classify`, under Admin) -- the mutable version of the page above, and
  the bulk tool for fixing that classification: classifying one tag here fixes every work that has
  it in one action, instead of a per-work correction on each of them. A real library can easily
  have thousands of unique tags, so checking a box per tag and reading through a dropdown per row
  doesn't scale -- instead, each row has a checkbox ("Select all" in the header selects only the
  rows currently visible, respecting the filter tab, sort order, and the on-page text search), and
  a bar above the table applies one category to everything checked ("Set selected: Fandom/Character/
  Relationship/Freeform") -- search for a name, select all, glance at the list, then set it in one
  click. The same sort dropdown as the read-only Tags page (Name A-Z/Z-A, Most/Fewest Works) is
  available here too, and is preserved through every bulk action's redirect. Since
  AO3 libraries are typically mostly Freeform tags, two further bulk actions ("mark unclassified on
  this page as Freeform" / "mark ALL unclassified tags as Freeform") sweep the rest without needing
  to select anything.

  This page also does AO3-style tag wrangling, split into two genuinely different mechanisms rather
  than one generic graph, matching how real AO3 wrangling separates a tag's "Parent Tag" from its
  "Fandom":

  **Same-category hierarchy** ("Merge selected into &rarr;" / "Make selected children of &rarr;")
  -- "Merge selected into" folds every checked tag into one canonical tag everywhere (display,
  Tags-page counts, classification), category-blind since two spellings of the same tag aren't a
  category question; it stops appearing here as its own row (undo it from the **Tag Wrangling**
  page under Admin -- see below). "Make selected children of" keeps each checked tag as itself but
  *requires* the parent to share the same category -- a Fandom's parent must be a Fandom, a
  Character's a Character, a Relationship's a Relationship, a Freeform tag's a Freeform tag; a tag
  of a different category is silently skipped. Filtering or excluding Downloads by the parent also
  matches works only tagged with the child, at any depth in the chain (a real multi-level hierarchy
  is allowed, e.g. Freeform &rarr; Freeform &rarr; Freeform, cycle-checked). The parent doesn't have
  to already exist as a real tag -- typing a brand-new name creates a consolidated parent purely to
  group existing same-category tags under (e.g. wrangling "Torchwood" and "Doctor Who," both
  Fandoms, as children of a new "Sci-Fi Shows"); it shows up here as its own row with a count of the
  distinct works matching any descendant (not summed), but starts Unclassified like any new tag, so
  it only appears under a specific category tab once you've classified it yourself. A parent with
  children shows a small &#9656; toggle and renders them indented underneath, one level deeper per
  hop, collapsed by default -- the same nesting shows on the read-only Tags and Fandoms pages too,
  and searching this page's text filter for a descendant auto-expands every collapsed ancestor
  needed to reveal it.

  **Fandom/Character/Relationship association** (the Fandom/Character(s)/Relationship(s) columns)
  -- a completely separate, cross-category concept: every Character, Relationship, and Freeform tag
  gets its own "Fandom" dropdown (defaulting to "No Fandom"), a Relationship gets one Character
  dropdown per "/"-or-"&amp;"-separated name part in its own text (the Character's spelling can
  differ from the literal substring), and a Freeform tag can be linked to any number of Characters
  and Relationships via small add/remove chips. Every one of these dropdowns (and the bulk "Apply to
  selected" trio below) lists its options depth-first through the same-category hierarchy above
  rather than one flat alphabetical list -- a parent tag is immediately followed by its own children,
  each indented one level further, so a same-category family stays visually grouped even inside a
  plain `<select>`. The dropdown separates "No Fandom (auto)" -- nobody's set anything on this tag
  or any of its same-category ancestors, so it defaults to none -- from a plain "No Fandom," a real,
  explicit choice that's saved just like picking an actual Fandom (an explicit "for real, no fandom,"
  not a placeholder for "undecided"). A same-category child with no Fandom of its own inherits the
  nearest ancestor's explicit choice (a real Fandom or an explicit "No Fandom," whichever is closer),
  falling back to "No Fandom (auto)" only if nothing in the whole chain has ever set one; picking
  "No Fandom (auto)" back on a tag clears its own explicit choice, reverting it to that inheritance.
  Once a Character/Relationship/
  Freeform tag resolves to a real Fandom, every work using that tag counts as belonging to that
  Fandom for Downloads filtering and the Fandoms page, even when the epub's own raw tags never
  mention it directly -- and the Character/Relationship/Additional-Tags suggestions and "Find
  another..." search on Downloads narrow to that Fandom the same way (see the Search & Filter
  section above).

  Setting the same association on many tags at once doesn't need a visit to each row: a Fandom/
  Character/Relationship dropdown trio plus "Apply to selected" (next to the other bulk-action
  buttons) applies whichever of the three you picked (each defaults to "don't change"/"don't add")
  to every checked tag in one action -- Fandom to every selected Character/Relationship/Freeform
  tag, Character/Relationship only to selected Freeform tags (a Relationship's Characters stay
  per-name-part, set individually in its own row).

  **Organize by:** (both this page and the read-only Tags page above) regroups the current listing
  by association instead of the same-category hierarchy -- pick Fandom, Character, or Relationship
  (mutually exclusive, "None" restores the normal same-category nesting) and every tag on the list
  is nested under its resolved Fandom/linked Character(s)/linked Relationship(s) as a synthetic
  parent row instead of its same-category parent. This is how you see, say, the Relationship tab
  grouped by Fandom, or the Freeform tab grouped by the Characters each tag is linked to. A tag
  with more than one association for the chosen dimension (a Freeform tag linked to two Characters)
  appears once under each parent; a tag with none stays a standalone top-level row rather than
  disappearing. On the read-only Tags page, clicking a synthetic parent heading filters Downloads by
  that association directly (e.g. a Fandom heading links like the Fandoms page does), not by
  whatever category the heading's own name happens to also be classified as.

  Organizing by Fandom treats the "No Fandom (auto)" vs. explicit "No Fandom" distinction above the
  same way it treats a real one: a tag someone deliberately set to "No Fandom" (on itself or
  inherited from an ancestor's explicit choice) gets grouped under its own "No Fandom" heading right
  alongside the real Fandom headings, while a tag nobody's classified either way yet stays a
  standalone top-level row instead of being swept into that heading -- so "grouped under No Fandom"
  only ever means someone confirmed there really is none, not "still undecided."
- **Tag Wrangling** (`/tags/classify/wranglings`, under Admin) -- the full same-category "Merged
  into"/"Child of" list (see Classify Tags above), split onto its own page once it got too long to
  sit at the bottom of that one -- a text box filters the list by tag or target as you type. This is
  also the only place to undo a merge, since a merged tag no longer has a row of its own on Classify
  Tags to undo it from. The per-tag Fandom/Character/Relationship association controls stay on
  Classify Tags itself, since they need the row's own count/category context.
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
have the same checkbox picker under the Fandom column ("edit", admin-only -- hidden entirely for a
regular user, same restriction as the Classify Tags page itself) as a shortcut scoped to one work's
own tags, but saving it sets the same global classification the Classify Tags page does -- checking
"Torchwood" there marks it a fandom everywhere it appears, not just on that one row.

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
