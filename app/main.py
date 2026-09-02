import asyncio
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from fast_autocomplete import AutoComplete
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import ao3_client, audiobookshelf, auth, catalog_import, db, epub_reader, rss, scanner
from .epub_meta import looks_like_relationship

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
# Optional: a second, read-only location this app never writes to itself --
# e.g. files obtained manually or synced in from elsewhere. Scanned in
# addition to DOWNLOAD_DIR (see scanner.scan_raw's extra_dirs), using the
# exact same "<id> or <id>_" filename convention. Blank (the default)
# disables it entirely.
MANUAL_DOWNLOAD_DIR = os.environ.get("MANUAL_DOWNLOAD_DIR", "")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
FEEDS_DB_PATH = os.environ.get("FEEDS_DB_PATH", "/data/feeds.sqlite")
AUTO_REFRESH_INTERVAL_SECONDS = int(os.environ.get("AUTO_REFRESH_INTERVAL_SECONDS", 60 * 60))

DOWNLOADS_PAGE_SIZE = 25
TAGS_PAGE_SIZE = 100
FACET_SUGGESTION_COUNT = 10

# The zone this app's own naive timestamps (file mtimes, its own
# datetime.now() calls) are actually recorded in -- defaults to UTC, this
# container's own default clock, if unset. Each account's own *display*
# timezone (see /account/timezone) is a completely separate setting; this
# only matters for correctly converting *from* whatever zone the underlying
# recorded times are really in.
SERVER_TZ_NAME = os.environ.get("TZ", "UTC")
TIMEZONE_OPTIONS = sorted(available_timezones())

# Optional: only needed for restricted/mature-locked-behind-login works or
# a logged-in user's own reading history -- a queue of ordinary public
# works downloads fine with both left blank. See app/ao3_client.py.
AO3_USERNAME = os.environ.get("AO3_USERNAME", "")
AO3_PASSWORD = os.environ.get("AO3_PASSWORD", "")
AO3_EXTRA_WAIT_SECONDS = int(os.environ.get("AO3_EXTRA_WAIT_SECONDS", ao3_client.DEFAULT_EXTRA_WAIT_SECONDS))

# Optional: link downloaded works to their Audiobookshelf copy, if any.
# All three unset (the default) disables the integration entirely.
ABS_DB_PATH = os.environ.get("ABS_DB_PATH", "")
ABS_LIBRARY_ID = os.environ.get("ABS_LIBRARY_ID", "")
ABS_BASE_URL = os.environ.get("ABS_BASE_URL", "")

LAST_REFRESHED_KEY = "last_refreshed_at"
FEEDS_LAST_REFRESHED_KEY = "feeds_last_refreshed_at"

BASE_DIR = os.path.dirname(__file__)

app = FastAPI(title="AO3 Downloads Viewer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


async def _auto_refresh_loop():
    """Periodically refreshes only feeds with auto-refresh enabled. Waits
    a full interval before the first run, so restarting/redeploying the
    container doesn't itself trigger a fetch against every tracked feed.
    """
    while True:
        await asyncio.sleep(AUTO_REFRESH_INTERVAL_SECONDS)
        await asyncio.to_thread(rss.refresh_auto_feeds, FEEDS_DB_PATH)


DOWNLOAD_WORKER_CURRENT_KEY = "download_worker_current_title"
AO3_LOGIN_ERROR_KEY = "ao3_login_error"

_download_worker_task: asyncio.Task | None = None
_download_worker_stop = asyncio.Event()

# How often the worker re-scans the downloads folder while a batch is
# running, so newly-downloaded/redownloaded files show up on Home/
# Incomplete Works with their real (current) mtime instead of sitting on
# whatever was cached from the last manual Refresh -- otherwise a work
# downloaded mid-batch would keep showing stale info for as long as the
# rest of the queue takes to drain. A full rescan isn't free, so this
# throttles it rather than refreshing after every single item.
DOWNLOAD_WORKER_REFRESH_INTERVAL_SECONDS = 60


def _download_worker_running() -> bool:
    return _download_worker_task is not None and not _download_worker_task.done()


async def _refresh_downloads_cache_safely() -> None:
    """A refresh failure (e.g. a misconfigured Audiobookshelf path) should
    never take down the download worker itself -- the batch just keeps
    going and tries again next interval.
    """
    try:
        await asyncio.to_thread(_refresh_downloads_cache_now)
    except Exception:
        pass


async def _download_worker_loop():
    """Walks db.download_queue's pending items one at a time, calling into
    ao3_client for each -- exits once the queue is drained rather than
    polling forever, since _ensure_download_worker_running restarts it the
    next time something's enqueued. A per-item failure never raises out of
    Ao3.download() itself (it logs its own failure and moves on -- see
    ao3_client's module docstring), so the try/except here is only a
    safety net against a bug in this glue code, not the normal way a bad
    download shows up.
    """
    client = await asyncio.to_thread(
        ao3_client.build_client,
        DOWNLOAD_DIR, LOG_PATH, os.path.dirname(DB_PATH),
        AO3_USERNAME, AO3_PASSWORD, AO3_EXTRA_WAIT_SECONDS,
    )
    if AO3_USERNAME and AO3_PASSWORD:
        # Only touched at all when credentials are actually configured --
        # a fixed password clears a stale warning from an earlier run;
        # nobody using this unauthenticated ever sees this banner.
        db.set_meta(DB_PATH, AO3_LOGIN_ERROR_KEY, client.login_error or "")
    last_refresh = time.monotonic()
    try:
        while not _download_worker_stop.is_set():
            item = db.get_next_pending_download(DB_PATH)
            if item is None:
                break
            db.mark_download_status(DB_PATH, item["id"], "downloading")
            db.set_meta(DB_PATH, DOWNLOAD_WORKER_CURRENT_KEY, item["title"] or item["url"])
            # A fresh filesystem check, not the (possibly stale) scanner
            # cache the Queue/Incomplete Works views that fed this item
            # were built from -- skips a needless redownload (and the risk
            # of a spurious logged failure) of a work that's actually
            # already on disk, in either location. See ao3_client.work_id_on_disk.
            already_on_disk = await asyncio.to_thread(ao3_client.work_id_on_disk, DOWNLOAD_DIR, item["work_id"])
            if not already_on_disk and MANUAL_DOWNLOAD_DIR:
                already_on_disk = await asyncio.to_thread(
                    ao3_client.work_id_on_disk, MANUAL_DOWNLOAD_DIR, item["work_id"], True
                )
            if not already_on_disk:
                try:
                    await asyncio.to_thread(client.download, item["url"])
                except Exception:
                    pass
            db.mark_download_status(DB_PATH, item["id"], "done", datetime.now().isoformat())

            if time.monotonic() - last_refresh >= DOWNLOAD_WORKER_REFRESH_INTERVAL_SECONDS:
                await _refresh_downloads_cache_safely()
                last_refresh = time.monotonic()
    finally:
        # A guaranteed catch-up refresh once the batch ends (drained or
        # stopped) so the last few items -- fewer than a full interval's
        # worth -- don't sit stale until someone happens to click Refresh.
        await _refresh_downloads_cache_safely()
        db.set_meta(DB_PATH, DOWNLOAD_WORKER_CURRENT_KEY, "")
        await asyncio.to_thread(client.close)


def _ensure_download_worker_running():
    global _download_worker_task
    if not _download_worker_running():
        _download_worker_stop.clear()
        _download_worker_task = asyncio.create_task(_download_worker_loop())


CATALOG_IMPORT_STATUS_KEY = "catalog_import_status"  # "" | "running" | "done" | "error"
CATALOG_IMPORT_PROGRESS_KEY = "catalog_import_progress"
CATALOG_IMPORT_ERROR_KEY = "catalog_import_error"
CATALOG_LAST_IMPORTED_KEY = "catalog_last_imported_at"

_catalog_import_task: asyncio.Task | None = None


def _catalog_import_running() -> bool:
    return _catalog_import_task is not None and not _catalog_import_task.done()


def _run_catalog_import(source_db_path: str, table_name: str | None) -> None:
    """Runs synchronously on a worker thread (see _catalog_import_worker) --
    import_from_sqlite streams the source in batches, so this can run
    against a multi-million-row export without blocking the event loop or
    holding the whole thing in memory at once. Status/progress/error are
    all recorded via db.set_meta so /admin/catalog can poll them across
    requests without holding any of this in Python state itself.
    """
    last_update = 0.0

    def progress(imported: int, skipped: int) -> None:
        # Throttled to roughly once every 5 seconds of wall time, not every
        # batch -- a multi-million-row import can mean thousands of
        # batches, and a separate write transaction to app.db per batch
        # was itself a source of lock contention against ordinary page
        # loads (see _connect's own timeout bump for the other half of
        # that fix). The page is only ever checked by a manual refresh
        # anyway, so finer-grained updates than this buy nothing.
        nonlocal last_update
        now = time.monotonic()
        if now - last_update < 5:
            return
        last_update = now
        db.set_meta(DB_PATH, CATALOG_IMPORT_PROGRESS_KEY, f"{imported} imported, {skipped} skipped so far")

    try:
        imported, skipped = catalog_import.import_from_sqlite(
            DB_PATH, source_db_path, table_name, progress_cb=progress
        )
        db.set_meta(DB_PATH, CATALOG_IMPORT_PROGRESS_KEY, f"{imported} imported, {skipped} skipped")
        # Not scanner.rebuild_work_tags -- catalog_works/catalog_work_tags
        # never participate in that pipeline (see scanner.py's own module
        # docstring), so an import can't change any on-disk work's
        # resolved tags. Only the Catalog Browse autocomplete cache
        # (keyed off this) needs to know a fresher import landed.
        db.set_meta(DB_PATH, CATALOG_LAST_IMPORTED_KEY, datetime.now().isoformat())
        db.set_meta(DB_PATH, CATALOG_IMPORT_STATUS_KEY, "done")
    except Exception as exc:
        db.set_meta(DB_PATH, CATALOG_IMPORT_ERROR_KEY, str(exc))
        db.set_meta(DB_PATH, CATALOG_IMPORT_STATUS_KEY, "error")


async def _catalog_import_worker(source_db_path: str, table_name: str | None) -> None:
    await asyncio.to_thread(_run_catalog_import, source_db_path, table_name)


@app.on_event("startup")
async def _startup():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db.init_db(DB_PATH)

    if db.count_users(DB_PATH) == 0:
        # First run: seed one admin account so there's always a way in.
        # Username/password are both "admin" -- change it immediately via
        # the Account page after the first login.
        db.create_user(DB_PATH, "admin", auth.hash_password("admin"), "admin")

    for url, label in db.pop_legacy_tracked_feeds(DB_PATH):
        try:
            rss.add_tracked_feed(FEEDS_DB_PATH, url, label)
        except rss.FeedRefreshError:
            pass  # best-effort; the user can re-add manually if a URL is stale

    if db.get_next_pending_download(DB_PATH) is not None:
        # Items were left queued from before a restart -- pick back up
        # instead of stranding them until someone revisits Queue and
        # re-selects the same rows.
        _ensure_download_worker_running()

    # One-time-per-boot backfill: links any relationship-character slot
    # that already qualifies (both tags classified before this feature
    # existed, or classified independently since) without needing anyone
    # to re-touch either tag -- see set_selected_tags for the same call
    # made reactively as new classifications happen.
    _auto_link_relationship_characters()

    # Ensures work_tags reflects the current works_cache/classification/
    # wrangling/association state right away -- otherwise a container
    # started from an existing DB predating this table would show every
    # work with no resolved tags until the next refresh or classification
    # edit (see scanner.rebuild_work_tags's own callers for the reactive case).
    scanner.rebuild_work_tags(DB_PATH)

    asyncio.create_task(_auto_refresh_loop())


# Everything requires login except these. Within that, a fixed set of
# path prefixes are admin-only: maintenance/setup pages a regular user
# (e.g. a friend given access) should never see, plus tag classification
# (the per-work fandom-picker POST included, matched by its own "/fandom"
# suffix since it lives under the same "/works/{id}/..." prefix as the
# bookmark toggle, which every logged-in user *should* be able to reach).
PUBLIC_PATHS = {"/login"}
ADMIN_PATH_PREFIXES = ("/admin", "/issues", "/tracked", "/queue", "/incomplete", "/refresh", "/tags/classify", "/metatags")


def _is_admin_only_path(path: str) -> bool:
    return path.endswith("/fandom") or any(path.startswith(prefix) for prefix in ADMIN_PATH_PREFIXES)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get("session")
    user = db.get_session_user(DB_PATH, token) if token else None
    if user is None:
        return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)

    if _is_admin_only_path(request.url.path) and not user.is_admin:
        return PlainTextResponse("Forbidden -- admin access required.", status_code=403)

    request.state.user = user
    return await call_next(request)


def human_size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def local_time(value: datetime | str | None, tz_name: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Formats a naive datetime (or an ISO string, e.g. from db.get_meta) in
    a user's chosen display timezone. Every naive value in this app was
    recorded on this container's own clock (see SERVER_TZ_NAME) -- that's
    the "from" zone; tz_name (an IANA name, or None/blank for "no
    conversion, show the server's own time as-is") is the "to" zone, set
    per-account under Account. Falls back to the untouched server time if
    tz_name doesn't resolve to a real zone.
    """
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if not tz_name:
        return dt.strftime(fmt)
    try:
        target_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return dt.strftime(fmt)
    return dt.replace(tzinfo=ZoneInfo(SERVER_TZ_NAME)).astimezone(target_tz).strftime(fmt)


templates.env.filters["human_size"] = human_size
templates.env.filters["format_number"] = lambda n: f"{n:,}" if n is not None else ""
templates.env.filters["local_time"] = local_time
templates.env.filters["effective_timestamp"] = scanner.effective_timestamp

# AO3-style blurb icons/tags for the Downloads page (see dashboard.html),
# colors/symbols matched against AO3's own "Symbols we use on the Archive"
# reference guide rather than guessed. The 4th (completion/WIP) icon AO3
# shows always renders as its "status unknown" blank state here, since
# chapter counts aren't tracked for plain downloads (only for tracked feed
# entries, a separate data path -- see rss.py/queue) -- that's a real AO3
# state, not a placeholder.
_RATING_CLASSES = {
    "Not Rated": "notrated",
    "General Audiences": "general",
    "Teen And Up Audiences": "teen",
    "Mature": "mature",
    "Explicit": "explicit",
}
_RATING_SYMBOLS = {
    "General Audiences": "G", "Teen And Up Audiences": "T", "Mature": "M", "Explicit": "E",
}
_CATEGORY_CLASSES = {
    "Gen": "gen", "F/M": "fm", "M/M": "mm", "F/F": "ff", "Other": "other",
}
_CATEGORY_SYMBOLS = {
    "Gen": "⊙",  # circled dot -- AO3's "no romantic/sexual relationships" icon
    "F/M": "⚥",  # male and female sign
    "M/M": "♂",  # male sign
    "F/F": "♀",  # female sign
    "Other": "⚭",  # two rings, standing in for AO3's "other relationships" icon
}


def _completion_status(entry) -> str:
    """"complete" | "wip" | "unknown". Chapters come from the epub's own
    preface page (see epub_meta.parse_epub_stats), so this is real, not a
    placeholder: chapters_total is only ever set once the author commits to
    a total, so have < total is still a WIP even with a definite total
    (e.g. "5/12"). Shared by blurb_icons (display) and the Downloads page
    Completion filter, so they can never disagree about a work's status.
    """
    if entry.chapters_have is None:
        return "unknown"
    if entry.chapters_total is not None and entry.chapters_have >= entry.chapters_total:
        return "complete"
    return "wip"


def blurb_icons(entry) -> dict:
    rating_label = entry.rating or "Not Rated"
    rating_class = _RATING_CLASSES.get(entry.rating, "notrated")
    rating_symbol = _RATING_SYMBOLS.get(entry.rating, "")  # blank square, no letter -- matches AO3's Not Rated icon

    if len(entry.categories) > 1:
        category_class, category_label, category_symbol = "multi", "Multi", ""
    elif entry.categories:
        category_class = _CATEGORY_CLASSES.get(entry.categories[0], "other")
        category_label = entry.categories[0]
        category_symbol = _CATEGORY_SYMBOLS.get(entry.categories[0], "⚭")
    else:
        category_class, category_label, category_symbol = "unknown", "No category", ""

    if "Choose Not To Use Archive Warnings" in entry.warnings:
        warning_class, warning_label, warning_symbol = "unstated", "Creator Chose Not To Use Archive Warnings", "!?"
    elif entry.warnings and entry.warnings != ["No Archive Warnings Apply"]:
        warning_class, warning_label, warning_symbol = "yes", ", ".join(entry.warnings), "!"
    elif "No Archive Warnings Apply" in entry.warnings:
        warning_class, warning_label, warning_symbol = "no", "No Archive Warnings Apply", ""
    else:
        warning_class, warning_label, warning_symbol = "no", "Unknown", ""

    completion_status = _completion_status(entry)
    if completion_status == "unknown":
        completion_class, completion_label, completion_symbol = "unknown", "Completion status unknown", ""
    elif completion_status == "complete":
        completion_class, completion_label, completion_symbol = "complete", "Complete", "✓"
    else:
        completion_class, completion_label, completion_symbol = "wip", "Work in Progress", "⊘"

    return {
        "rating_class": rating_class, "rating_label": rating_label, "rating_symbol": rating_symbol,
        "category_class": category_class, "category_label": category_label, "category_symbol": category_symbol,
        "warning_class": warning_class, "warning_label": warning_label, "warning_symbol": warning_symbol,
        "completion_class": completion_class, "completion_label": completion_label, "completion_symbol": completion_symbol,
    }


def blurb_tag_line(entry) -> list[dict]:
    """Warnings, relationships, characters, then freeform/additional tags --
    AO3's own Warning/Relationship/Character/Freeform tag line, now that
    scanner._resolve_tag_categories actually distinguishes characters from
    freeform tags instead of lumping every non-fandom leftover together.

    li_class/param use AO3's own real li-class-plus-a.tag markup convention
    (see a real works-index page's `<li class='characters'><a class="tag"
    href="/tags/.../works">`) rather than this app's own class names, so a
    pasted AO3 skin's tag-category coloring (`.warnings .tag`, `.characters
    .tag`, etc.) applies natively -- no selector translation needed for
    this part. `param` doubles as the actual Downloads filter query param
    for that category, so every tag here is also a working filter link.
    """
    tags = [{"text": w, "li_class": "warnings", "param": "warning"} for w in entry.warnings]
    tags += [{"text": r, "li_class": "relationships", "param": "relationship"} for r in entry.relationships]
    tags += [{"text": c, "li_class": "characters", "param": "character"} for c in entry.characters]
    tags += [{"text": t, "li_class": "freeforms", "param": "freeform"} for t in entry.freeform_tags]
    return tags


templates.env.filters["blurb_icons"] = blurb_icons
templates.env.filters["blurb_tag_line"] = blurb_tag_line


# base.html's own markup now reuses AO3's real ids/classes directly
# (#header, #outer.wrapper, #inner.wrapper, #main, li.blurb, a.tag, dl.stats,
# .warnings/.relationships/.characters/.freeforms, .primary/.navigation/
# .actions/.dropdown on the nav) -- see a real AO3 works-index page's markup
# -- so most of a pasted skin now applies with no translation at all. This
# map only covers AO3 ids/classes with no equivalent here where redirecting
# them onto a stand-in element is still a reasonable approximation.
#
# #dashboard was deliberately dropped from this map after live-testing a
# real skin: it's AO3's small personal-dashboard widget, with its own
# background/border rules (e.g. a gold gradient fill) that make sense on a
# little summary box but not when redirected onto the *entire* main content
# area -- every blurb ended up painted solid gold instead of just bordered,
# which is a worse result than leaving #dashboard rules to simply no-op.
_AO3_SELECTOR_MAP: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?<![\w.#-])\.splash(?![\w-])"), ".blurb-list"),
    (re.compile(r"(?<![\w.#-])#stat_chart(?![\w-])"), ".blurb-ao3-stats"),
]


def translate_ao3_skin_selectors(css: str) -> str:
    """Rewrites the small set of AO3 selectors with no real equivalent here
    onto this app's closest stand-in -- see _AO3_SELECTOR_MAP. Everything
    else in a pasted skin either matches this app's markup directly now, or
    has no equivalent at all and simply does nothing.
    """
    for pattern, replacement in _AO3_SELECTOR_MAP:
        css = pattern.sub(replacement, css)
    return css


templates.env.filters["translate_ao3_skin_selectors"] = translate_ao3_skin_selectors


def sanitize_style_content(css: str) -> str:
    """<style> content is raw CSS text per the HTML spec, not HTML -- the
    browser doesn't decode entities inside it, it just scans for the
    literal closing tag. So the one thing a pasted theme could use to
    break out into real HTML/script is a literal "</style" substring;
    neutralizing just that (rather than HTML-escaping the whole thing,
    which would corrupt valid CSS like `div > p`) is what actually makes
    this safe to render unescaped. Used with the `safe` filter in
    base.html for exactly that reason.
    """
    return re.sub(r"</style", "&lt;/style", css, flags=re.IGNORECASE)


templates.env.filters["sanitize_style_content"] = sanitize_style_content


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "user": request.state.user,
        "theme_css": db.get_active_theme_css(DB_PATH, request.state.user.id),
        "last_refreshed": db.get_meta(DB_PATH, LAST_REFRESHED_KEY),
        "feeds_last_refreshed": db.get_meta(DB_PATH, FEEDS_LAST_REFRESHED_KEY),
        "refresh_error": request.query_params.get("refresh_error"),
        "ao3_login_error": db.get_meta(DB_PATH, AO3_LOGIN_ERROR_KEY) or "",
    }


def _abs_links() -> dict[str, str]:
    """work_id -> Audiobookshelf item URL, empty if the integration isn't
    configured (ABS_BASE_URL unset) regardless of what's cached in abs_matches.
    """
    if not ABS_BASE_URL:
        return {}
    return {
        work_id: audiobookshelf.item_url(ABS_BASE_URL, item_id)
        for work_id, item_id in db.get_all_abs_matches(DB_PATH).items()
    }


def _read_ids(user_id: int) -> tuple[set[str], set[str]]:
    """(abs_read_ids, manually_read_ids) for one user -- kept separate
    rather than pre-merged so the template can tell "Audiobookshelf says
    so" (no toggle, it's not this app's call to override) from "you marked
    it yourself" (toggle stays available) -- see _blurb.html.
    """
    return db.get_abs_read_work_ids(DB_PATH, user_id), db.get_read_marked_work_ids(DB_PATH, user_id)


def paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    """Clamps page into [1, total_pages] and slices items to that page.
    Returns (page_items, clamped_page, total_pages). total_pages is always
    >= 1, even for an empty list, so callers never divide by zero.
    """
    total_pages = max(1, math.ceil(len(items) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages


# Downloads page search/filter. Nine facets funnel through one dict mapping
# a facet name to a function pulling the relevant tag-like values off a
# WorkEntry, so matching/suggestion logic is written once regardless of
# facet kind. "Static" facets (rating/warning/category/completion) have a
# small fixed vocabulary, so every option is always shown with a count.
# "Dynamic" facets (fandom/character/relationship/freeform/language) have
# an unbounded vocabulary (20,000+ tags in a real library), so only
# selected values plus the top FACET_SUGGESTION_COUNT unselected ones are
# shown -- see _facet_suggestions.
FACETS = {
    "rating": lambda e: [e.rating or "Not Rated"],
    "warning": lambda e: list(e.warnings) or ["Unknown"],
    "category": lambda e: list(e.categories) or ["No category"],
    "completion": lambda e: [_completion_status(e)],
    "fandom": lambda e: e.fandoms,
    "character": lambda e: e.characters,
    "relationship": lambda e: e.relationships,
    "freeform": lambda e: e.freeform_tags,
    "language": lambda e: [e.language] if e.language else [],
}

STATIC_FACET_DEFS = {
    "rating": ("Rating", [(v, v) for v in ["Not Rated", "General Audiences", "Teen And Up Audiences", "Mature", "Explicit"]]),
    "warning": ("Warning", [(v, v) for v in ["No Archive Warnings Apply", "Choose Not To Use Archive Warnings", "Graphic Depictions Of Violence", "Major Character Death", "Rape/Non-Con", "Underage", "Unknown"]]),
    "category": ("Category", [(v, v) for v in ["Gen", "F/M", "M/M", "F/F", "Multi", "Other", "No category"]]),
    "completion": ("Completion", [("complete", "Complete"), ("wip", "Work in Progress"), ("unknown", "Unknown")]),
}
DYNAMIC_FACET_LABELS = {
    "fandom": "Fandom",
    "character": "Character",
    "relationship": "Relationship",
    "freeform": "Additional Tags",
    "language": "Language",
}

# Real AO3's Exclude section mirrors Include for these seven facets only --
# Completion and Language have no Exclude equivalent there (they live under
# "More Options" instead). Derived from the Include defs, not hand-written,
# so they can't drift out of sync with them.
EXCLUDE_FACETS = ("rating", "warning", "category", "fandom", "character", "relationship", "freeform")
EXCLUDE_STATIC_FACET_DEFS = {k: v for k, v in STATIC_FACET_DEFS.items() if k in EXCLUDE_FACETS}
EXCLUDE_DYNAMIC_FACET_LABELS = {k: v for k, v in DYNAMIC_FACET_LABELS.items() if k in EXCLUDE_FACETS}

# The three dynamic facets a tag can be fandom-scoped for -- Fandom itself
# isn't scoped to another fandom by this mechanism, and Language has its
# own small fixed-ish vocabulary that isn't tag-wrangling territory.
FANDOM_SCOPED_FACETS = ("character", "relationship", "freeform")

SORT_OPTIONS = {
    "title": lambda e: (e.title or "").lower(),
    "author": lambda e: (e.author or "").lower(),
    "word_count_desc": lambda e: -(e.word_count or 0),
    "word_count_asc": lambda e: (e.word_count or 0),
    "newest": lambda e: scanner.effective_timestamp(e) or datetime.min,
}
# word_count_desc fakes "descending" by negating its own sort key instead
# (a plain number, unlike a datetime, negates safely) -- "newest" can't do
# that (datetime.min.timestamp() overflows), so it's the one SORT_OPTIONS
# entry that needs an actual reverse=True at the call site below.
DESCENDING_SORTS = {"newest"}
def _series_sort_key(entry) -> float:
    """Numeric-aware ordering for the series view (see /series/{series_name}
    below) -- AO3's own series page lists works "Part 1, 2, ... 10" in that
    numeric order, not lexicographic ("1", "10", "2", ...). A work with no
    recorded position (or a non-numeric one) sorts to the end rather than
    erroring.
    """
    try:
        return float(entry.series_index)
    except (TypeError, ValueError):
        return float("inf")


SORT_LABELS = {
    "title": "Title (A–Z)",
    "author": "Author (A–Z)",
    "word_count_desc": "Word Count (High to Low)",
    "word_count_asc": "Word Count (Low to High)",
    "newest": "Date Downloaded (Newest)",
}
DEFAULT_SORT = "title"

# Typeahead search for the four unbounded tag facets (Language isn't
# included -- its vocabulary is small enough that every value is already
# shown). Built lazily and cached per facet, keyed by the last refresh
# time, so a keystroke is a cheap in-memory lookup rather than rebuilding
# the index from 20,000+ tags on every request -- only the first search
# after a refresh pays that cost.
TAG_SEARCH_FACETS = ("fandom", "character", "relationship", "freeform")
_RELATIONSHIP_SPLIT_RE = re.compile(r"\s*[/&]\s*")
_autocomplete_cache: dict[str, tuple[str | None, AutoComplete, dict[str, set[str]]]] = {}


def _build_autocomplete_index(values: set[str]) -> tuple[AutoComplete, dict[str, set[str]]]:
    """Indexes each tag by its full text, plus -- for relationship-style
    tags like "Ianto Jones/Jack Harkness" -- each individual party's name,
    so searching "Jack" finds it even though the string doesn't start with
    "Jack" (AutoComplete only matches by prefix of the whole string).
    `word_to_tags` maps an indexed key back to the real tag(s) to select,
    since a search result only ever gives back the key that matched.
    """
    word_to_tags: dict[str, set[str]] = defaultdict(set)
    for tag in values:
        word_to_tags[tag].add(tag)
        for part in _RELATIONSHIP_SPLIT_RE.split(tag):
            part = part.strip()
            if part and part != tag:
                word_to_tags[part].add(tag)
    return AutoComplete(words={key: {} for key in word_to_tags}), word_to_tags


def _get_autocompleter(facet: str) -> tuple[AutoComplete, dict[str, set[str]]]:
    cache_key = db.get_meta(DB_PATH, LAST_REFRESHED_KEY)
    cached_key, cached_ac, cached_index = _autocomplete_cache.get(facet, (None, None, None))
    if cached_ac is not None and cached_key == cache_key:
        return cached_ac, cached_index

    entries = scanner.load_cached(DB_PATH).entries
    values = {v for entry in entries for v in FACETS[facet](entry)}
    autocompleter, word_to_tags = _build_autocomplete_index(values)
    _autocomplete_cache[facet] = (cache_key, autocompleter, word_to_tags)
    return autocompleter, word_to_tags


def _search_facet_tags(facet: str, q: str, limit: int = 20, active_fandoms: set[str] | None = None) -> list[str]:
    q = q.strip()
    if facet not in TAG_SEARCH_FACETS or len(q) < 2:
        return []
    autocompleter, word_to_tags = _get_autocompleter(facet)
    results = autocompleter.search(word=q, max_cost=2, size=limit * 2)
    matched: set[str] = set()
    for result in results:
        key = " ".join(result) if isinstance(result, list) else result
        matched.update(word_to_tags.get(key, ()))
    if active_fandoms and facet in FANDOM_SCOPED_FACETS:
        # Same "unscoped or scoped to a currently-included fandom" rule as
        # the top-10 suggestions (_facet_suggestions) -- otherwise typing
        # into "Find another..." would be the one way left to slip a
        # different fandom's tag past the scoping.
        scope = _build_fandom_scope(db.get_tag_children(DB_PATH), db.get_all_tag_fandoms(DB_PATH))
        matched = {tag for tag in matched if not scope.get(tag) or scope[tag] in active_fandoms}
    return sorted(matched, key=str.lower)[:limit]


CATALOG_BROWSE_PAGE_SIZE = 25
_catalog_autocomplete_cache: dict[str, tuple[str | None, AutoComplete, dict[str, set[str]]]] = {}


def _get_catalog_autocompleter(category: str) -> tuple[AutoComplete, dict[str, set[str]]]:
    """Same cache-by-last-change-timestamp pattern as _get_autocompleter,
    just sourced from db.get_catalog_tag_values (bounded by how many
    distinct tags exist, not how many catalog works reference them) instead
    of scanning every WorkEntry -- rebuilt once per catalog import, not
    once per request, however large catalog_works itself grows.
    """
    cache_key = db.get_meta(DB_PATH, CATALOG_LAST_IMPORTED_KEY)
    cached_key, cached_ac, cached_index = _catalog_autocomplete_cache.get(category, (None, None, None))
    if cached_ac is not None and cached_key == cache_key:
        return cached_ac, cached_index

    values = db.get_catalog_tag_values(DB_PATH, category)
    autocompleter, word_to_tags = _build_autocomplete_index(values)
    _catalog_autocomplete_cache[category] = (cache_key, autocompleter, word_to_tags)
    return autocompleter, word_to_tags


def _search_catalog_tags(category: str, q: str, limit: int = 20) -> list[str]:
    q = q.strip()
    if category not in catalog_import.CATALOG_TAG_KINDS or len(q) < 2:
        return []
    autocompleter, word_to_tags = _get_catalog_autocompleter(category)
    results = autocompleter.search(word=q, max_cost=2, size=limit * 2)
    matched: set[str] = set()
    for result in results:
        key = " ".join(result) if isinstance(result, list) else result
        matched.update(word_to_tags.get(key, ()))
    return sorted(matched, key=str.lower)[:limit]


def _all_descendants(value: str, children: dict[str, set[str]]) -> set[str]:
    """Every tag reachable by following 'child' wrangling edges downward
    from `value`, at any depth -- not just its direct children. E.g. for
    Fandom "Harry Potter" with Relationship "Harry Potter/Hermione
    Granger" wrangled under it, and Characters "Harry Potter"/"Hermione
    Granger" wrangled under *that* relationship, this returns all three,
    not just the relationship. Safe against a cycle even though
    db.set_tag_wrangling already refuses to create one, since a node
    already visited is never re-expanded.
    """
    result: set[str] = set()
    stack = list(children.get(value, ()))
    while stack:
        node = stack.pop()
        if node in result:
            continue
        result.add(node)
        stack.extend(children.get(node, ()))
    return result


def _build_fandom_scope(children: dict[str, set[str]], explicit_fandoms: dict[str, str]) -> dict[str, str]:
    """tag -> its resolved Fandom association (see db.set_tag_fandom /
    scanner.resolve_tag_fandom), for every Character/Relationship/Freeform
    tag whose own same-category 'child' chain resolves to something other
    than "No Fandom". Used to keep a fandom-specific tag (e.g. "The
    Doctor", associated with the Fandom "Doctor Who") out of a *different*
    fandom's Character/Relationship/Additional-Tags suggestions and "Find
    another..." search results -- a tag that resolves to "No Fandom" (no
    association anywhere in its own chain) has no scope here and stays
    universally suggestible, same as an unwrangled tag like "Coffee Shops".

    Unlike the old chain-walk this replaces, "which fandom does this tag
    belong to" is no longer inferred by climbing the same-category
    hierarchy looking for a Fandom node -- Fandom is its own explicit,
    inheritable association (db.tag_fandoms), completely separate from
    same-category parent/child. See scanner.resolve_tag_fandom, the same
    resolution scanner already applies to fold associated fandoms into
    entry.fandoms.
    """
    parent_of = scanner.child_parent_map(children)
    scope = {}
    for tag in set(parent_of) | set(explicit_fandoms):
        fandom = scanner.resolve_tag_fandom(tag, parent_of, explicit_fandoms)
        if fandom != "No Fandom":
            scope[tag] = fandom
    return scope


def _value_or_children_present(value: str, entry_values: set[str], children: dict[str, set[str]]) -> bool:
    """True if `value` itself is one of entry_values, or if entry_values
    contains any descendant of `value` at any depth (see
    db.get_all_tag_descendants -- `children` here is expected to
    already be the transitive-closure map, not raw direct edges) -- a
    descendant keeps its own name/category, but filtering by an ancestor
    should match it too, same as real AO3 wrangling (searching "Alternate
    Reality" also surfaces works only tagged with "Alternate Reality -
    Canon Divergence", however many wrangling hops separate the two).
    """
    if value in entry_values:
        return True
    return bool(children.get(value)) and not entry_values.isdisjoint(children[value])


def _entry_matches(entry, filters: dict, skip_include: str | None = None, skip_exclude: str | None = None) -> bool:
    """Include is AND across facets AND within a facet's selected values
    (matches real AO3: checking both "M/M" and "F/F" means only works with
    both). Exclude is OR within a facet (matching ANY excluded value drops
    the work) and AND across facets, same as real AO3. `skip_include`/
    `skip_exclude` each skip one facet's own Include/Exclude constraint --
    used to build that facet's own suggestion/count list from what
    everything *else* currently matches; they're independent since a facet
    can have both an active Include and an active Exclude at once.

    `filters["children"]` (parent tag -> set of ALL descendants at any
    depth, from db.get_all_tag_descendants; absent/empty
    when there's no wrangling) expands each selected value to also match a
    work tagged with any of that value's descendants -- see
    _value_or_children_present.
    """
    children = filters.get("children") or {}
    for name, values in filters["facets"].items():
        if name == skip_include or not values:
            continue
        entry_values = set(FACETS[name](entry))
        if not all(_value_or_children_present(v, entry_values, children) for v in values):
            return False
    for name, values in filters["exclude"].items():
        if name == skip_exclude or not values:
            continue
        entry_values = set(FACETS[name](entry))
        if any(_value_or_children_present(v, entry_values, children) for v in values):
            return False
    if filters["word_min"] is not None and (entry.word_count or 0) < filters["word_min"]:
        return False
    if filters["word_max"] is not None and (entry.word_count or 0) > filters["word_max"]:
        return False
    if filters["crossover"] == "only" and len(entry.fandoms) < 2:
        return False
    if filters["crossover"] == "exclude" and len(entry.fandoms) >= 2:
        return False
    if filters["date_from"] is not None or filters["date_to"] is not None:
        ts = scanner.effective_timestamp(entry)
        entry_date = ts.date() if ts is not None else None
        if filters["date_from"] is not None and (entry_date is None or entry_date < filters["date_from"]):
            return False
        if filters["date_to"] is not None and (entry_date is None or entry_date > filters["date_to"]):
            return False
    if filters["q"]:
        q = filters["q"].lower()
        haystacks = [entry.title, entry.author, entry.summary, *entry.fandoms, *entry.characters, *entry.relationships, *entry.freeform_tags]
        if not any(q in (h or "").lower() for h in haystacks):
            return False
    return True


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_filters(request: Request) -> dict:
    qp = request.query_params
    return {
        "facets": {name: qp.getlist(name) for name in FACETS},
        "exclude": {name: qp.getlist(f"x_{name}") for name in EXCLUDE_FACETS},
        "word_min": int(qp["word_min"]) if qp.get("word_min", "").isdigit() else None,
        "word_max": int(qp["word_max"]) if qp.get("word_max", "").isdigit() else None,
        "crossover": qp.get("crossover") if qp.get("crossover") in ("only", "exclude") else None,
        "date_from": _parse_date(qp.get("date_from")),
        "date_to": _parse_date(qp.get("date_to")),
        "bookmarked": qp.get("bookmarked") == "true",
        "unread": qp.get("unread") == "true",
        "q": qp.get("q", "").strip(),
        "sort": qp.get("sort") or DEFAULT_SORT,
    }


def _skip_kwargs(name: str, mode: str) -> dict:
    return {"skip_include": name} if mode == "facets" else {"skip_exclude": name}


def _facet_suggestions(entries: list, filters: dict, name: str, mode: str = "facets", top_n: int = FACET_SUGGESTION_COUNT) -> list[tuple[str, int]]:
    """Top `top_n` values for this facet (Include values if mode="facets",
    Exclude values if mode="exclude"), excluding whatever's already
    selected, counted against entries matching every *other* active filter
    (so narrowing a different facet updates the suggestions, but selecting
    more values within this same facet/direction doesn't shrink its own list).

    For the three fandom-scoped facets (see FANDOM_SCOPED_FACETS), a
    candidate that's wrangled to a *different*, unselected fandom (per
    filters["fandom_scope"]) is dropped from suggestions entirely -- e.g.
    once Fandom "Harry Potter" is selected (Include), "The Doctor" (scoped
    to "Doctor Who") never shows up as a suggested Character, while an
    unscoped tag like "Coffee Shops" still does. This only looks at the
    currently *included* fandom(s), regardless of whether these are
    Include or Exclude suggestions being built -- both describe the same
    "what fandom am I browsing" context.
    """
    selected = set(filters[mode][name])
    active_fandoms = set(filters["facets"].get("fandom", ())) if name in FANDOM_SCOPED_FACETS else set()
    scope = filters.get("fandom_scope", {})
    counts: Counter = Counter()
    for entry in entries:
        if _entry_matches(entry, filters, **_skip_kwargs(name, mode)):
            for v in FACETS[name](entry):
                if v in selected:
                    continue
                if active_fandoms and scope.get(v) and scope[v] not in active_fandoms:
                    continue
                counts[v] += 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top_n]


def _selected_with_counts(entries: list, filters: dict, name: str, mode: str = "facets") -> list[tuple[str, int]]:
    selected = filters[mode][name]
    counts: Counter = Counter()
    for entry in entries:
        if _entry_matches(entry, filters, **_skip_kwargs(name, mode)):
            counts.update(v for v in FACETS[name](entry) if v in selected)
    return [(v, counts.get(v, 0)) for v in selected]


def _static_facet_counts(entries: list, filters: dict, name: str, options: list[tuple[str, str]], mode: str = "facets") -> list[tuple[str, str, int, bool]]:
    """Every fixed option for this facet, always shown, each with a live
    count (against entries matching every other active filter) and whether
    it's currently selected. Returns (value, label, count, checked).
    """
    counts: Counter = Counter()
    for entry in entries:
        if _entry_matches(entry, filters, **_skip_kwargs(name, mode)):
            counts.update(FACETS[name](entry))
    selected = set(filters[mode][name])
    return [(value, label, counts.get(value, 0), value in selected) for value, label in options]


def _filter_query_string(filters: dict, *, drop_key: str | None = None, drop_value: str | None = None) -> str:
    """Query string (leading '&', empty if no filters active) reflecting
    every currently active filter except `page` -- used for pagination
    links and, with drop_key/drop_value set, for a single active filter
    chip's "remove just this one" link. `drop_key` for a facet is the raw
    query param name -- an Include facet's own name (e.g. "fandom") or an
    Exclude facet's `x_`-prefixed name (e.g. "x_fandom").
    """
    parts = []
    for name, values in filters["facets"].items():
        for v in values:
            if name == drop_key and v == drop_value:
                continue
            parts.append(f"{name}={quote(v)}")
    for name, values in filters["exclude"].items():
        for v in values:
            if f"x_{name}" == drop_key and v == drop_value:
                continue
            parts.append(f"x_{name}={quote(v)}")
    for key in ("q", "word_min", "word_max", "date_from", "date_to"):
        if filters[key] and drop_key != key:
            parts.append(f"{key}={quote(str(filters[key]))}")
    if filters["crossover"] and drop_key != "crossover":
        parts.append(f"crossover={quote(filters['crossover'])}")
    if filters["bookmarked"] and drop_key != "bookmarked":
        parts.append("bookmarked=true")
    if filters["unread"] and drop_key != "unread":
        parts.append("unread=true")
    if filters["sort"] != DEFAULT_SORT and drop_key != "sort":
        parts.append(f"sort={quote(filters['sort'])}")
    return ("&" + "&".join(parts)) if parts else ""


def _active_chips(filters: dict) -> list[dict]:
    """One entry per currently-set filter value, for the "Filtered by: ..."
    summary line -- each links back to a URL with just that one value removed.
    """
    chips = []
    for name, values in filters["facets"].items():
        label = STATIC_FACET_DEFS[name][0] if name in STATIC_FACET_DEFS else DYNAMIC_FACET_LABELS[name]
        for value in values:
            chips.append({
                "text": f"{label}: {value}",
                "remove_href": "/?" + _filter_query_string(filters, drop_key=name, drop_value=value).lstrip("&"),
            })
    for name, values in filters["exclude"].items():
        label = STATIC_FACET_DEFS[name][0] if name in STATIC_FACET_DEFS else DYNAMIC_FACET_LABELS[name]
        for value in values:
            chips.append({
                "text": f"Exclude {label}: {value}",
                "remove_href": "/?" + _filter_query_string(filters, drop_key=f"x_{name}", drop_value=value).lstrip("&"),
            })
    if filters["q"]:
        chips.append({"text": f'Search: "{filters["q"]}"', "remove_href": "/?" + _filter_query_string(filters, drop_key="q").lstrip("&")})
    if filters["word_min"] is not None:
        chips.append({"text": f"Words ≥ {filters['word_min']:,}", "remove_href": "/?" + _filter_query_string(filters, drop_key="word_min").lstrip("&")})
    if filters["word_max"] is not None:
        chips.append({"text": f"Words ≤ {filters['word_max']:,}", "remove_href": "/?" + _filter_query_string(filters, drop_key="word_max").lstrip("&")})
    if filters["date_from"] is not None:
        chips.append({"text": f"Downloaded on/after {filters['date_from']}", "remove_href": "/?" + _filter_query_string(filters, drop_key="date_from").lstrip("&")})
    if filters["date_to"] is not None:
        chips.append({"text": f"Downloaded on/before {filters['date_to']}", "remove_href": "/?" + _filter_query_string(filters, drop_key="date_to").lstrip("&")})
    if filters["crossover"] == "only":
        chips.append({"text": "Crossovers only", "remove_href": "/?" + _filter_query_string(filters, drop_key="crossover").lstrip("&")})
    elif filters["crossover"] == "exclude":
        chips.append({"text": "No crossovers", "remove_href": "/?" + _filter_query_string(filters, drop_key="crossover").lstrip("&")})
    if filters["bookmarked"]:
        chips.append({"text": "Bookmarked only", "remove_href": "/?" + _filter_query_string(filters, drop_key="bookmarked").lstrip("&")})
    if filters["unread"]:
        chips.append({"text": "Unread only", "remove_href": "/?" + _filter_query_string(filters, drop_key="unread").lstrip("&")})
    return chips


def _build_filter_panel(entries: list, filters: dict) -> dict:
    return {
        "q": filters["q"],
        "word_min": filters["word_min"],
        "word_max": filters["word_max"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "crossover": filters["crossover"],
        "bookmarked": filters["bookmarked"],
        "unread": filters["unread"],
        "sort": filters["sort"],
        "sort_options": SORT_LABELS,
        "searchable_facets": TAG_SEARCH_FACETS,
        "static_include": {
            name: {"label": label, "options": _static_facet_counts(entries, filters, name, options, mode="facets")}
            for name, (label, options) in STATIC_FACET_DEFS.items()
        },
        "dynamic_include": {
            name: {
                "label": label,
                "selected": _selected_with_counts(entries, filters, name, mode="facets"),
                "suggestions": _facet_suggestions(entries, filters, name, mode="facets"),
            }
            for name, label in DYNAMIC_FACET_LABELS.items()
        },
        "static_exclude": {
            name: {"label": label, "options": _static_facet_counts(entries, filters, name, options, mode="exclude")}
            for name, (label, options) in EXCLUDE_STATIC_FACET_DEFS.items()
        },
        "dynamic_exclude": {
            name: {
                "label": label,
                "selected": _selected_with_counts(entries, filters, name, mode="exclude"),
                "suggestions": _facet_suggestions(entries, filters, name, mode="exclude"),
            }
            for name, label in EXCLUDE_DYNAMIC_FACET_LABELS.items()
        },
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, page: int = 1):
    result = scanner.load_cached(DB_PATH)
    filters = _parse_filters(request)
    raw_children = db.get_tag_children(DB_PATH)
    filters["children"] = db.get_all_tag_descendants(DB_PATH)
    filters["fandom_scope"] = _build_fandom_scope(raw_children, db.get_all_tag_fandoms(DB_PATH))
    # Facet suggestion/count computation needs the *whole* library, not the
    # already-filtered list below -- each facet's own counts are computed
    # by re-filtering result.entries excluding just that one facet (see
    # _build_filter_panel), which only works against the unfiltered set.
    filter_panel = _build_filter_panel(result.entries, filters)
    active_chips = _active_chips(filters)

    bookmarked_ids = db.get_bookmarked_work_ids(DB_PATH, request.state.user.id)
    bookmark_notes = db.get_bookmark_notes(DB_PATH, request.state.user.id)
    abs_read_ids, read_marked_ids = _read_ids(request.state.user.id)

    entries = [e for e in result.entries if _entry_matches(e, filters)]
    if filters["bookmarked"]:
        entries = [e for e in entries if e.work_id in bookmarked_ids]
    if filters["unread"]:
        entries = [e for e in entries if e.work_id not in abs_read_ids and e.work_id not in read_marked_ids]
    entries.sort(
        key=SORT_OPTIONS.get(filters["sort"], SORT_OPTIONS[DEFAULT_SORT]),
        reverse=filters["sort"] in DESCENDING_SORTS,
    )

    page_entries, page, total_pages = paginate(entries, page, DOWNLOADS_PAGE_SIZE)

    pager_qs = _filter_query_string(filters)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_base_context(request),
            "entries": page_entries,
            "bookmarked_ids": bookmarked_ids,
            "bookmark_notes": bookmark_notes,
            "abs_read_ids": abs_read_ids,
            "read_marked_ids": read_marked_ids,
            "filter_panel": filter_panel,
            "active_chips": active_chips,
            "abs_links": _abs_links(),
            "page": page,
            "total_pages": total_pages,
            "total_filtered": len(entries),
            "pager_qs": pager_qs,
            "home_edit_source": request.state.user.is_admin and db.get_user_home_edit_source(DB_PATH, request.state.user.id),
        },
    )


@app.get("/series/{series_name}", response_class=HTMLResponse)
def series_view(request: Request, series_name: str):
    """A local stand-in for AO3's own series page -- this app has no AO3
    series id to link to (series membership comes from Audiobookshelf's own
    series/bookSeries tables, see audiobookshelf.py, which don't carry
    AO3's id), so clicking a work's series line stays inside this app and
    lists every other downloaded work in the same series, in series order.
    """
    result = scanner.load_cached(DB_PATH)
    entries = [e for e in result.entries if e.series == series_name]
    entries.sort(key=_series_sort_key)

    bookmarked_ids = db.get_bookmarked_work_ids(DB_PATH, request.state.user.id)
    bookmark_notes = db.get_bookmark_notes(DB_PATH, request.state.user.id)
    abs_read_ids, read_marked_ids = _read_ids(request.state.user.id)

    return templates.TemplateResponse(
        "series.html",
        {
            **_base_context(request),
            "series_name": series_name,
            "entries": entries,
            "bookmarked_ids": bookmarked_ids,
            "bookmark_notes": bookmark_notes,
            "abs_read_ids": abs_read_ids,
            "read_marked_ids": read_marked_ids,
            "abs_links": _abs_links(),
        },
    )


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, page: int = 1):
    """Every work this account has clicked into before (see /go/{work_id}
    and the reader routes, both of which call db.record_view), most
    recently viewed first. One row per work_id no matter how many times
    it's been opened -- record_view always upserts to the latest time.
    """
    result = scanner.load_cached(DB_PATH)
    view_times = {wid: datetime.fromisoformat(ts) for wid, ts in db.get_view_history(DB_PATH, request.state.user.id).items()}
    entries = [e for e in result.entries if e.work_id in view_times]
    entries.sort(key=lambda e: view_times[e.work_id], reverse=True)

    page_entries, page, total_pages = paginate(entries, page, DOWNLOADS_PAGE_SIZE)

    bookmarked_ids = db.get_bookmarked_work_ids(DB_PATH, request.state.user.id)
    bookmark_notes = db.get_bookmark_notes(DB_PATH, request.state.user.id)
    abs_read_ids, read_marked_ids = _read_ids(request.state.user.id)

    return templates.TemplateResponse(
        "history.html",
        {
            **_base_context(request),
            "entries": page_entries,
            "view_times": view_times,
            "bookmarked_ids": bookmarked_ids,
            "bookmark_notes": bookmark_notes,
            "abs_read_ids": abs_read_ids,
            "read_marked_ids": read_marked_ids,
            "abs_links": _abs_links(),
            "page": page,
            "total_pages": total_pages,
        },
    )


def _entry_by_work_id(work_id: str):
    return next((e for e in scanner.load_cached(DB_PATH).entries if e.work_id == work_id), None)


@app.get("/reader/{work_id}", response_class=HTMLResponse)
def read_work(work_id: str):
    """Jumps straight to the first real chapter (index 1) rather than the
    preface (index 0, just Words/Chapters stats) -- the preface is still
    reachable from there via "Previous" or the chapter-jump dropdown.
    """
    return RedirectResponse(url=f"/reader/{work_id}/1")


@app.get("/reader/{work_id}/{chapter_index}", response_class=HTMLResponse)
def read_chapter(request: Request, work_id: str, chapter_index: int):
    """An in-browser reading fallback over a work's own downloaded epub --
    see app/epub_reader.py. Not admin-gated: reading isn't an editing
    action, so any logged-in user who can see a work on Home/Issues can
    open it here.
    """
    entry = _entry_by_work_id(work_id)
    if entry is None or not entry.on_disk or not entry.file_path:
        return templates.TemplateResponse(
            "reader.html", {**_base_context(request), "work_id": work_id, "error": "missing"}, status_code=404
        )

    chapters = epub_reader.list_chapters(entry.file_path)
    if not chapters:
        return templates.TemplateResponse(
            "reader.html",
            {**_base_context(request), "work_id": work_id, "entry": entry, "error": "unreadable"},
            status_code=422,
        )

    if chapter_index < 0 or chapter_index >= len(chapters):
        return RedirectResponse(url=f"/reader/{work_id}/{max(0, min(chapter_index, len(chapters) - 1))}")

    db.record_view(DB_PATH, request.state.user.id, work_id, datetime.now().isoformat())
    abs_read_ids, read_marked_ids = _read_ids(request.state.user.id)

    chapter = chapters[chapter_index]
    chapter_html = epub_reader.get_chapter_html(entry.file_path, work_id, chapter)
    return templates.TemplateResponse(
        "reader.html",
        {
            **_base_context(request),
            "work_id": work_id,
            "entry": entry,
            "chapters": chapters,
            "chapter": chapter,
            "chapter_html": chapter_html,
            "prev_index": chapter_index - 1 if chapter_index > 0 else None,
            "next_index": chapter_index + 1 if chapter_index < len(chapters) - 1 else None,
            "abs_read_ids": abs_read_ids,
            "read_marked_ids": read_marked_ids,
        },
    )


@app.get("/go/{work_id}")
def go_to_ao3(request: Request, work_id: str):
    """Every "title" link on Home/Issues for a work not yet downloaded
    routes through here instead of straight to AO3, purely so a click
    still lands in History (see /history) the same way opening the
    in-app reader already does for a work that's on disk.
    """
    db.record_view(DB_PATH, request.state.user.id, work_id, datetime.now().isoformat())
    return RedirectResponse(url=f"https://archiveofourown.org/works/{work_id}", status_code=302)


@app.get("/reader/{work_id}/{chapter_index}/asset/{asset_path:path}")
def read_chapter_asset(work_id: str, chapter_index: int, asset_path: str):
    entry = _entry_by_work_id(work_id)
    if entry is None or not entry.file_path:
        return Response(status_code=404)
    found = epub_reader.get_asset_bytes(entry.file_path, asset_path)
    if found is None:
        return Response(status_code=404)
    content, content_type = found
    return Response(content=content, media_type=content_type)


@app.post("/works/{work_id}/bookmark")
def toggle_bookmark(request: Request, work_id: str, bookmarked: bool = Form(...), next: str = Form("/")):
    """Bookmarking is per-user, not admin-gated -- any logged-in user can
    mark works for themselves. Unlike the fandom-picker POST, this doesn't
    touch the shared tag classification at all.
    """
    if bookmarked:
        db.add_bookmark(DB_PATH, request.state.user.id, work_id, datetime.now().isoformat())
    else:
        db.remove_bookmark(DB_PATH, request.state.user.id, work_id)
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/works/{work_id}/bookmark/note")
def set_bookmark_note(request: Request, work_id: str, note: str = Form(""), next: str = Form("/")):
    db.set_bookmark_note(DB_PATH, request.state.user.id, work_id, note)
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/works/{work_id}/read")
def toggle_read(request: Request, work_id: str, read: bool = Form(...), next: str = Form("/")):
    """Manual read/unread toggle, per-user like bookmarking. This never
    touches abs_read_status -- if Audiobookshelf already reports a work
    finished for this account, it stays shown as read regardless of what
    happens here (see _blurb.html, which hides this toggle in that case
    rather than pretending it can override Audiobookshelf).
    """
    if read:
        db.add_read_mark(DB_PATH, request.state.user.id, work_id, datetime.now().isoformat())
    else:
        db.remove_read_mark(DB_PATH, request.state.user.id, work_id)
    return RedirectResponse(url=next or "/", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    result = scanner.load_cached(DB_PATH)
    return templates.TemplateResponse(
        "admin.html",
        {
            **_base_context(request),
            "stats": result.stats,
            "download_dir": DOWNLOAD_DIR,
            "manual_download_dir": MANUAL_DOWNLOAD_DIR,
            "log_path": LOG_PATH,
            "log_exists": os.path.isfile(LOG_PATH),
            "home_edit_source": db.get_user_home_edit_source(DB_PATH, request.state.user.id),
        },
    )


@app.post("/admin/home_edit_source")
def set_home_edit_source(request: Request, enabled: bool = Form(False)):
    """Per-user, not global -- each admin decides for themselves whether
    Home shows an "Edit" shortcut on every blurb (see _blurb.html), same
    as the per-user Theme/Audiobookshelf-username settings on Account.
    """
    db.set_user_home_edit_source(DB_PATH, request.state.user.id, enabled)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/tags/search")
def tag_search(request: Request, facet: str, q: str = ""):
    """Typeahead endpoint backing the Downloads page's per-facet "Find
    another..." box -- see _search_facet_tags. Used to reach any of a
    library's 20,000+ tags, not just the top-10 suggestions already shown.
    `fandom` (repeatable) is whatever Include-Fandom checkboxes are
    currently checked client-side, passed through so results stay scoped
    the same way the suggestion list already is.
    """
    active_fandoms = set(request.query_params.getlist("fandom"))
    return JSONResponse(_search_facet_tags(facet, q, active_fandoms=active_fandoms))


@app.get("/issues", response_class=HTMLResponse)
def issues(request: Request, show_dismissed: bool = False):
    result = scanner.load_cached(DB_PATH)
    issue_entries = [e for e in result.entries if e.issue_type]
    if not show_dismissed:
        issue_entries = [e for e in issue_entries if not e.dismissed]
    return templates.TemplateResponse(
        "issues.html",
        {
            **_base_context(request),
            "entries": issue_entries,
            "show_dismissed": show_dismissed,
            "abs_links": _abs_links(),
        },
    )


@app.post("/issues/{work_id}/dismiss")
def dismiss_issue(work_id: str, dismissed: bool = Form(...)):
    db.set_dismissed(DB_PATH, work_id, dismissed)
    return RedirectResponse(url="/issues", status_code=303)


@app.post("/issues/{work_id}/edit")
def edit_issue(work_id: str, title: str = Form(""), author: str = Form("")):
    db.set_title_author(DB_PATH, work_id, title.strip() or None, author.strip() or None)
    return RedirectResponse(url="/issues", status_code=303)


@app.post("/works/{work_id}/fandom")
def set_work_fandom(
    work_id: str,
    fandoms: list[str] = Form([]),
    other_fandoms: str = Form(""),
    next: str = Form("/"),
):
    """Classifies tags globally (see db.set_tag_categories), scoped to this
    work's own candidate tags plus whatever the user typed in "other" --
    it looks like a per-work edit, but the effect applies to every work
    that shares the same tag, since classification is per tag, not per work.
    Unchecking a candidate here marks it Freeform, not Character or
    Relationship -- this widget is a quick per-work fandom shortcut, not
    the full 4-way tool (see the Tags page for those). Relationship-shaped
    candidates aren't even shown as checkboxes here (see _fandom_picker.html)
    but are still present in `candidates`, so they're explicitly skipped
    when defaulting the rest to Freeform -- otherwise saving this form would
    silently overwrite every relationship tag on the work to Freeform.
    """
    by_id = {e.work_id: e for e in scanner.load_cached(DB_PATH).entries}
    entry = by_id.get(work_id)
    candidates = entry.fandom_candidates if entry else []

    checked = set(fandoms)
    categories = {
        tag: ("fandom" if tag in checked else "freeform")
        for tag in candidates
        if tag in checked or not looks_like_relationship(tag)
    }
    for extra in (f.strip() for f in other_fandoms.split(",")):
        if extra:
            categories[extra] = "fandom"

    db.set_tag_categories(DB_PATH, categories)
    scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(url=next or "/", status_code=303)


DEFAULT_NAME_COUNT_SORT = "count_desc"
NAME_COUNT_SORT_LABELS = {
    "name_asc": "Name (A-Z)",
    "name_desc": "Name (Z-A)",
    "count_desc": "Most Works",
    "count_asc": "Fewest Works",
}


def _sort_name_count_rows(rows: list[tuple], sort: str) -> list[tuple]:
    """Sorts a list of (name, count, ...) tuples for any of the Tags/Browse/
    Fandoms pages -- shared since they're all the same (name, count) shape
    underneath. Name sorts have no tiebreak (names are unique, one row per
    tag/fandom); count sorts always tiebreak ascending by name, regardless
    of count direction, so equal-count rows land in a stable, predictable
    order rather than flipping with the primary sort.
    """
    if sort == "name_desc":
        return sorted(rows, key=lambda row: row[0].lower(), reverse=True)
    if sort == "count_asc":
        return sorted(rows, key=lambda row: (row[1], row[0].lower()))
    if sort == "count_desc":
        return sorted(rows, key=lambda row: (-row[1], row[0].lower()))
    return sorted(rows, key=lambda row: row[0].lower())  # name_asc, and the default


LETTER_FILTER_OPTIONS = ["all"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]

# AO3's own real Fandom media-type vocabulary (its Fandoms page's own
# category list), reused verbatim rather than inventing this app's own --
# a Fandom tag's own explicit choice(s) among these (see
# db.set_tag_media_types -- a Fandom can belong to more than one) only
# makes sense once it's been explicitly classified Fandom, not merely
# guessed (see the Classify Tags "(guessed)" hint).
FANDOM_MEDIA_TYPES = [
    "Anime & Manga",
    "Books & Literature",
    "Cartoons & Comics & Graphic Novels",
    "Celebrities & Real People",
    "Movies",
    "Music & Bands",
    "Other Media",
    "Theater",
    "TV Shows",
    "Video Games",
    "Uncategorized Fandoms",
]


def _filter_by_letter(rows: list[tuple], letter: str) -> list[tuple]:
    """Narrows (name, count, ...) rows to those starting with the given
    uppercase letter; "#" catches names starting with anything else (a
    digit, punctuation, etc.), "all" is a no-op.
    """
    if letter == "all":
        return rows
    if letter == "#":
        return [row for row in rows if not row[0][:1].isalpha()]
    return [row for row in rows if row[0][:1].upper() == letter]


def _filter_by_media_type(
    rows: list[tuple], media_type: str, parent_of: dict[str, str], explicit_media_types: dict[str, set[str]]
) -> list[tuple]:
    """Narrows (fandom_name, count, ...) rows to those whose *resolved*
    media types (scanner.resolve_tag_media_type, so a same-category child
    inheriting its parent's explicit choice still counts) include this
    one -- a Fandom belonging to more than one category (e.g. both
    "Movies" and "Comics") passes every one of its tabs' filters, not
    just one; "all" is a no-op. A row whose direct parent gets filtered
    out here (none of its own categories match) still falls back to its
    own top-level row rather than disappearing -- see _group_tag_rows_by_parent.
    """
    if media_type == "all":
        return rows
    return [row for row in rows if media_type in scanner.resolve_tag_media_type(row[0], parent_of, explicit_media_types)]


def _add_virtual_parent_counts(counts: Counter, entries: list, tags_of, children_map: dict[str, set[str]]) -> None:
    """Mutates `counts` in place to add an entry for each wrangling parent
    that isn't itself a real tag on any work -- a "consolidated" parent an
    admin created purely to group existing descendants under (see
    db.set_tag_wrangling; the target of a 'child' wrangling never has to
    already exist as a literal tag). `children_map` is expected to already
    be the transitive-closure map (see db.get_all_tag_descendants), so
    a virtual grandparent's count reaches descendants at any depth, not
    just its direct children. The count itself is the number of distinct
    works matching ANY descendant -- the same "parent or any descendant"
    mechanism Downloads filtering already uses, see
    _value_or_children_present -- not a sum of the descendants' own
    counts, so a work with two of the same parent's descendants isn't
    double-counted. A parent that's already a real tag is left alone; its
    count stays its own literal count, same as before wrangling existed.
    """
    for parent, children in children_map.items():
        if parent in counts:
            continue
        matching_ids = {e.work_id for e in entries if set(tags_of(e)) & children}
        if matching_ids:
            counts[parent] = len(matching_ids)


def _tag_rows(
    result, filter: str, sort: str, restrict_to: set[str] | None = None, q: str = ""
) -> tuple[list[tuple[str, int, str | None]], dict[str, int], int]:
    """Returns (tags, bucket_counts, total_tags) for the given filter tab --
    tags is (tag, work_count, category). Shared by the admin classification
    page and the read-only Browse page: same underlying data, one mutable
    (checkboxes/bulk actions), one not.

    `restrict_to`, when given, narrows the whole library down to just
    these tag names before bucketing/filtering -- used by Classify Tags'
    "edit source" view (a single work's own fandom_candidates) so the
    filter tabs and their counts describe just that work's tags instead
    of the whole library.

    `q`, when given, keeps only tags containing it (case-insensitive) --
    and, deliberately, searches the *whole* library regardless of the
    current filter tab (a search is one library-wide lookup, not a smaller
    search within whatever tab happens to be open) rather than narrowing
    an already-tab-filtered list further. Not reflected in bucket_counts
    either (those stay whole-library totals, same "always the full
    picture" convention the Fandoms page's media-type tab counts already
    use). Unlike the page's own client-side "filter tags on this page"
    box, this narrows the *whole* library before pagination, so a search
    actually reaches every page instead of just whichever one happens to
    be open -- the reason this parameter exists at all.

    `category` is a tag's effective Fandom/Relationship bucket, not just
    its explicit one: a heuristically guessed Fandom or Relationship
    (scanner._resolve_tag_categories, folded into entry.fandoms/
    entry.relationships) counts the same as an explicitly confirmed one,
    since it's already being treated as real everywhere else in the app
    (the per-row Fandom-assignment dropdown, Downloads filtering) --
    showing it as "Unclassified" here instead would just be a different,
    stale answer to the same question. Character has no such guess (it's
    only ever explicit), and Freeform is the bare "nothing else matched"
    fallback with no positive signal of its own, so both stay
    explicit-classification-only -- that keeps "Unclassified" meaningful
    as an actual review queue (what the bulk "mark as Freeform" actions
    sweep) instead of shrinking to nothing now that every leftover tag
    already defaults to Freeform internally. Callers wanting to tell a
    guess apart from a confirmed choice (to show a "(guessed)" marker)
    should cross-reference the tag against db.get_all_tag_categories
    themselves -- a tag whose returned category isn't in that dict got it
    from a guess.
    """
    counts: Counter = Counter()
    for entry in result.entries:
        for tag in entry.fandom_candidates:
            counts[tag] += 1
    _add_virtual_parent_counts(
        counts, result.entries, lambda e: e.fandom_candidates, db.get_all_tag_descendants(DB_PATH)
    )
    if restrict_to is not None:
        counts = Counter({tag: count for tag, count in counts.items() if tag in restrict_to})

    explicit = db.get_all_tag_categories(DB_PATH)
    guessed_fandoms = {f for e in result.entries for f in e.fandoms}
    guessed_relationships = {r for e in result.entries for r in e.relationships}

    def effective_category(tag: str) -> str | None:
        if tag in explicit:
            return explicit[tag]
        if tag in guessed_fandoms:
            return "fandom"
        if tag in guessed_relationships:
            return "relationship"
        return None

    bucket_counts = {"fandom": 0, "character": 0, "relationship": 0, "freeform": 0, "unclassified": 0}
    for tag in counts:
        bucket_counts[effective_category(tag) or "unclassified"] += 1

    tags = [(tag, count, effective_category(tag)) for tag, count in counts.items()]
    if q:
        q_lower = q.lower()
        tags = [(t, c, cat) for t, c, cat in tags if q_lower in t.lower()]
    elif filter != "all":
        tags = [(t, c, cat) for t, c, cat in tags if (cat or "unclassified") == filter]
    tags = _sort_name_count_rows(tags, sort)
    return tags, bucket_counts, len(counts)


def _group_tag_rows_by_parent(
    tags: list[tuple[str, int, str | None]], children_map: dict[str, set[str]]
) -> list[dict]:
    """Groups an already-filtered-and-sorted (tag, count, category) list
    into a tree of {"tag", "count", "category", "children"} rows, each
    "children" entry the same shape -- so a real multi-level hierarchy
    (e.g. Fandom -> Relationship -> Character, see db.set_tag_wrangling)
    renders as real nesting, not just one level (Fandoms has no per-tag
    category, so it passes `category=None` for every row). Pagination
    then paginates the returned top-level list only (one page slot per
    top-level row; a collapsed parent's descendants ride along "for free"
    since they're hidden by default), so a heavily-childed tag doesn't
    blow a page's row budget regardless of how deep its own tree goes.

    A tag only nests under its direct parent when that parent is also
    present in `tags` (the same filtered set) -- e.g. the current filter
    tab excludes the parent's category. Otherwise it falls back to its own
    top-level row instead of silently disappearing. Children keep the
    relative order they already have in `tags` (whatever sort is active),
    not a separate re-sort.
    """
    by_tag = {tag: (tag, count, category) for tag, count, category in tags}
    parent_of = {child: parent for parent, children in children_map.items() for child in children}

    children_by_parent: dict[str, list[tuple[str, int, str | None]]] = defaultdict(list)
    top_level: list[tuple[str, int, str | None]] = []
    for row in tags:
        tag = row[0]
        parent = parent_of.get(tag)
        if parent is not None and parent in by_tag:
            children_by_parent[parent].append(row)
        else:
            top_level.append(row)

    def to_row(row: tuple[str, int, str | None]) -> dict:
        tag, count, category = row
        return {
            "tag": tag,
            "count": count,
            "category": category,
            "children": [to_row(child_row) for child_row in children_by_parent.get(tag, [])],
        }

    return [to_row(row) for row in top_level]


ORGANIZE_BY_OPTIONS = ("fandom", "character", "relationship", "freeform", "media_type")
ORGANIZE_BY_LABELS = {
    "fandom": "Fandom", "character": "Character", "relationship": "Relationship", "freeform": "Freeform",
    "media_type": "Fandom Category",
}
# The read-only Tags page turns a synthetic group heading into a link that
# filters Downloads by that same association (e.g. "/?fandom=..."), but the
# Downloads page has no "media_type" facet of its own to filter by -- so
# that option is admin-only (Classify Tags), left out of the dropdown here
# to avoid offering a heading that would just dead-end.
ORGANIZE_BY_LABELS_BROWSE = {k: v for k, v in ORGANIZE_BY_LABELS.items() if k != "media_type"}


def _association_parents(
    tag: str,
    dimension: str,
    tag_fandoms: dict[str, str],
    parent_of: dict[str, str],
    relationship_characters: dict[str, dict[int, str]],
    freeform_characters: dict[str, set[str]],
    freeform_relationships: dict[str, set[str]],
    character_freeform_tags: dict[str, set[str]],
    relationship_freeform_tags: dict[str, set[str]],
    category: str | None = None,
    explicit_media_types: dict[str, set[str]] | None = None,
) -> list[str]:
    """The "parent(s)" `tag` belongs to for the given Organize-by
    dimension -- used to regroup a Tags-page listing by an association
    (see db.tag_fandoms / relationship_characters / freeform_characters /
    freeform_relationships) instead of the same-category wrangling
    hierarchy. Fandom is always at most one value (see
    scanner.resolve_tag_fandom_explicit); Character/Relationship/Freeform
    can be several (a Relationship's own per-name-part Characters plus,
    for a Freeform tag, however many it's linked to; or, for Freeform,
    every Freeform tag that links back to this Character/Relationship)
    or none at all if `tag` has no association for this dimension (e.g.
    organizing by Relationship while looking at a Character tag).

    "media_type" (AO3's own "Fandom Category" -- Anime & Manga, Books &
    Literature, etc, see db.tag_media_types) can be several too, since a
    single Fandom can genuinely belong to more than one AO3-style category
    (a franchise spanning both "Movies" and "Comics", say) -- a tag with
    two resolved categories appears under both headings, same as a
    Freeform tag linked to two Characters does. A Fandom-category tag
    (`category == "fandom"`) resolves its own media type(s) directly; any
    other tag resolves through its associated Fandom first (same lookup
    the "fandom" dimension above uses) and then that Fandom's media
    type(s), so grouping the Character/Relationship/Freeform tabs by
    Fandom Category groups them the way their Fandom is grouped.
    `category` and `explicit_media_types` are only needed for this
    dimension -- every other branch below ignores them.

    Freeform is the reverse of Character/Relationship: a Freeform tag has
    no "parent Freeform tag" of its own (there's no such association --
    only the same-category hierarchy, which is what Organize-by "None"
    already shows), so organizing the Freeform tab itself by Freeform
    always leaves everything standalone. What it's for is organizing the
    *other* tabs -- e.g. Character by Freeform groups each Character
    under every Freeform tag that links to it, the mirror image of
    Freeform-organized-by-Character. `character_freeform_tags`/
    `relationship_freeform_tags` are that reverse index, precomputed once
    by the caller (see _group_tag_rows_by_association) rather than
    re-scanning every Freeform tag's links per tag here.

    A tag resolving to "No Fandom" is only grouped under a "No Fandom"
    heading when that's an *explicit* choice (on the tag itself or an
    ancestor) -- a tag nobody's classified either way yet stays
    standalone instead of being swept into that heading alongside tags
    someone deliberately marked as having none.
    """
    if dimension == "fandom":
        fandom, is_explicit = scanner.resolve_tag_fandom_explicit(tag, parent_of, tag_fandoms)
        if fandom == "No Fandom":
            return ["No Fandom"] if is_explicit else []
        return [fandom]
    if dimension == "character":
        chars = set(relationship_characters.get(tag, {}).values()) | freeform_characters.get(tag, set())
        return sorted(chars)
    if dimension == "relationship":
        return sorted(freeform_relationships.get(tag, set()))
    if dimension == "freeform":
        freeforms = character_freeform_tags.get(tag, set()) | relationship_freeform_tags.get(tag, set())
        return sorted(freeforms)
    if dimension == "media_type":
        explicit_media_types = explicit_media_types or {}
        if category != "fandom":
            fandom, fandom_is_explicit = scanner.resolve_tag_fandom_explicit(tag, parent_of, tag_fandoms)
            if not fandom_is_explicit or fandom == "No Fandom":
                return []
            tag = fandom
        media_types, is_explicit = scanner.resolve_tag_media_type_explicit(tag, parent_of, explicit_media_types)
        if media_types == {"Uncategorized Fandoms"} and not is_explicit:
            return []
        return sorted(media_types)
    return []


def _group_tag_rows_by_association(
    tags: list[tuple[str, int, str | None]],
    dimension: str,
    tag_fandoms: dict[str, str],
    parent_of: dict[str, str],
    relationship_characters: dict[str, dict[int, str]],
    freeform_characters: dict[str, set[str]],
    freeform_relationships: dict[str, set[str]],
    sort: str,
    explicit_media_types: dict[str, set[str]] | None = None,
) -> list[dict]:
    """Regroups an already-filtered (tag, count, category) list by
    Organize-by `dimension` (see _association_parents) instead of the
    same-category wrangling hierarchy _group_tag_rows_by_parent builds --
    e.g. organizing the Relationship tab by Fandom nests each relationship
    under its resolved Fandom as a heading. A tag with no association for
    this dimension stays its own top-level row instead of disappearing; a
    tag with multiple parents (a Freeform tag linked to two Characters)
    appears once under each one. Groups and standalone tags are then
    merged into one list and sorted together by `sort`, same as any other
    Tags-page listing, rather than groups always coming first.

    `explicit_media_types` is only used by the "media_type" dimension
    (see _association_parents) -- every other dimension ignores it, so
    callers not exercising that dimension can leave it unset.
    """
    character_freeform_tags: dict[str, set[str]] = defaultdict(set)
    relationship_freeform_tags: dict[str, set[str]] = defaultdict(set)
    for freeform_tag, chars in freeform_characters.items():
        for character_tag in chars:
            character_freeform_tags[character_tag].add(freeform_tag)
    for freeform_tag, rels in freeform_relationships.items():
        for relationship_tag in rels:
            relationship_freeform_tags[relationship_tag].add(freeform_tag)

    groups: dict[str, list[tuple[str, int, str | None]]] = defaultdict(list)
    ungrouped: list[tuple[str, int, str | None]] = []
    for row in tags:
        parents = _association_parents(
            row[0], dimension, tag_fandoms, parent_of, relationship_characters, freeform_characters,
            freeform_relationships, character_freeform_tags, relationship_freeform_tags,
            row[2], explicit_media_types,
        )
        if not parents:
            ungrouped.append(row)
        for parent in parents:
            groups[parent].append(row)

    # Children are carried alongside each summary tuple itself (a 4th
    # element `_sort_name_count_rows` simply ignores, since it only reads
    # indices 0/1) rather than looked back up by name afterward -- a
    # group's name can collide with an unrelated real tag of the same name
    # (e.g. organizing an unfiltered "All" list by Fandom puts a synthetic
    # "Harry Potter" heading next to the real "Harry Potter" fandom tag),
    # and a name-keyed lookup would have wrongly attached the group's
    # children to that unrelated tag too.
    summaries: list[tuple[str, int, str | None, list]] = [
        (
            parent,
            sum(r[1] for r in rows),
            None,
            [{"tag": t, "count": c, "category": cat, "children": []} for t, c, cat in rows],
        )
        for parent, rows in groups.items()
    ]
    summaries.extend((tag, count, category, []) for tag, count, category in ungrouped)
    ordered = _sort_name_count_rows(summaries, sort)

    return [
        {"tag": name, "count": count, "category": category, "children": children}
        for name, count, category, children in ordered
    ]


def _grouped_tag_rows(tags: list[tuple[str, int, str | None]], organize_by: str, sort: str) -> list[dict]:
    """Chooses the same-category wrangling nesting (default) or an
    Organize-by association grouping (fandom/character/relationship/
    freeform/media_type, see _group_tag_rows_by_association), whichever
    the caller asked for -- shared by both Tags pages so they group
    identically.
    """
    if organize_by in ORGANIZE_BY_OPTIONS:
        parent_of = scanner.child_parent_map(db.get_tag_children(DB_PATH))
        return _group_tag_rows_by_association(
            tags, organize_by, db.get_all_tag_fandoms(DB_PATH), parent_of,
            db.get_all_relationship_characters(DB_PATH), db.get_all_freeform_characters(DB_PATH),
            db.get_all_freeform_relationships(DB_PATH), sort, db.get_all_tag_media_types(DB_PATH),
        )
    return _group_tag_rows_by_parent(tags, db.get_tag_children(DB_PATH))


@app.get("/tags", response_class=HTMLResponse)
def tags_browse(
    request: Request,
    filter: str = "all",
    page: int = 1,
    sort: str = DEFAULT_NAME_COUNT_SORT,
    letter: str = "all",
    organize_by: str = "",
    q: str = "",
):
    """Read-only tag browsing under Browse -- anyone logged in can see
    this, unlike /tags/classify (admin-only, see the module-level
    ADMIN_PATH_PREFIXES) which actually changes the shared classification.
    """
    result = scanner.load_cached(DB_PATH)
    tags, bucket_counts, total_tags = _tag_rows(result, filter, sort, q=q)
    tags = _filter_by_letter(tags, letter)
    tags = _grouped_tag_rows(tags, organize_by, sort)
    page_tags, page, total_pages = paginate(tags, page, TAGS_PAGE_SIZE)
    return templates.TemplateResponse(
        "tags_browse.html",
        {
            **_base_context(request),
            "tags": page_tags,
            "filter": filter,
            "page": page,
            "total_pages": total_pages,
            "bucket_counts": bucket_counts,
            "total_tags": total_tags,
            "sort": sort,
            "sort_options": NAME_COUNT_SORT_LABELS,
            "letter": letter,
            "letter_options": LETTER_FILTER_OPTIONS,
            "organize_by": organize_by,
            "organize_by_options": ORGANIZE_BY_LABELS_BROWSE,
            "explicit_categories": db.get_all_tag_categories(DB_PATH),
            "q": q,
            "pager_qs": f"&filter={quote(filter)}&sort={quote(sort)}&letter={quote(letter)}&organize_by={quote(organize_by)}&q={quote(q)}",
        },
    )


@app.get("/tags/classify", response_class=HTMLResponse)
def tags_classify_page(
    request: Request,
    filter: str = "all",
    page: int = 1,
    sort: str = DEFAULT_NAME_COUNT_SORT,
    organize_by: str = "",
    work_id: str = "",
    q: str = "",
    show_guessed: bool = False,
    show_set: bool = False,
    incomplete_only: bool = False,
):
    """`work_id`, when set (see the Home page's "Edit" button, gated by
    the "Use Home as edit source" account setting), narrows this whole
    page down to just that one work's own tags -- restricting _tag_rows
    to its fandom_candidates rather than the whole library -- so an admin
    can jump straight from a specific fic on Home to classifying/
    associating just its own tags, without hunting for them in a
    library-wide list. Filter/sort/organize-by still apply on top of that
    narrowed set. A work_id that doesn't match any entry (a stale link)
    leaves edit_source_entry None so the template can say so instead of
    silently showing the whole library.

    `show_guessed`/`show_set` (both off by default) and `incomplete_only`
    narrow the tag list further, same "on top of everything else, not
    reflected in bucket_counts" treatment `q` already gets -- see
    _matches_classification_source and _is_tag_complete for what each one
    means.
    """
    result = scanner.load_cached(DB_PATH)
    edit_source_entry = next((e for e in result.entries if e.work_id == work_id), None) if work_id else None
    restrict_to = set(edit_source_entry.fandom_candidates) if edit_source_entry else None
    tags, bucket_counts, total_tags = _tag_rows(result, filter, sort, restrict_to, q)

    explicit_categories = db.get_all_tag_categories(DB_PATH)
    tag_fandoms = db.get_all_tag_fandoms(DB_PATH)
    tag_media_types = db.get_all_tag_media_types(DB_PATH)
    relationship_characters = db.get_all_relationship_characters(DB_PATH)
    freeform_characters = db.get_all_freeform_characters(DB_PATH)
    freeform_relationships = db.get_all_freeform_relationships(DB_PATH)
    children_map = db.get_tag_children(DB_PATH)
    parent_of = scanner.child_parent_map(children_map)
    verified_tags = db.get_all_verified_tags(DB_PATH)

    if show_guessed or show_set:
        tags = [
            (t, c, cat) for t, c, cat in tags
            if _matches_classification_source(cat, t in explicit_categories, show_guessed, show_set)
        ]
    if incomplete_only:
        tags = [
            (t, c, cat) for t, c, cat in tags
            if not _is_tag_complete(
                t, cat, t in children_map and bool(children_map[t]), t in parent_of,
                tag_fandoms, tag_media_types, relationship_characters, freeform_characters, freeform_relationships,
                verified_tags,
            )
        ]

    tags = _grouped_tag_rows(tags, organize_by, sort)
    page_tags, page, total_pages = paginate(tags, page, TAGS_PAGE_SIZE)
    return templates.TemplateResponse(
        "tags.html",
        {
            **_base_context(request),
            "tags": page_tags,
            "filter": filter,
            "page": page,
            "total_pages": total_pages,
            "bucket_counts": bucket_counts,
            "total_tags": total_tags,
            "sort": sort,
            "sort_options": NAME_COUNT_SORT_LABELS,
            "organize_by": organize_by,
            "organize_by_options": ORGANIZE_BY_LABELS,
            "explicit_categories": explicit_categories,
            "wranglings": db.get_all_tag_wranglings(DB_PATH),
            "tag_fandoms": tag_fandoms,
            "tag_media_types": tag_media_types,
            "media_type_options": FANDOM_MEDIA_TYPES,
            "known_fandoms": _flatten_tag_options(sorted({f for e in result.entries for f in e.fandoms}), children_map),
            "known_characters": _flatten_tag_options(sorted({c for e in result.entries for c in e.characters}), children_map),
            "known_relationships": _flatten_tag_options(sorted({r for e in result.entries for r in e.relationships}), children_map),
            "relationship_characters": relationship_characters,
            "freeform_characters": freeform_characters,
            "freeform_relationships": freeform_relationships,
            "relationship_name_parts": _relationship_name_parts,
            "verified_tags": verified_tags,
            "work_id": work_id,
            "edit_source_entry": edit_source_entry,
            "q": q,
            "show_guessed": show_guessed,
            "show_set": show_set,
            "incomplete_only": incomplete_only,
            "pager_qs": (
                f"&filter={quote(filter)}&sort={quote(sort)}&organize_by={quote(organize_by)}&work_id={quote(work_id)}&q={quote(q)}"
                f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
            ),
        },
    )


def _effective_tag_category(entries: list, tag: str, explicit_categories: dict[str, str] | None = None) -> str | None:
    """The category `tag` behaves as somewhere in the library -- checking
    entry.fandoms/characters/relationships/freeform_tags membership
    (already resolved, explicit-or-heuristic, see
    scanner._resolve_tag_categories) -- or None if the tag never appears
    as a candidate anywhere yet (a brand-new/virtual tag with no real
    occurrences to judge by). Used to enforce that a 'child' wrangling
    really is same-category, even for a tag nobody's explicitly
    classified on the Tags page.

    explicit_categories (db.get_all_tag_categories(), passed in rather
    than queried here so a caller checking many tags only pays for one
    query) is consulted first when given -- a *purely* virtual tag with
    zero real occurrences (e.g. a wrangling target nobody's ever tagged a
    work with directly) can still have an explicit category of its own
    (see wrangle_tags' auto-classification of a new parent), and the
    entries scan alone could never see that, since it only ever iterates
    a real work's own resolved tag lists. Callers that don't need that
    (nothing here can be a zero-occurrence virtual tag) can omit it and
    keep the cheaper entries-only check.
    """
    if explicit_categories is not None and tag in explicit_categories:
        return explicit_categories[tag]
    for entry in entries:
        if tag in entry.fandoms:
            return "fandom"
        if tag in entry.characters:
            return "character"
        if tag in entry.relationships:
            return "relationship"
        if tag in entry.freeform_tags:
            return "freeform"
    return None


def _shared_child_category(children: set[str], entries: list, explicit_categories: dict[str, str] | None = None) -> str | None:
    """The single category every one of `children` resolves to via
    _effective_tag_category, or None if they're mixed (2+ distinct
    categories) or none of them resolve to one at all. Used by
    wrangle_tags to give an uncategorized parent tag a real category once
    its children agree on one, instead of leaving it ambiguous forever.
    """
    categories = {_effective_tag_category(entries, child, explicit_categories) for child in children} - {None}
    return categories.pop() if len(categories) == 1 else None


@app.post("/tags/classify/wrangle")
def wrangle_tags(
    tags: list[str] = Form([]),
    relation: str = Form(...),
    target: str = Form(""),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Bulk-wrangles every checked tag to `target`. 'synonym' is
    category-blind (two spellings of the same tag aren't a category
    question) and merges the tag into target everywhere. 'child' is a
    same-category hierarchy (a Fandom's parent must be a Fandom, a
    Character's a Character, etc) -- see db.set_tag_wrangling -- so a tag
    whose effective category (_effective_tag_category) differs from
    target's is silently skipped, unless target has no established
    category yet (a brand-new/virtual tag nobody's classified), in which
    case anything can attach to it until it does get one. Each tag is
    otherwise wrangled independently, so one tag failing this check or
    the underlying cycle guard doesn't block the rest of the batch.

    Once a brand-new/virtual target has picked up children this way, it
    doesn't have to stay uncategorized forever: if *every* one of its
    children (this batch plus any it already had) resolves to the same
    category, target is explicitly classified as that category too --
    see the block after the loop. Mixed-category children (only possible
    while target itself has no category to enforce against) leave it
    unclassified, same as before. A Verified tag among those checked is
    skipped entirely (see _unverified) -- its own wrangling relation is
    part of what "Verified" locks. target itself being Verified doesn't
    block *other* tags from being wrangled under it, only the auto-typing
    step at the end, since that's the one thing here that would actually
    change target's own data.
    """
    target = target.strip()
    verified_tags = db.get_all_verified_tags(DB_PATH)
    if relation in ("synonym", "child") and target:
        entries = scanner.load_cached(DB_PATH).entries if relation == "child" else []
        # Fetched once up front (rather than inside _effective_tag_category
        # itself) so checking N tags in the loop below costs one query, not
        # N -- see _effective_tag_category's own docstring for why this is
        # the piece that lets a purely virtual target (zero real
        # occurrences) that this same function typed on an earlier call be
        # recognized as already-categorized on a later one.
        explicit_categories = db.get_all_tag_categories(DB_PATH) if relation == "child" else {}
        target_category = _effective_tag_category(entries, target, explicit_categories) if relation == "child" else None
        for tag in _unverified(tags, verified_tags):
            if relation == "child" and target_category is not None:
                tag_category = _effective_tag_category(entries, tag, explicit_categories)
                if tag_category is not None and tag_category != target_category:
                    continue
            try:
                db.set_tag_wrangling(DB_PATH, tag, relation, target)
            except ValueError:
                pass

        if relation == "child" and target_category is None and target not in verified_tags:
            children = db.get_tag_children(DB_PATH).get(target, set())
            new_category = _shared_child_category(children, entries, explicit_categories)
            if new_category is not None:
                db.set_tag_categories(DB_PATH, {target: new_category})
                if new_category in ("character", "relationship"):
                    _auto_link_relationship_characters()
        scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.get("/tags/classify/search_tags")
def search_classify_tags(q: str = ""):
    """Backs the "Canonical/parent tag name" autocomplete -- a plain
    case-insensitive substring match (not the typo-tolerant, per-facet
    AutoComplete index _search_facet_tags uses, which is keyed to
    Downloads' own five tag-shaped facets) over every tag currently in the
    library, any category, since a merge/child target can be any existing
    tag. Admin-only by virtue of sitting under /tags/classify (see
    ADMIN_PATH_PREFIXES), same as the page it serves.
    """
    q = q.strip().lower()
    if len(q) < 2:
        return JSONResponse([])
    all_tags = {t for e in scanner.load_cached(DB_PATH).entries for t in e.fandom_candidates}
    return JSONResponse(sorted((t for t in all_tags if q in t.lower()), key=str.lower)[:20])


@app.get("/tags/classify/wranglings", response_class=HTMLResponse)
def tag_wranglings_page(request: Request):
    """The full same-category wrangling list (Merged into / Child of),
    split out from the bottom of Classify Tags onto its own page once a
    real library's wrangling list got long enough to be its own scroll --
    the per-tag Fandom/Character/Relationship association controls stay on
    Classify Tags itself, since they need the row's own count/category
    context to make sense.
    """
    return templates.TemplateResponse(
        "tag_wranglings.html",
        {
            **_base_context(request),
            "wranglings": db.get_all_tag_wranglings(DB_PATH),
        },
    )


@app.post("/tags/classify/unwrangle")
def unwrangle_tag(tag: str = Form(...)):
    """Undoes tag's own 'synonym'/'child' relation (see the Tag Wrangling
    page). Refuses on a Verified tag, same as every other classification
    change -- see _unverified.
    """
    if tag not in db.get_all_verified_tags(DB_PATH):
        db.remove_tag_wrangling(DB_PATH, tag)
        scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(url="/tags/classify/wranglings", status_code=303)


def _metatag_children_map(metatags: dict[int, tuple[str, int | None]]) -> dict[int, set[int]]:
    """parent id -> its direct child ids only, inverting get_all_metatags'
    own parent_id column -- same idea as scanner.child_parent_map's
    inverse, just the other direction and over metatag ids instead of
    tag names.
    """
    children: dict[int, set[int]] = defaultdict(set)
    for metatag_id, (_, parent_id) in metatags.items():
        if parent_id is not None:
            children[parent_id].add(metatag_id)
    return dict(children)


def _flatten_metatag_options(metatags: dict[int, tuple[str, int | None]]) -> list[tuple[int, str, int]]:
    """Every metatag ordered into a depth-first walk of the tree -- (id,
    name, depth), for the "Parent" <select> on the create-metatag form.
    Simpler than _flatten_tag_options: every metatag is always in the one
    same set (get_all_metatags returns literally all of them), so there's
    no "parent belongs to a different category" case to reconcile.
    """
    children_by_parent = _metatag_children_map(metatags)

    def sort_key(metatag_id: int) -> str:
        return metatags[metatag_id][0].lower()

    ordered: list[tuple[int, str, int]] = []

    def visit(metatag_id: int, depth: int) -> None:
        ordered.append((metatag_id, metatags[metatag_id][0], depth))
        for child_id in sorted(children_by_parent.get(metatag_id, ()), key=sort_key):
            visit(child_id, depth + 1)

    top_level = [mid for mid, (_, parent_id) in metatags.items() if parent_id is None]
    for metatag_id in sorted(top_level, key=sort_key):
        visit(metatag_id, 0)
    return ordered


def _metatag_tree(metatags: dict[int, tuple[str, int | None]], children_of: dict[int, set[int]]) -> list[dict]:
    """metatags nested into {"id", "name", "children": [...]} rows, top
    level first, siblings sorted alphabetically at each level -- the shape
    metatags.html actually renders, same "dumb template" convention
    _group_tag_rows_by_parent already established for the same-category
    hierarchy display.
    """
    def build(metatag_id: int) -> dict:
        child_ids = sorted(children_of.get(metatag_id, ()), key=lambda i: metatags[i][0].lower())
        return {"id": metatag_id, "name": metatags[metatag_id][0], "children": [build(cid) for cid in child_ids]}

    top_level = [mid for mid, (_, parent_id) in metatags.items() if parent_id is None]
    return [build(mid) for mid in sorted(top_level, key=lambda i: metatags[i][0].lower())]


def _tags_for_metatag(
    metatag_id: int, children_of: dict[int, set[int]], metatag_tags: dict[int, set[str]]
) -> list[tuple[str, int]]:
    """(tag, direct_metatag_id) pairs for metatag_id and every descendant,
    at any depth -- an association at a leaf rolls up through every
    ancestor's own page. `direct_metatag_id` (not just the tag name) rides
    along so the page can label an entry that only shows up here because a
    more specific descendant carries it, and so "remove" always targets
    the tag's real, direct link regardless of which node is being viewed.
    _all_descendants is generic over its key type despite its str-typed
    hint (no string-specific operation in its body) -- reused here as-is
    for metatag ids rather than writing a duplicate int-flavored copy.
    """
    ids = {metatag_id} | _all_descendants(metatag_id, children_of)
    pairs = [(tag, mid) for mid in ids for tag in metatag_tags.get(mid, ())]
    return sorted(pairs, key=lambda pair: pair[0].lower())


@app.get("/metatags", response_class=HTMLResponse)
def metatags_page(request: Request, metatag_id: int | None = None):
    """A second, independent tag hierarchy, cross-category and unrelated
    to Classify Tags' own same-category "child" wrangling -- a purely
    user-built way to group tags of any category under broader themes
    (e.g. filing many specific Freeform "X loves Y" tags somewhere under
    a "Love" branch). Visiting any node shows every tag linked to it or
    to any of its descendants (_tags_for_metatag), not just tags linked
    to that exact node -- an association several levels down still rolls
    all the way up.
    """
    metatags = db.get_all_metatags(DB_PATH)
    children_of = _metatag_children_map(metatags)
    metatag_tags = db.get_all_metatag_tags(DB_PATH)

    selected = metatags.get(metatag_id) if metatag_id is not None else None
    breadcrumb: list[tuple[int, str]] = []
    tags: list[dict] = []
    can_delete = False
    if selected is not None:
        current: int | None = metatag_id
        while current is not None:
            name, parent_id = metatags[current]
            breadcrumb.append((current, name))
            current = parent_id
        breadcrumb.reverse()
        entries = scanner.load_cached(DB_PATH).entries
        tags = [
            {
                "tag": tag,
                "direct_metatag_id": direct_id,
                "direct_metatag_name": metatags[direct_id][0],
                "category": _effective_tag_category(entries, tag),
            }
            for tag, direct_id in _tags_for_metatag(metatag_id, children_of, metatag_tags)
        ]
        can_delete = not children_of.get(metatag_id) and not metatag_tags.get(metatag_id)

    return templates.TemplateResponse(
        "metatags.html",
        {
            **_base_context(request),
            "tree": _metatag_tree(metatags, children_of),
            "metatag_options": _flatten_metatag_options(metatags),
            "metatag_id": metatag_id,
            "selected_name": selected[0] if selected else None,
            "breadcrumb": breadcrumb,
            "tags": tags,
            "can_delete": can_delete,
        },
    )


@app.post("/metatags/create")
def create_metatag_route(name: str = Form(...), parent_id: str = Form("")):
    """Creates a new metatag as a child of parent_id (blank -> top level),
    redirecting to the new node on success or back to the parent (where
    the create form was) on a duplicate/blank name -- see db.create_metatag.
    """
    name = name.strip()
    parent = int(parent_id) if parent_id.strip() else None
    redirect_id = parent
    if name:
        try:
            redirect_id = db.create_metatag(DB_PATH, name, parent)
        except ValueError:
            pass
    url = f"/metatags?metatag_id={redirect_id}" if redirect_id is not None else "/metatags"
    return RedirectResponse(url=url, status_code=303)


@app.post("/metatags/delete")
def delete_metatag_route(metatag_id: int = Form(...)):
    """Deletes an empty leaf metatag (see db.delete_metatag), redirecting
    up to its parent since metatag_id no longer exists; refused (still
    has children or linked tags) redirects back to the same node instead,
    left exactly as it was.
    """
    parent_id = db.get_all_metatags(DB_PATH).get(metatag_id, (None, None))[1]
    try:
        db.delete_metatag(DB_PATH, metatag_id)
    except ValueError:
        return RedirectResponse(url=f"/metatags?metatag_id={metatag_id}", status_code=303)
    url = f"/metatags?metatag_id={parent_id}" if parent_id is not None else "/metatags"
    return RedirectResponse(url=url, status_code=303)


@app.post("/metatags/add_tag")
def add_tag_to_metatag_route(metatag_id: int = Form(...), tag: str = Form("")):
    tag = tag.strip()
    if tag:
        db.add_tag_to_metatag(DB_PATH, metatag_id, tag)
    return RedirectResponse(url=f"/metatags?metatag_id={metatag_id}", status_code=303)


@app.post("/metatags/remove_tag")
def remove_tag_from_metatag_route(metatag_id: int = Form(...), tag: str = Form(...), viewing_id: int = Form(...)):
    """metatag_id is the tag's real, direct link to remove -- viewing_id
    is just where to redirect back to, since a tag shown on an ancestor's
    page (via _tags_for_metatag's aggregation) is actually linked to some
    descendant, not the node currently being viewed.
    """
    db.remove_tag_from_metatag(DB_PATH, metatag_id, tag)
    return RedirectResponse(url=f"/metatags?metatag_id={viewing_id}", status_code=303)


def _flatten_tag_options(names: list[str], children_map: dict[str, set[str]]) -> list[tuple[str, int]]:
    """Orders `names` (every known tag of one category, e.g. every
    Character) into a depth-first walk of their same-category wrangling
    hierarchy (db.get_tag_children), each paired with its depth (0 for a
    top-level tag) -- so a <select> can list "Ron Weasley" immediately
    followed by its indented children ("Ron Weasley (Auror)", etc.)
    instead of scattering a hierarchy across one flat alphabetical list.
    A tag whose parent isn't itself in `names` (a different category, or
    simply not one of the names being listed) is treated as its own
    top-level entry rather than disappearing. Every name in `names`
    appears exactly once, siblings sorted alphabetically at each level.
    """
    names_set = set(names)
    parent_of = {child: parent for parent, kids in children_map.items() for child in kids}
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    top_level: list[str] = []
    for name in names:
        parent = parent_of.get(name)
        if parent is not None and parent in names_set:
            children_by_parent[parent].append(name)
        else:
            top_level.append(name)

    ordered: list[tuple[str, int]] = []

    def visit(name: str, depth: int) -> None:
        ordered.append((name, depth))
        for child in sorted(children_by_parent.get(name, [])):
            visit(child, depth + 1)

    for name in sorted(top_level):
        visit(name, 0)
    return ordered


def _relationship_name_parts(relationship_tag: str) -> list[str]:
    """The individual party names in a Relationship tag's own text, split
    on "/" or "&" (same convention/regex as the autocomplete indexer) --
    one Character-association slot per part, see
    db.get_all_relationship_characters.
    """
    return [part.strip() for part in _RELATIONSHIP_SPLIT_RE.split(relationship_tag) if part.strip()]


def _unverified(tags: list[str], verified_tags: set[str]) -> list[str]:
    """Drops any tag currently marked Verified -- a locked row, same idea
    as "No Fandom" already blocking new Character/Relationship links:
    once someone's confirmed a row is right, every classification/
    association route refuses to touch it until its own Verified box is
    unchecked again (see set_tag_verified_route, the one action always
    allowed regardless of this). Used both for a single tag (wrap it in a
    list) and for a bulk action's whole checked batch.
    """
    return [tag for tag in tags if tag not in verified_tags]


def _matches_classification_source(
    category: str | None, is_explicit: bool, show_guessed: bool, show_set: bool
) -> bool:
    """The "Show guessed"/"Show set" checkboxes on Classify Tags -- both
    off (the default) means no restriction at all, including Unclassified
    rows. Checking one or both narrows to already-classified tags only
    (Unclassified is neither guessed nor explicitly set, so it drops out
    the moment either box is checked), matching whichever source(s) are
    checked -- both checked together is "any classified tag, guessed or
    not", i.e. everything except Unclassified.
    """
    if not show_guessed and not show_set:
        return True
    if category is None:
        return False
    return (show_guessed and not is_explicit) or (show_set and is_explicit)


def _is_tag_complete(
    tag: str,
    category: str | None,
    is_parent: bool,
    is_child: bool,
    tag_fandoms: dict[str, str],
    tag_media_types: dict[str, set[str]],
    relationship_characters: dict[str, dict[int, str]],
    freeform_characters: dict[str, set[str]],
    freeform_relationships: dict[str, set[str]],
    verified_tags: set[str],
) -> bool:
    """Whether `tag` has every piece of classification data its category
    calls for -- feeds the "Incomplete items only" checkbox on Classify
    Tags:

    - Fandom: needs its own AO3-style Fandom Category (media type) set.
    - Character: needs a Fandom association set -- an explicit "No Fandom"
      counts as complete too, since that's a real decision, not a gap.
    - Relationship: needs a Fandom association, and (unless that Fandom is
      explicitly "No Fandom", which the table itself refuses to attach
      Characters to at all) a linked Character for every name part.
    - Freeform: a tag with no wrangling role of its own (not a parent or
      child of anything, see db.get_tag_children) is always complete --
      Fandom/Character only start to matter once a Freeform tag is
      actually organizing other tags. A parent or child Freeform tag needs
      a Fandom association; if that Fandom isn't "No Fandom" and this tag
      is the parent (not a child -- a child is the non-canonical one and
      inherits the parent's own completeness rather than needing its own
      Character), it also needs at least one linked Character. Separately
      -- parent/child or not -- any Freeform tag linked to a Relationship
      needs exactly that Relationship's own party count in linked
      Characters, so a Freeform tagged with "Harry/Draco" isn't missing
      one side of it.

    On top of all of the above, for every one of those four types, `tag`
    must also have its own "Verified" checkbox checked (see
    db.get_all_verified_tags) -- the per-category data can be entirely
    filled in and it's still "incomplete" until someone's confirmed it by
    eye, so ticking every box above without ever checking Verified never
    clears a tag off this list on its own.

    Anything else (not yet classified at all) has nothing to check.
    """
    if category == "fandom":
        return tag in tag_media_types and tag in verified_tags
    if category == "character":
        return tag in tag_fandoms and tag in verified_tags
    if category == "relationship":
        if tag not in tag_fandoms:
            return False
        if tag_fandoms[tag] != "No Fandom":
            linked = relationship_characters.get(tag, {})
            parts = _relationship_name_parts(tag)
            if not parts or not all(i in linked for i in range(len(parts))):
                return False
        return tag in verified_tags
    if category == "freeform":
        if is_parent or is_child:
            if tag not in tag_fandoms:
                return False
            if tag_fandoms[tag] != "No Fandom" and not is_child and not freeform_characters.get(tag):
                return False
        for relationship_tag in freeform_relationships.get(tag, ()):
            if len(freeform_characters.get(tag, ())) != len(_relationship_name_parts(relationship_tag)):
                return False
        return tag in verified_tags
    return True


def _tag_has_no_fandom(tag: str) -> bool:
    """True once a tag has the explicit, terminal "No Fandom" choice set
    on it (see db.set_tag_fandom) -- as opposed to simply never having had
    a Fandom association set at all. A tag like this is meant to be
    fandom-agnostic (a universal trope, an OC with no canon), so it can't
    also carry a Character/Relationship association of its own -- those
    are inherently fandom-specific and would contradict that choice.
    """
    return db.get_all_tag_fandoms(DB_PATH).get(tag) == "No Fandom"


def _auto_link_relationship_characters() -> None:
    """Fills in any relationship-character slot left blank where the name
    part matches an existing Character tag's own name exactly -- in
    either direction, since this recomputes from scratch every time it
    runs rather than reacting to just one side of a match: a Relationship
    classified after its Characters already exist, or a Character
    classified after its Relationship already does, both get linked the
    next time this runs. An already-linked slot (auto or set by hand) is
    never touched, and a Relationship with "No Fandom" explicitly set
    (see _tag_has_no_fandom) is skipped entirely, same restriction as the
    manual per-slot control.
    """
    categories = db.get_all_tag_categories(DB_PATH)
    character_tags = {tag for tag, category in categories.items() if category == "character"}
    existing = db.get_all_relationship_characters(DB_PATH)
    for tag, category in categories.items():
        if category != "relationship" or _tag_has_no_fandom(tag):
            continue
        linked = existing.get(tag, {})
        for part_index, part in enumerate(_relationship_name_parts(tag)):
            if part_index not in linked and part in character_tags:
                db.set_relationship_character(DB_PATH, tag, part_index, part)


@app.post("/tags/classify/set_fandom")
def set_tag_fandom_route(
    tag: str = Form(...),
    fandom: str = Form(""),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Sets tag's own Fandom association -- 'No Fandom' is a real,
    explicit choice (it stops inheritance from an ancestor same-category
    tag), not the same as never setting anything. An empty `fandom`
    (the dropdown's "No Fandom (auto)" option) instead clears any
    explicit association, reverting the tag back to inheriting from its
    same-category parent chain. See scanner.resolve_tag_fandom_explicit.
    A Verified tag refuses this entirely -- see _unverified.
    """
    if tag not in db.get_all_verified_tags(DB_PATH):
        if fandom:
            db.set_tag_fandom(DB_PATH, tag, fandom)
        else:
            db.remove_tag_fandom(DB_PATH, tag)
        scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/set_media_type")
def set_tag_media_type_route(
    tag: str = Form(...),
    media_type: list[str] = Form([]),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Replaces tag's own explicit AO3-style media types wholesale with
    whatever's checked (see the Classify Tags checkbox group and
    db.set_tag_media_types) -- a Fandom can genuinely belong to more than
    one category, so this isn't a single-value dropdown. An empty
    selection clears tag's own explicit choice entirely, reverting it to
    inheritance (see db.set_tag_media_types); checking only "Uncategorized
    Fandoms" is still a real, terminal choice, not the same as clearing
    it. Only meaningful on a tag explicitly classified Fandom (see
    tags.html), though nothing here re-checks that server-side -- there's
    no association to corrupt either way if it's ever called on something
    else. A Verified tag refuses this entirely -- see _unverified.
    """
    if tag not in db.get_all_verified_tags(DB_PATH):
        db.set_tag_media_types(DB_PATH, tag, set(media_type))
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/set_verified")
def set_tag_verified_route(
    tag: str = Form(...),
    verified: bool = Form(False),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Toggles the "Verified" checkbox -- a personal checklist over
    classification that's independent of it (see db.get_all_verified_tags):
    ticking it doesn't change a tag's category/Fandom/associations at all,
    just marks that someone looked at this row and confirmed it's right.
    """
    db.set_tag_verified(DB_PATH, tag, verified)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/set_relationship_character")
def set_relationship_character_route(
    relationship_tag: str = Form(...),
    part_index: int = Form(...),
    character_tag: str = Form(""),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Links one of relationship_tag's "/"-or-"&"-separated name parts to
    an actual Character tag -- the Character's own spelling can differ
    from the literal substring (e.g. the relationship says "Harry Potter"
    but the Character tag is "Harry James Potter"). An empty
    character_tag clears that slot instead of linking it. A relationship
    with "No Fandom" explicitly set can't gain a new link (see
    _tag_has_no_fandom) -- clearing an existing one is still always
    allowed, so setting "No Fandom" later never traps a stale link. A
    Verified relationship_tag refuses either direction, though -- see
    _unverified, unverifying it first is the only way back in.
    """
    character_tag = character_tag.strip()
    if relationship_tag not in db.get_all_verified_tags(DB_PATH):
        if character_tag:
            if not _tag_has_no_fandom(relationship_tag):
                db.set_relationship_character(DB_PATH, relationship_tag, part_index, character_tag)
        else:
            db.remove_relationship_character(DB_PATH, relationship_tag, part_index)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/remove_freeform_character")
def remove_freeform_character_route(
    freeform_tag: str = Form(...),
    character_tag: str = Form(...),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    if freeform_tag not in db.get_all_verified_tags(DB_PATH):
        db.remove_freeform_character(DB_PATH, freeform_tag, character_tag)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/remove_freeform_relationship")
def remove_freeform_relationship_route(
    freeform_tag: str = Form(...),
    relationship_tag: str = Form(...),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    if freeform_tag not in db.get_all_verified_tags(DB_PATH):
        db.remove_freeform_relationship(DB_PATH, freeform_tag, relationship_tag)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/apply_associations")
def apply_associations(
    tags: list[str] = Form([]),
    fandom: str = Form(""),
    character: str = Form(""),
    relationship: str = Form(""),
    media_type: str = Form(""),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    """Bulk-applies whichever of Fandom/Character/Relationship/Media Type
    were picked (each blank means "don't touch that one") to every
    checked tag, so setting the same Fandom on a dozen Characters -- or
    adding the same Media Type to a dozen Fandoms -- doesn't need a visit
    to each row's own per-row control. Fandom only applies to a selected
    tag whose effective category is character/relationship/freeform (see
    _effective_tag_category); Character/Relationship association only
    applies to selected tags that are freeform, since a Relationship's
    Characters are per-name-part slots with no sensible bulk target;
    Media Type only applies to a tag *explicitly* classified Fandom (see
    tags.html's own "(classify to set a type)" hint for a merely-guessed
    one), same restriction as the per-row control -- and, since a Fandom
    can belong to more than one category, this *adds* the picked one to
    whatever the tag already has rather than replacing its whole set (the
    per-row checkbox group is the tool for replacing/clearing one). A
    Verified tag among those checked is skipped entirely -- see
    _unverified.
    """
    fandom = fandom.strip()
    character = character.strip()
    relationship = relationship.strip()
    media_type = media_type.strip()
    if fandom or character or relationship or media_type:
        entries = scanner.load_cached(DB_PATH).entries
        tag_fandoms = db.get_all_tag_fandoms(DB_PATH)
        explicit_categories = db.get_all_tag_categories(DB_PATH)
        tag_media_types = db.get_all_tag_media_types(DB_PATH)
        for tag in _unverified(tags, db.get_all_verified_tags(DB_PATH)):
            category = _effective_tag_category(entries, tag)
            if fandom and category in ("character", "relationship", "freeform"):
                db.set_tag_fandom(DB_PATH, tag, fandom)
            no_fandom = tag_fandoms.get(tag) == "No Fandom"
            if character and category == "freeform" and not no_fandom:
                db.add_freeform_character(DB_PATH, tag, character)
            if relationship and category == "freeform" and not no_fandom:
                db.add_freeform_relationship(DB_PATH, tag, relationship)
            if media_type and explicit_categories.get(tag) == "fandom":
                db.set_tag_media_types(DB_PATH, tag, tag_media_types.get(tag, set()) | {media_type})
        if fandom:
            scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/set_selected")
def set_selected_tags(
    tags: list[str] = Form([]),
    category: str = Form(...),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    tags = _unverified(tags, db.get_all_verified_tags(DB_PATH))
    if category in ("fandom", "character", "relationship", "freeform") and tags:
        db.set_tag_categories(DB_PATH, {t: category for t in tags})
        if category in ("character", "relationship"):
            _auto_link_relationship_characters()
        scanner.rebuild_work_tags(DB_PATH)
    elif category == "unclassify" and tags:
        db.remove_tag_categories(DB_PATH, tags)
        scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/mark_page_freeform")
def mark_page_freeform(
    tags: list[str] = Form([]),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
    work_id: str = Form(""),
    q: str = Form(""),
    show_guessed: bool = Form(False),
    show_set: bool = Form(False),
    incomplete_only: bool = Form(False),
):
    explicit = db.get_all_tag_categories(DB_PATH)
    verified = db.get_all_verified_tags(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in tags if t not in explicit and t not in verified})
    scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(
        url=(
            f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}&work_id={quote(work_id)}&q={quote(q)}"
            f"&show_guessed={str(show_guessed).lower()}&show_set={str(show_set).lower()}&incomplete_only={str(incomplete_only).lower()}"
        ),
        status_code=303,
    )


@app.post("/tags/classify/mark_all_unclassified_freeform")
def mark_all_unclassified_freeform(work_id: str = Form("")):
    result = scanner.load_cached(DB_PATH)
    all_tags = {t for e in result.entries for t in e.fandom_candidates}
    explicit = db.get_all_tag_categories(DB_PATH)
    verified = db.get_all_verified_tags(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in all_tags if t not in explicit and t not in verified})
    scanner.rebuild_work_tags(DB_PATH)
    return RedirectResponse(url=f"/tags/classify?work_id={quote(work_id)}", status_code=303)


@app.get("/fandoms", response_class=HTMLResponse)
def fandoms(request: Request, sort: str = DEFAULT_NAME_COUNT_SORT, letter: str = "all", media_type: str = "all"):
    result = scanner.load_cached(DB_PATH)
    counts = Counter()
    for entry in result.entries:
        for name in entry.fandoms:
            counts[name] += 1
    children_map = db.get_tag_children(DB_PATH)
    _add_virtual_parent_counts(counts, result.entries, lambda e: e.fandoms, db.get_all_tag_descendants(DB_PATH))
    parent_of = scanner.child_parent_map(children_map)
    explicit_media_types = db.get_all_tag_media_types(DB_PATH)

    sorted_fandoms = _sort_name_count_rows([(name, count, None) for name, count in counts.items()], sort)
    sorted_fandoms = _filter_by_letter(sorted_fandoms, letter)

    # Tab counts reflect the whole library regardless of which letter/media
    # type tab happens to be selected right now -- same "always the full
    # picture" convention as Classify Tags' own filter-tab counts. A Fandom
    # belonging to more than one category counts once in each of their
    # tabs (not split fractionally), so these tab counts -- unlike a
    # single-value tab set -- don't sum back to total_fandoms, which is
    # instead the distinct-fandom count from before this per-category split.
    total_fandoms = len(sorted_fandoms)
    media_type_counts: Counter = Counter()
    for row in sorted_fandoms:
        for mt in scanner.resolve_tag_media_type(row[0], parent_of, explicit_media_types):
            media_type_counts[mt] += 1

    sorted_fandoms = _filter_by_media_type(sorted_fandoms, media_type, parent_of, explicit_media_types)
    sorted_fandoms = _group_tag_rows_by_parent(sorted_fandoms, children_map)
    return templates.TemplateResponse(
        "fandoms.html",
        {
            **_base_context(request),
            "fandoms": sorted_fandoms,
            "sort": sort,
            "sort_options": NAME_COUNT_SORT_LABELS,
            "letter": letter,
            "letter_options": LETTER_FILTER_OPTIONS,
            "media_type": media_type,
            "media_type_options": FANDOM_MEDIA_TYPES,
            "media_type_counts": media_type_counts,
            "total_fandoms": total_fandoms,
        },
    )


def _feeds_with_rows() -> list[dict]:
    result = scanner.load_cached(DB_PATH)
    local_by_id = {e.work_id: e for e in result.entries}

    feeds = []
    for feed in rss.list_tracked_feeds(FEEDS_DB_PATH):
        rows = []
        for entry in rss.get_feed_entries(FEEDS_DB_PATH, feed.url):
            local_entry = local_by_id.get(entry.work_id)
            on_disk = bool(local_entry and local_entry.on_disk)
            local_timestamp = scanner.effective_timestamp(local_entry) if local_entry else None
            rows.append({
                "entry": entry,
                "on_disk": on_disk,
                "status": rss.assess_status(entry, on_disk, local_timestamp),
            })
        feeds.append({"feed": feed, "rows": rows})
    return feeds


@app.get("/tracked", response_class=HTMLResponse)
def tracked(request: Request):
    return templates.TemplateResponse(
        "tracked.html",
        {
            **_base_context(request),
            "feeds": _feeds_with_rows(),
        },
    )


MANUAL_LINK_SOURCE = "Manually added"
AO3_WORK_URL_RE = re.compile(r"works/(\d+)", re.IGNORECASE)


def _parse_manual_links(text: str) -> list[tuple[str, str]]:
    """Pulls (work_id, canonical url) pairs out of pasted text -- scans for
    any works/<id> occurrence rather than requiring one link per line, so a
    comma-separated list or a pasted paragraph of links both work. Dedupes
    by work_id, keeping the first occurrence.
    """
    found: dict[str, str] = {}
    for match in AO3_WORK_URL_RE.finditer(text):
        work_id = match.group(1)
        found.setdefault(work_id, f"https://archiveofourown.org/works/{work_id}")
    return list(found.items())


def _manual_queue_rows() -> list[dict]:
    """Manually pasted AO3 links (see /queue/manual_links/add) that aren't
    downloaded yet. A link's stored row is never deleted just because its
    work_id lands on disk -- it simply stops appearing here, and would
    reappear on its own if that file were ever removed, the same way a
    tracked feed's entry behaves.
    """
    result = scanner.load_cached(DB_PATH)
    local_by_id = {e.work_id: e for e in result.entries}
    rows = []
    for link in db.list_manual_links(DB_PATH):
        local_entry = local_by_id.get(link["work_id"])
        if local_entry and local_entry.on_disk:
            continue
        entry = rss.FeedEntry(
            work_id=link["work_id"],
            title=local_entry.title if local_entry else None,
            author=local_entry.author if local_entry else None,
        )
        rows.append({"entry": entry, "on_disk": False, "status": "not_downloaded", "feed_titles": [MANUAL_LINK_SOURCE]})
    return rows


def _queue_items() -> list[dict]:
    """Tracked-feed entries that still need attention (not downloaded at
    all, or possibly out of date -- a first cut over the same status rss
    already computes for the Tracked Feeds page), plus every manually
    pasted link that isn't downloaded yet.

    The same work can show up more than once (tracked through two feeds,
    or tracked AND manually pasted) -- dedupe by work_id so it shows up
    once, keeping whichever feed's copy has the most recent feed_updated
    info and listing every source it came from.
    """
    status_order = {"not_downloaded": 0, "may_need_update": 1}
    by_work_id: dict[str, dict] = {}
    for group in _feeds_with_rows():
        feed_title = group["feed"].user_title or group["feed"].title or group["feed"].url
        for row in group["rows"]:
            if row["status"] not in status_order:
                continue
            work_id = row["entry"].work_id
            existing = by_work_id.get(work_id)
            if existing is None:
                by_work_id[work_id] = {**row, "feed_titles": [feed_title]}
                continue
            if feed_title not in existing["feed_titles"]:
                existing["feed_titles"].append(feed_title)
            new_updated = row["entry"].feed_updated
            if new_updated is not None and (existing["entry"].feed_updated is None or new_updated > existing["entry"].feed_updated):
                existing["entry"] = row["entry"]
                existing["status"] = row["status"]
                existing["on_disk"] = row["on_disk"]

    for row in _manual_queue_rows():
        work_id = row["entry"].work_id
        existing = by_work_id.get(work_id)
        if existing is None:
            by_work_id[work_id] = row
        elif MANUAL_LINK_SOURCE not in existing["feed_titles"]:
            existing["feed_titles"].append(MANUAL_LINK_SOURCE)

    items = list(by_work_id.values())
    items.sort(key=lambda item: (status_order[item["status"]], (item["entry"].title or "").lower()))
    return items


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    return templates.TemplateResponse(
        "queue.html",
        {
            **_base_context(request),
            "items": _queue_items(),
            "download_queue_counts": db.get_download_queue_counts(DB_PATH),
            "download_worker_running": _download_worker_running(),
            "download_worker_current": db.get_meta(DB_PATH, DOWNLOAD_WORKER_CURRENT_KEY) or "",
        },
    )


def _enqueue_selected_downloads(items: list[tuple[str, str, str | None]]) -> None:
    """Shared by every "Download Selected" action (Queue, Incomplete Works)
    -- adds (work_id, url, title) rows to the one shared download queue
    and makes sure the background worker is running to pick them up.
    """
    if items:
        db.enqueue_downloads(DB_PATH, items, datetime.now().isoformat())
        _ensure_download_worker_running()


@app.post("/queue/download")
async def download_selected_queue_items(work_id: list[str] = Form([])):
    """Enqueues the checked Queue rows for the background download worker
    (see _download_worker_loop). Looks the URL/title back up from the same
    tracked-feed data the Queue page itself renders from, so the browser
    only ever has to post back a work_id, not a full AO3 URL.
    """
    wanted = set(work_id)
    selected = [
        (item["entry"].work_id, f"https://archiveofourown.org/works/{item['entry'].work_id}", item["entry"].title)
        for item in _queue_items() if item["entry"].work_id in wanted
    ]
    _enqueue_selected_downloads(selected)
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/queue/stop_downloads")
async def stop_download_worker(next: str = Form("/queue")):
    """Lets the current in-flight item finish rather than aborting it
    mid-download -- the loop only checks this flag between items. Shared
    by both Queue and Incomplete Works, since there's one download worker
    for the whole app -- `next` sends you back to whichever page you
    stopped it from.
    """
    _download_worker_stop.set()
    return RedirectResponse(url=next, status_code=303)


@app.post("/queue/clear_finished_downloads")
def clear_finished_downloads_route(next: str = Form("/queue")):
    db.clear_finished_downloads(DB_PATH)
    return RedirectResponse(url=next, status_code=303)


@app.post("/queue/manual_links/add")
def add_manual_links_route(links: str = Form("")):
    parsed = _parse_manual_links(links)
    if parsed:
        db.add_manual_links(DB_PATH, parsed, datetime.now().isoformat())
    return RedirectResponse(url="/queue", status_code=303)


@app.post("/queue/manual_links/remove")
def remove_manual_link_route(work_id: str = Form(...)):
    db.remove_manual_link(DB_PATH, work_id)
    return RedirectResponse(url="/queue", status_code=303)


@app.get("/incomplete", response_class=HTMLResponse)
def incomplete_works(request: Request):
    """Every already-downloaded work that's a WIP (see _completion_status),
    regardless of whether it's tracked through any feed -- unlike Queue,
    which is scoped to tracked feeds and to what isn't downloaded yet or
    might have updated. Sorted oldest-checked first (by when you last
    downloaded/refreshed the file locally, since a plain epub carries no
    AO3-side "last updated" date of its own -- only a tracked feed's RSS
    entry has that), so a WIP nobody's redownloaded in a long time -- and
    might quietly have new chapters waiting -- surfaces at the top.
    """
    result = scanner.load_cached(DB_PATH)
    items = [
        {"entry": entry, "last_updated": scanner.effective_timestamp(entry)}
        for entry in result.entries
        if _completion_status(entry) == "wip"
    ]
    items.sort(key=lambda item: item["last_updated"] or datetime.min)
    return templates.TemplateResponse(
        "incomplete.html",
        {
            **_base_context(request),
            "items": items,
            "download_queue_counts": db.get_download_queue_counts(DB_PATH),
            "download_worker_running": _download_worker_running(),
            "download_worker_current": db.get_meta(DB_PATH, DOWNLOAD_WORKER_CURRENT_KEY) or "",
        },
    )


@app.post("/incomplete/download")
async def download_selected_incomplete_items(work_id: list[str] = Form([])):
    wanted = set(work_id)
    result = scanner.load_cached(DB_PATH)
    selected = [
        (entry.work_id, f"https://archiveofourown.org/works/{entry.work_id}", entry.title)
        for entry in result.entries if entry.work_id in wanted
    ]
    _enqueue_selected_downloads(selected)
    return RedirectResponse(url="/incomplete", status_code=303)


@app.post("/tracked/add")
def add_tracked_feed_route(url: str = Form(...), label: str = Form("")):
    redirect_url = "/tracked"
    try:
        rss.add_tracked_feed(FEEDS_DB_PATH, url.strip(), label.strip() or None)
    except rss.FeedRefreshError as exc:
        redirect_url += f"?refresh_error={quote(str(exc))}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/tracked/delete")
def delete_tracked_feed_route(url: str = Form(...)):
    rss.delete_tracked_feed(FEEDS_DB_PATH, url)
    return RedirectResponse(url="/tracked", status_code=303)


@app.post("/tracked/toggle_auto")
def toggle_auto_refresh(url: str = Form(...), enabled: bool = Form(...)):
    rss.set_feed_auto_refresh(FEEDS_DB_PATH, url, enabled)
    return RedirectResponse(url="/tracked", status_code=303)


def _refresh_downloads_cache_now() -> None:
    """Rescans the downloads folder/log only -- kept separate from feed
    refreshing (see /refresh/feeds) since walking a large downloads folder
    and hitting every tracked feed on every click was too much to always
    bundle together. Also re-matches against Audiobookshelf if configured --
    for matched works, Audiobookshelf's own already-scanned title/tags are
    used in place of parsing the epub file again (see scanner._scan_disk),
    so matches are loaded first and threaded into the same scan.

    Shared by the manual /refresh route and the background download worker
    (see _download_worker_loop) -- a work the worker just downloaded/
    redownloaded would otherwise keep showing whatever stale mtime/absence
    was in the cache from the last manual refresh, potentially for as long
    as the current queue takes to drain.
    """
    abs_matches = {}
    if ABS_DB_PATH and ABS_LIBRARY_ID:
        abs_matches = audiobookshelf.load_matches(ABS_DB_PATH, ABS_LIBRARY_ID)

    scanner.refresh_cache(
        DOWNLOAD_DIR, LOG_PATH, DB_PATH, abs_matches,
        extra_dirs=[MANUAL_DOWNLOAD_DIR] if MANUAL_DOWNLOAD_DIR else None,
    )
    if ABS_DB_PATH and ABS_LIBRARY_ID:
        db.save_abs_matches(DB_PATH, {work_id: m.item_id for work_id, m in abs_matches.items()})
        # Read status is per-app-account, not shared like the match above --
        # only accounts that have paired an Audiobookshelf username (Account
        # page) get synced; everyone else stays on the manual toggle alone.
        for user_id, abs_username in db.list_user_abs_usernames(DB_PATH).items():
            finished = audiobookshelf.load_read_work_ids(ABS_DB_PATH, ABS_LIBRARY_ID, abs_username)
            db.save_abs_read_status(DB_PATH, user_id, finished)
    db.set_meta(DB_PATH, LAST_REFRESHED_KEY, datetime.now().isoformat())


@app.post("/refresh")
def refresh(next: str = Form("/")):
    _refresh_downloads_cache_now()
    return RedirectResponse(url=next or "/", status_code=303)


@app.post("/refresh/feeds")
def refresh_feeds(next: str = Form("/tracked")):
    errors = rss.refresh_all_tracked_feeds(FEEDS_DB_PATH)
    db.set_meta(DB_PATH, FEEDS_LAST_REFRESHED_KEY, datetime.now().isoformat())

    redirect_url = next or "/tracked"
    if errors:
        sep = "&" if "?" in redirect_url else "?"
        redirect_url += f"{sep}refresh_error={quote('; '.join(errors))}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": error})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/")):
    credentials = db.get_user_credentials(DB_PATH, username.strip())
    if not credentials or not auth.verify_password(password, credentials[1]):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next, "error": "Invalid username or password."},
            status_code=401,
        )
    user, _ = credentials
    token = auth.generate_session_token()
    db.create_session(DB_PATH, token, user.id, datetime.now().isoformat())
    response = RedirectResponse(url=next or "/", status_code=303)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout(request: Request):
    token = request.cookies.get("session")
    if token:
        db.delete_session(DB_PATH, token)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, error: str = "", saved: str = ""):
    context = _base_context(request)
    return templates.TemplateResponse(
        "account.html",
        {
            **context,
            "error": error,
            "saved": saved,
            "themes": db.list_user_themes(DB_PATH, request.state.user.id),
            "active_theme_id": db.get_active_theme_id(DB_PATH, request.state.user.id),
            "abs_username": db.get_user_abs_username(DB_PATH, request.state.user.id) or "",
            "timezone_options": TIMEZONE_OPTIONS,
        },
    )


@app.post("/account/themes")
def add_theme(request: Request, name: str = Form(...), css: str = Form("")):
    """Saves a new named theme (raw CSS, applied only to this user's own
    page loads -- see base.html, translate_ao3_skin_selectors, and
    sanitize_style_content) and switches to it right away, the same way
    saving used to apply immediately back when there was only ever one
    theme. Switching back to an earlier saved theme afterward is just
    "Use" on its row -- no need to re-paste its CSS.
    """
    theme_id = db.create_theme(
        DB_PATH, request.state.user.id, name.strip() or "Untitled theme", css, datetime.now().isoformat()
    )
    db.set_active_theme(DB_PATH, request.state.user.id, theme_id)
    return RedirectResponse(url="/account?saved=theme", status_code=303)


@app.post("/account/themes/{theme_id}/edit")
def edit_theme(request: Request, theme_id: int, name: str = Form(...), css: str = Form("")):
    """Updates a saved theme's name/CSS in place -- doesn't change which
    theme is active, whether or not this is the one currently in use.
    """
    db.update_theme(DB_PATH, request.state.user.id, theme_id, name.strip() or "Untitled theme", css)
    return RedirectResponse(url="/account?saved=theme", status_code=303)


@app.post("/account/themes/{theme_id}/activate")
def activate_theme(request: Request, theme_id: int):
    db.set_active_theme(DB_PATH, request.state.user.id, theme_id)
    return RedirectResponse(url="/account?saved=theme_active", status_code=303)


@app.post("/account/themes/deactivate")
def deactivate_theme(request: Request):
    """Switches back to the default look without deleting any saved theme."""
    db.set_active_theme(DB_PATH, request.state.user.id, None)
    return RedirectResponse(url="/account?saved=theme_active", status_code=303)


@app.post("/account/themes/{theme_id}/delete")
def remove_theme(request: Request, theme_id: int):
    db.delete_theme(DB_PATH, request.state.user.id, theme_id)
    return RedirectResponse(url="/account?saved=theme_deleted", status_code=303)


@app.post("/account/timezone")
def save_timezone(request: Request, timezone: str = Form("")):
    """Per-account display timezone -- see local_time. Blank means "no
    conversion, show the server's own recorded time as-is".
    """
    db.set_user_timezone(DB_PATH, request.state.user.id, timezone.strip() or None)
    return RedirectResponse(url="/account?saved=timezone", status_code=303)


@app.post("/account/abs_username")
def save_abs_username(request: Request, abs_username: str = Form("")):
    """Pairs this account with an Audiobookshelf username so the next
    Refresh can pull that person's own read/finished status (see
    audiobookshelf.load_read_work_ids) -- purely opt-in, and per-account,
    so one household's users can each pair their own (or not pair at all
    and just use the manual read toggle).
    """
    db.set_user_abs_username(DB_PATH, request.state.user.id, abs_username)
    return RedirectResponse(url="/account?saved=abs_username", status_code=303)


@app.post("/account/password")
def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    user = request.state.user
    credentials = db.get_user_credentials(DB_PATH, user.username)
    if not credentials or not auth.verify_password(current_password, credentials[1]):
        return RedirectResponse(url="/account?error=" + quote("Current password is incorrect."), status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/account?error=" + quote("New passwords don't match."), status_code=303)
    if len(new_password) < 4:
        return RedirectResponse(url="/account?error=" + quote("New password is too short."), status_code=303)
    db.set_user_password(DB_PATH, user.id, auth.hash_password(new_password))
    return RedirectResponse(url="/account?saved=password", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        "admin_users.html",
        {**_base_context(request), "users": db.list_users(DB_PATH), "error": error},
    )


@app.post("/admin/users/create")
def admin_create_user(username: str = Form(...), password: str = Form(...), role: str = Form("user")):
    username = username.strip()
    if not username or db.get_user_credentials(DB_PATH, username):
        return RedirectResponse(url="/admin/users?error=" + quote("That username is already taken."), status_code=303)
    if role not in ("user", "admin"):
        role = "user"
    db.create_user(DB_PATH, username, auth.hash_password(password), role)
    return RedirectResponse(url="/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/password")
def admin_set_user_password(user_id: int, new_password: str = Form(...)):
    db.set_user_password(DB_PATH, user_id, auth.hash_password(new_password))
    return RedirectResponse(url="/admin/users", status_code=303)


@app.get("/admin/catalog", response_class=HTMLResponse)
def admin_catalog_page(request: Request, error: str = ""):
    """Bulk-import work metadata from an external SQLite export (see
    app/catalog_import.py) into db.catalog_works, for works this app has
    never scanned a file for. Deliberately NOT merged into Downloads/Tags/
    Fandoms or any other page yet -- those all run scanner.load_cached on
    every request, which would mean rebuilding a Python object for every
    catalog row on every page load; fine at a few thousand rows, not at
    the multi-million-row scale this is actually built for. This page is
    the only place catalog_works is currently visible (just a count).
    """
    return templates.TemplateResponse(
        "admin_catalog.html",
        {
            **_base_context(request),
            "catalog_count": db.count_catalog_works(DB_PATH),
            "running": _catalog_import_running(),
            "status": db.get_meta(DB_PATH, CATALOG_IMPORT_STATUS_KEY) or "",
            "progress": db.get_meta(DB_PATH, CATALOG_IMPORT_PROGRESS_KEY) or "",
            "import_error": db.get_meta(DB_PATH, CATALOG_IMPORT_ERROR_KEY) or "",
            "error": error,
        },
    )


@app.post("/admin/catalog/import")
async def admin_catalog_import(source_db_path: str = Form(...), table_name: str = Form("")):
    global _catalog_import_task
    source_db_path = source_db_path.strip()
    if not source_db_path:
        return RedirectResponse(
            url="/admin/catalog?error=" + quote("Enter a path to the source SQLite file."), status_code=303
        )
    if _catalog_import_running():
        return RedirectResponse(
            url="/admin/catalog?error=" + quote("An import is already running."), status_code=303
        )
    db.set_meta(DB_PATH, CATALOG_IMPORT_STATUS_KEY, "running")
    db.set_meta(DB_PATH, CATALOG_IMPORT_PROGRESS_KEY, "")
    db.set_meta(DB_PATH, CATALOG_IMPORT_ERROR_KEY, "")
    _catalog_import_task = asyncio.create_task(
        _catalog_import_worker(source_db_path, table_name.strip() or None)
    )
    return RedirectResponse(url="/admin/catalog", status_code=303)


CATALOG_BROWSE_CATEGORY_LABELS = {
    "fandom": "Fandom",
    "relationship": "Relationship",
    "freeform": "Additional Tag",
    "warning": "Warning",
    "category": "Category",
}


@app.get("/catalog/browse", response_class=HTMLResponse)
def catalog_browse_page(request: Request, category: str = "fandom", tag: str = "", page: int = 1):
    """Browse db.catalog_works by a single fandom/relationship/tag at a
    time -- open to every logged-in user, not just Admin (see
    ADMIN_PATH_PREFIXES; this path isn't in it), same as Fandoms/Tags
    browsing. Deliberately not a Downloads-style multi-facet filter panel:
    this is a plain indexed lookup (db.search_catalog_works) against
    however many millions of rows catalog_works holds, so it only ever
    supports "show me works with this one tag," paginated in SQL -- never
    a full-library scan/materialization the way Downloads' own filter
    panel works for the (much smaller) on-disk library.
    """
    category = category if category in catalog_import.CATALOG_TAG_KINDS else "fandom"
    works, page, total_pages = (
        db.search_catalog_works(DB_PATH, category, tag, page, CATALOG_BROWSE_PAGE_SIZE) if tag else ([], 1, 1)
    )
    return templates.TemplateResponse(
        "catalog_browse.html",
        {
            **_base_context(request),
            "category": category,
            "category_labels": CATALOG_BROWSE_CATEGORY_LABELS,
            "tag": tag,
            "works": works,
            "page": page,
            "total_pages": total_pages,
            "catalog_count": db.count_catalog_works(DB_PATH),
        },
    )


@app.get("/catalog/browse/search")
def catalog_browse_search(category: str = "fandom", q: str = ""):
    """Typeahead endpoint backing the Catalog Browse page's tag search box
    -- see _search_catalog_tags.
    """
    return JSONResponse(_search_catalog_tags(category, q))
