# AO3 Downloads Viewer

A Docker dashboard for what [ao3downloader](https://github.com/nianeyna/ao3downloader) has
downloaded. It scans your downloads folder, reads embedded epub metadata (title, author, rating,
category, relationships, a best-effort fandom guess), and cross-checks against ao3downloader's
own `log.jsonl` to flag anything logged as downloaded but missing on disk, or logged as a failure.

Mostly it only looks at files already on disk, the existing log, and a small local SQLite file for
manual corrections and tracked feeds you add yourself -- the Tracked Feeds page is one exception,
fetching AO3 Atom feed URLs you explicitly add, and the Queue page's "Download Selected" is the
other, actually downloading works you pick in the background using ao3downloader's own internals
(see "Optional: downloading from within the app" below) rather than only reading what a separate,
manually-run ao3downloader already produced.

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

## Optional: downloading from within the app

The Queue page (`/queue`) can actually fetch the works you select, in the background, instead of
only telling you what's missing. This is meant for exactly the situation of a large backlog (a
freshly-added tracked feed, hundreds or thousands of works) that would otherwise mean running
ao3downloader by hand and babysitting it -- select rows, click "Download Selected", and it keeps
going on its own while you do something else in the app (classify tags, browse, whatever).

**How it works**: [ao3downloader](https://github.com/nianeyna/ao3downloader) itself is a menu-driven
interactive CLI with no non-interactive mode -- every one of its actions prompts through `input()`/
`getpass()` and has no command-line flags at all. Underneath that menu, though, its actual
scrape-and-save logic (`Repository`, `FileOps`, `Ao3`) is plain, ordinary, non-interactive Python --
the prompting lives entirely in a thin CLI shell around those classes. This app
depends on `ao3downloader` as a library and drives those classes directly (see `app/ao3_client.py`),
bypassing its menu shell entirely, so there's nothing to script or automate at the terminal level.
Downloaded files and log entries land in the exact same `DOWNLOAD_DIR`/`LOG_DIR` folders (and the
exact same `log.jsonl` format) a manual ao3downloader run already uses -- this app's own scanner
picks them up on the next Refresh with no extra wiring, and any failure shows up on the Issues page
exactly like a failure from a manual run would, since it's ao3downloader's own logging doing that,
not something this app tracks separately.

**Setup**: `DOWNLOAD_DIR` and `LOG_DIR` are mounted read-write now (not read-only), since this
feature writes into them. Login is optional -- set `AO3_USERNAME`/`AO3_PASSWORD` in `.env` only if
you need restricted/mature-locked-behind-login works or your own reading history; ordinary public
works download fine with both left blank, unauthenticated. `AO3_EXTRA_WAIT_SECONDS` (default 2) adds
a small delay between each download on top of ao3downloader's own rate-limit backoff (which only
kicks in once AO3 actually throttles a request) -- a deliberately conservative default given this
can run unattended over a queue thousands of works long.

If a login attempt itself fails (wrong password, or AO3 blocking what looks like an automated
login), a banner appears at the top of every page: "AO3 login failed on the last download run
(...)". The batch still runs -- ordinary public works don't need login at all, so the rest of the
queue keeps going unauthenticated -- but anything requiring login will keep failing until this is
fixed. Double-check `AO3_USERNAME`/`AO3_PASSWORD`, restart the container to pick up a changed
`.env`, and the banner clears itself on the next successful login.

**How the queue behaves**: selecting rows and clicking "Download Selected" adds them to a small
persistent queue (a work id already anywhere in it, pending or finished, is left alone rather than
re-queued, so re-selecting the same rows twice is harmless) and starts a background worker if one
isn't already running. The worker works through it one item at a time, stops on its own once it's
empty, and picks back up automatically if the app restarts partway through a big batch. "Stop After
Current Item" lets whatever's mid-download finish rather than aborting it, then halts; "Clear
Attempted Count" just resets the Queue page's own counter once you've confirmed a batch actually
landed (via Home/Issues) -- it doesn't affect the real downloaded files or log.jsonl.

A row's "attempted" status only means the worker got to it, not that it necessarily succeeded --
per-item success/failure is exactly what Home ("✓ on disk") and Issues (parse errors, logged
failures) already surface, so this doesn't duplicate that tracking. Selecting a "may need update"
row does a fresh full download, the same as a new one -- it isn't a true incremental update.

**Keeping the dashboard in sync while it downloads**: the worker re-scans the downloads folder
periodically (every `DOWNLOAD_WORKER_REFRESH_INTERVAL_SECONDS`, and always once more right when a
batch finishes or is stopped) so newly-downloaded/redownloaded works show up on Home/Incomplete
Works without waiting for a manual Refresh click. A redownload only actually replaces the existing
file in place if ao3downloader computes the exact same filename for it as last time (its
`FileNamePattern` setting) -- otherwise it just writes a second file under a different name,
leaving the original sitting there with its original, increasingly stale-looking modified date.
Since this app can't know for certain what pattern a given library's files were originally created
with, the download worker closes that gap itself: after each download it looks for every file
matching that work id and keeps only the most recently written one, so a redownload can never
silently leave a stale duplicate for the scanner to show instead of the fresh copy. Nothing is ever
removed unless the download actually produced a file -- a failed attempt leaves the sole existing
copy untouched.

## Accounts, roles, and bookmarks

Every page requires logging in. The first time the app starts, it seeds one admin account --
username `admin`, password `admin` -- change that password immediately from the Account page,
reached via the "Hi, {username}" dropdown at the top right, once you're in. That same dropdown also
has **Bookmarks** (a shortcut to `/?bookmarked=true`) and **History** (see below). There are two
roles:

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

**History** (`/history`, from the account dropdown) lists every work you've clicked into before,
most recently viewed first, reusing the same blurb rendering as Downloads with a "last viewed" time
in place of nothing extra. Clicking a work's title counts as a view whether it opens the in-app
reader (a downloaded work) or goes out to AO3 (via `/go/{work_id}`, so an un-downloaded work still
gets tracked); re-viewing an already-listed work just moves it back to the top rather than adding a
second row. Per-user, like bookmarks.

**Time zone**: every timestamp this app shows (downloaded dates, last refreshed, History's "last
viewed", and so on) is recorded on the server's own clock -- UTC by default, or whatever `TZ` you
set in `.env` (see below) -- and converted to whichever zone you pick under Account for display.
It's a personal display preference, not a data change: two accounts can each pick their own zone (or
leave it on "Server time") independently, and it never touches what's actually recorded.

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

  A work's title (Home and Issues both) opens straight into an in-browser reader
  (`/reader/{work_id}`) whenever the file is actually on disk -- a one-page-per-chapter view with
  Next/Previous and a chapter-jump dropdown, reading directly from the work's own downloaded epub.
  It's a plain fallback for whenever an external reader (e.g. Audiobookshelf) isn't cooperating, not
  a replacement for one: no bookmarked reading position, no font/theme controls of its own beyond
  whatever Custom CSS theme you've set (see Account, above -- the reader's content area uses AO3's
  own `userstuff` class, so an AO3 skin styles it same as everywhere else in this app). Chapter
  titles come from whatever heading the chapter's own HTML uses (AO3's real per-chapter title, if it
  set one); a chapter's bundled image renders through this app rather than as a broken relative path.
  A Mark Read toggle sits right in the reader's own header, same manual per-account toggle as
  Downloads. Available to any logged-in user, not just admins -- reading isn't an editing action. A
  work not yet downloaded still opens its title in a new tab, straight to AO3 (via `/go/{work_id}`,
  so it still lands in History -- see below); AO3 itself stays one click away either way through the
  🔗 link next to the title.

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
  something you need while browsing. A "Use Home as edit source" checkbox (per-user, off by default)
  adds a small "Edit" button to the bottom-right corner of every blurb on Home -- a shortcut straight
  into Classify Tags, filtered to just that one work's own tags (a banner at the top says which work
  is loaded, with a "clear" link back to the whole library), instead of hunting for them in a
  library-wide list. Filter tabs, sort, and Organize-by still apply on top of that narrowed set, and
  every action taken while a work is loaded keeps it loaded afterward.
- **Issues** (`/issues`) -- everything with a problem: a parse error, logged as downloaded but
  missing on disk, or a logged failure. A logged failure shows ao3downloader's own exception message
  (e.g. "Work is only available to registered users of the Archive.") both as a hover tooltip on the
  badge and as plain text underneath, straight from log.jsonl's own `error` field -- see "Optional:
  downloading from within the app" above for adding `AO3_USERNAME`/`AO3_PASSWORD` if that's the
  cause. Each row can be dismissed (hidden from the default view, toggle "show dismissed" to see
  them again) and has an inline form to fix the title/author.
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
  persist across pagination. A tag with no explicit classification falls back to a heuristic guess
  (see `app/epub_meta.py`): `_guess_fandoms` for Fandom, or the "/" or "&" convention between names
  for Relationship. The Fandom and Relationship tabs (and the Fandom-assignment dropdown on Classify
  Tags below) show a heuristically guessed tag exactly like a confirmed one -- it's already being
  treated as real everywhere else in the app (Downloads filtering, the fandom-assignment dropdown),
  so hiding it under "Unclassified" until someone clicks a button would just be a stale, second
  answer to the same question. A guessed one is marked "(guessed)" so it's clear nobody's actually
  looked at it yet. Character has no such guess -- it's only ever explicit, since there's no reliable
  way to tell a Character tag from a Fandom or a Freeform tag by pattern alone -- and Freeform is the
  bare "nothing else matched" fallback with no positive signal of its own, so both stay
  explicit-classification-only: "Unclassified" specifically means neither confirmed nor guessed,
  keeping it a real review queue rather than shrinking to nothing now that every leftover tag already
  defaults to Freeform internally. The relationship guess can misfire on an ordinary tag that happens
  to use the same punctuation (e.g. "Hurt/Comfort") -- that's exactly what Classify Tags below is for
  fixing.
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
  by association instead of the same-category hierarchy -- pick Fandom, Character, Relationship, or
  Freeform (mutually exclusive, "None" restores the normal same-category nesting) and every tag on
  the list is nested under its resolved Fandom/linked Character(s)/Relationship(s)/Freeform tag(s) as
  a synthetic parent row instead of its same-category parent. This is how you see, say, the
  Relationship tab grouped by Fandom, or the Freeform tab grouped by the Characters each tag is
  linked to -- Freeform is the reverse of that last one: it groups Character/Relationship/Fandom tags
  under every Freeform tag that links back to them, since a Freeform tag has no "parent Freeform tag"
  association of its own to organize its own tab by (only the same-category hierarchy already covers
  that, under "None") -- organizing the Freeform tab by Freeform leaves everything standalone rather
  than grouping by nothing. A tag with more than one association for the chosen dimension (a Freeform
  tag linked to two Characters, or a Character linked from two different Freeform tags) appears once
  under each parent; a tag with none stays a standalone top-level row rather than disappearing. On
  the read-only Tags page, clicking a synthetic parent heading filters Downloads by that association
  directly (e.g. a Fandom heading links like the Fandoms page does), not by whatever category the
  heading's own name happens to also be classified as.

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
  since you downloaded it, across all feeds, sorted with not-downloaded first. Select any rows and
  click "Download Selected" to actually fetch them, in the background, so a big batch (hundreds or
  thousands of works) runs on its own while you do something else in the app -- see "Optional:
  downloading from within the app" above for how that works and what it needs. Successes and
  failures land on Home and Issues exactly like a manual ao3downloader run would, since it's driving
  the same underlying download logic. A "paste links" box above the table lets you add specific
  works straight to this same queue with no feed involved -- paste one or more AO3 work URLs (one
  per line, or however they come out of a copy-paste) and click "Add to Queue"; each one shows up as
  "Manually added" (with a &times; to retract it before it downloads) and drops out on its own once
  it's on disk, exactly like a tracked-feed row would.
- **Incomplete Works** (`/incomplete`) -- every already-downloaded work that's still a WIP, across
  your whole library, not just ones tracked through a feed (that's what Queue is for). Instead of a
  "From feed" column it shows Last Updated -- when you last downloaded/refreshed that file locally,
  since a plain epub carries no AO3-side "last updated" date of its own -- sorted oldest first, so a
  WIP nobody's redownloaded in a long time (and might quietly have new chapters waiting) surfaces at
  the top. "Download Selected" here queues a fresh redownload of the checked works, sharing the same
  background worker and queue as the Queue page above.

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
- The downloads folder and log file are mounted read-write, since the Queue page's "Download
  Selected" (see "Optional: downloading from within the app" above) writes new `.epub` files and
  log entries into them, the same as a manual ao3downloader run would -- nothing here ever modifies
  or deletes an existing file, only adds new ones.
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
