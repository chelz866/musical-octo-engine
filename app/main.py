import asyncio
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib.parse import quote

from fast_autocomplete import AutoComplete
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audiobookshelf, auth, db, rss, scanner
from .epub_meta import looks_like_relationship

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
FEEDS_DB_PATH = os.environ.get("FEEDS_DB_PATH", "/data/feeds.sqlite")
AUTO_REFRESH_INTERVAL_SECONDS = int(os.environ.get("AUTO_REFRESH_INTERVAL_SECONDS", 60 * 60))

DOWNLOADS_PAGE_SIZE = 25
TAGS_PAGE_SIZE = 100
FACET_SUGGESTION_COUNT = 10

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

    asyncio.create_task(_auto_refresh_loop())


# Everything requires login except these. Within that, a fixed set of
# path prefixes are admin-only: maintenance/setup pages a regular user
# (e.g. a friend given access) should never see, plus tag classification
# (the per-work fandom-picker POST included, matched by its own "/fandom"
# suffix since it lives under the same "/works/{id}/..." prefix as the
# bookmark toggle, which every logged-in user *should* be able to reach).
PUBLIC_PATHS = {"/login"}
ADMIN_PATH_PREFIXES = ("/admin", "/issues", "/tracked", "/queue", "/refresh", "/tags/classify")


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


templates.env.filters["human_size"] = human_size
templates.env.filters["format_number"] = lambda n: f"{n:,}" if n is not None else ""

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
        "theme_css": db.get_user_theme_css(DB_PATH, request.state.user.id),
        "last_refreshed": db.get_meta(DB_PATH, LAST_REFRESHED_KEY),
        "feeds_last_refreshed": db.get_meta(DB_PATH, FEEDS_LAST_REFRESHED_KEY),
        "refresh_error": request.query_params.get("refresh_error"),
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

SORT_OPTIONS = {
    "title": lambda e: (e.title or "").lower(),
    "author": lambda e: (e.author or "").lower(),
    "word_count_desc": lambda e: -(e.word_count or 0),
    "word_count_asc": lambda e: (e.word_count or 0),
    "newest": lambda e: scanner.effective_timestamp(e) or datetime.min,
}
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


def _search_facet_tags(facet: str, q: str, limit: int = 20) -> list[str]:
    q = q.strip()
    if facet not in TAG_SEARCH_FACETS or len(q) < 2:
        return []
    autocompleter, word_to_tags = _get_autocompleter(facet)
    results = autocompleter.search(word=q, max_cost=2, size=limit * 2)
    matched: set[str] = set()
    for result in results:
        key = " ".join(result) if isinstance(result, list) else result
        matched.update(word_to_tags.get(key, ()))
    return sorted(matched, key=str.lower)[:limit]


def _entry_matches(entry, filters: dict, skip_include: str | None = None, skip_exclude: str | None = None) -> bool:
    """Include is AND across facets AND within a facet's selected values
    (matches real AO3: checking both "M/M" and "F/F" means only works with
    both). Exclude is OR within a facet (matching ANY excluded value drops
    the work) and AND across facets, same as real AO3. `skip_include`/
    `skip_exclude` each skip one facet's own Include/Exclude constraint --
    used to build that facet's own suggestion/count list from what
    everything *else* currently matches; they're independent since a facet
    can have both an active Include and an active Exclude at once.
    """
    for name, values in filters["facets"].items():
        if name == skip_include or not values:
            continue
        if not set(values) <= set(FACETS[name](entry)):
            return False
    for name, values in filters["exclude"].items():
        if name == skip_exclude or not values:
            continue
        if set(FACETS[name](entry)) & set(values):
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
    """
    selected = set(filters[mode][name])
    counts: Counter = Counter()
    for entry in entries:
        if _entry_matches(entry, filters, **_skip_kwargs(name, mode)):
            counts.update(v for v in FACETS[name](entry) if v not in selected)
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
    entries.sort(key=SORT_OPTIONS.get(filters["sort"], SORT_OPTIONS[DEFAULT_SORT]))

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
            "log_path": LOG_PATH,
            "log_exists": os.path.isfile(LOG_PATH),
        },
    )


@app.get("/tags/search")
def tag_search(facet: str, q: str = ""):
    """Typeahead endpoint backing the Downloads page's per-facet "Find
    another..." box -- see _search_facet_tags. Used to reach any of a
    library's 20,000+ tags, not just the top-10 suggestions already shown.
    """
    return JSONResponse(_search_facet_tags(facet, q))


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


def _tag_rows(result, filter: str, sort: str) -> tuple[list[tuple[str, int, str | None]], dict[str, int], int]:
    """Returns (tags, bucket_counts, total_tags) for the given filter tab --
    tags is (tag, work_count, explicit_category_or_None). Shared by the
    admin classification page and the read-only Browse page: same
    underlying data, one mutable (checkboxes/bulk actions), one not.
    """
    counts: Counter = Counter()
    for entry in result.entries:
        for tag in entry.fandom_candidates:
            counts[tag] += 1

    explicit = db.get_all_tag_categories(DB_PATH)
    bucket_counts = {"fandom": 0, "character": 0, "relationship": 0, "freeform": 0, "unclassified": 0}
    for tag in counts:
        bucket_counts[explicit.get(tag, "unclassified")] += 1

    tags = [(tag, count, explicit.get(tag)) for tag, count in counts.items()]
    if filter != "all":
        tags = [(t, c, cat) for t, c, cat in tags if (cat or "unclassified") == filter]
    tags = _sort_name_count_rows(tags, sort)
    return tags, bucket_counts, len(counts)


@app.get("/tags", response_class=HTMLResponse)
def tags_browse(request: Request, filter: str = "all", page: int = 1, sort: str = DEFAULT_NAME_COUNT_SORT):
    """Read-only tag browsing under Browse -- anyone logged in can see
    this, unlike /tags/classify (admin-only, see the module-level
    ADMIN_PATH_PREFIXES) which actually changes the shared classification.
    """
    result = scanner.load_cached(DB_PATH)
    tags, bucket_counts, total_tags = _tag_rows(result, filter, sort)
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
            "pager_qs": f"&filter={quote(filter)}&sort={quote(sort)}",
        },
    )


@app.get("/tags/classify", response_class=HTMLResponse)
def tags_classify_page(request: Request, filter: str = "all", page: int = 1, sort: str = DEFAULT_NAME_COUNT_SORT):
    result = scanner.load_cached(DB_PATH)
    tags, bucket_counts, total_tags = _tag_rows(result, filter, sort)
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
            "pager_qs": f"&filter={quote(filter)}&sort={quote(sort)}",
        },
    )


@app.post("/tags/classify/set_selected")
def set_selected_tags(
    tags: list[str] = Form([]),
    category: str = Form(...),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
):
    if category in ("fandom", "character", "relationship", "freeform") and tags:
        db.set_tag_categories(DB_PATH, {t: category for t in tags})
    return RedirectResponse(
        url=f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}", status_code=303
    )


@app.post("/tags/classify/mark_page_freeform")
def mark_page_freeform(
    tags: list[str] = Form([]),
    filter: str = Form("all"),
    page: int = Form(1),
    sort: str = Form(DEFAULT_NAME_COUNT_SORT),
):
    explicit = db.get_all_tag_categories(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in tags if t not in explicit})
    return RedirectResponse(
        url=f"/tags/classify?filter={quote(filter)}&page={page}&sort={quote(sort)}", status_code=303
    )


@app.post("/tags/classify/mark_all_unclassified_freeform")
def mark_all_unclassified_freeform():
    result = scanner.load_cached(DB_PATH)
    all_tags = {t for e in result.entries for t in e.fandom_candidates}
    explicit = db.get_all_tag_categories(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in all_tags if t not in explicit})
    return RedirectResponse(url="/tags/classify", status_code=303)


@app.get("/fandoms", response_class=HTMLResponse)
def fandoms(request: Request, sort: str = DEFAULT_NAME_COUNT_SORT):
    result = scanner.load_cached(DB_PATH)
    counts = Counter()
    for entry in result.entries:
        for name in entry.fandoms:
            counts[name] += 1
    sorted_fandoms = _sort_name_count_rows(list(counts.items()), sort)
    return templates.TemplateResponse(
        "fandoms.html",
        {
            **_base_context(request),
            "fandoms": sorted_fandoms,
            "sort": sort,
            "sort_options": NAME_COUNT_SORT_LABELS,
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


@app.get("/queue", response_class=HTMLResponse)
def queue(request: Request):
    """Tracked-feed entries that still need attention: not downloaded at
    all, or possibly out of date. A first cut over the same status rss
    already computes for the Tracked Feeds page -- expected to grow.

    The same work can be tracked through more than one feed (e.g. it
    matches two tags you follow) -- dedupe by work_id so it shows up once,
    keeping whichever feed's copy has the most recent feed_updated info
    and listing every feed it came from.
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

    items = list(by_work_id.values())
    items.sort(key=lambda item: (status_order[item["status"]], (item["entry"].title or "").lower()))

    return templates.TemplateResponse(
        "queue.html",
        {
            **_base_context(request),
            "items": items,
        },
    )


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


@app.post("/refresh")
def refresh(next: str = Form("/")):
    """Rescans the downloads folder/log only -- kept separate from feed
    refreshing (see /refresh/feeds) since walking a large downloads folder
    and hitting every tracked feed on every click was too much to always
    bundle together. Also re-matches against Audiobookshelf if configured --
    for matched works, Audiobookshelf's own already-scanned title/tags are
    used in place of parsing the epub file again (see scanner._scan_disk),
    so matches are loaded first and threaded into the same scan.
    """
    abs_matches = {}
    if ABS_DB_PATH and ABS_LIBRARY_ID:
        abs_matches = audiobookshelf.load_matches(ABS_DB_PATH, ABS_LIBRARY_ID)

    scanner.refresh_cache(DOWNLOAD_DIR, LOG_PATH, DB_PATH, abs_matches)
    if ABS_DB_PATH and ABS_LIBRARY_ID:
        db.save_abs_matches(DB_PATH, {work_id: m.item_id for work_id, m in abs_matches.items()})
        # Read status is per-app-account, not shared like the match above --
        # only accounts that have paired an Audiobookshelf username (Account
        # page) get synced; everyone else stays on the manual toggle alone.
        for user_id, abs_username in db.list_user_abs_usernames(DB_PATH).items():
            finished = audiobookshelf.load_read_work_ids(ABS_DB_PATH, ABS_LIBRARY_ID, abs_username)
            db.save_abs_read_status(DB_PATH, user_id, finished)
    db.set_meta(DB_PATH, LAST_REFRESHED_KEY, datetime.now().isoformat())
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
            "theme_css": context["theme_css"] or "",
            "abs_username": db.get_user_abs_username(DB_PATH, request.state.user.id) or "",
        },
    )


@app.post("/account/theme")
def save_theme(request: Request, theme_css: str = Form("")):
    """Raw CSS, applied only to this user's own page loads (see base.html,
    translate_ao3_skin_selectors, and sanitize_style_content). A handful of
    common AO3 skin selectors (#header, #dashboard, .splash, etc.) are
    rewritten onto this app's closest equivalent element; anything else a
    skin targets that has no equivalent here silently no-ops.
    """
    db.set_user_theme_css(DB_PATH, request.state.user.id, theme_css)
    return RedirectResponse(url="/account?saved=theme", status_code=303)


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
