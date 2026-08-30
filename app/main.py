import asyncio
import math
import os
from collections import Counter
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import audiobookshelf, db, rss, scanner

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")
FEEDS_DB_PATH = os.environ.get("FEEDS_DB_PATH", "/data/feeds.sqlite")
AUTO_REFRESH_INTERVAL_SECONDS = int(os.environ.get("AUTO_REFRESH_INTERVAL_SECONDS", 60 * 60))

DOWNLOADS_PAGE_SIZE = 25
TAGS_PAGE_SIZE = 100

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

    for url, label in db.pop_legacy_tracked_feeds(DB_PATH):
        try:
            rss.add_tracked_feed(FEEDS_DB_PATH, url, label)
        except rss.FeedRefreshError:
            pass  # best-effort; the user can re-add manually if a URL is stale

    asyncio.create_task(_auto_refresh_loop())


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

    # Chapters come from the epub's own preface page (see epub_meta.parse_epub_stats),
    # so completion status is real here, not a placeholder: chapters_total is only
    # ever set once the author commits to a total, so have < total is still a WIP
    # even with a definite total (e.g. "5/12").
    if entry.chapters_have is None:
        completion_class, completion_label, completion_symbol = "unknown", "Completion status unknown", ""
    elif entry.chapters_total is not None and entry.chapters_have >= entry.chapters_total:
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
    """
    tags = [{"text": w, "class": "tag-warning"} for w in entry.warnings]
    tags += [{"text": r, "class": "tag"} for r in entry.relationships]
    tags += [{"text": c, "class": "tag-character"} for c in entry.characters]
    tags += [{"text": t, "class": "tag"} for t in entry.freeform_tags]
    return tags


templates.env.filters["blurb_icons"] = blurb_icons
templates.env.filters["blurb_tag_line"] = blurb_tag_line


def _base_context(request: Request) -> dict:
    return {
        "request": request,
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


def paginate(items: list, page: int, page_size: int) -> tuple[list, int, int]:
    """Clamps page into [1, total_pages] and slices items to that page.
    Returns (page_items, clamped_page, total_pages). total_pages is always
    >= 1, even for an empty list, so callers never divide by zero.
    """
    total_pages = max(1, math.ceil(len(items) / page_size))
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, fandom: str | None = None, page: int = 1):
    result = scanner.load_cached(DB_PATH)
    entries = result.entries
    if fandom:
        entries = [e for e in entries if fandom in e.fandoms]

    page_entries, page, total_pages = paginate(entries, page, DOWNLOADS_PAGE_SIZE)

    pager_qs = f"&fandom={quote(fandom)}" if fandom else ""

    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_base_context(request),
            "entries": page_entries,
            "stats": result.stats,
            "download_dir": DOWNLOAD_DIR,
            "log_path": LOG_PATH,
            "log_exists": os.path.isfile(LOG_PATH),
            "fandom_filter": fandom,
            "abs_links": _abs_links(),
            "page": page,
            "total_pages": total_pages,
            "total_filtered": len(entries),
            "pager_qs": pager_qs,
        },
    )


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
    Unchecking a candidate here marks it Freeform, not Character -- this
    widget is a quick per-work fandom shortcut, not the full 3-way tool
    (see the Tags page for Character classification).
    """
    by_id = {e.work_id: e for e in scanner.load_cached(DB_PATH).entries}
    entry = by_id.get(work_id)
    candidates = entry.fandom_candidates if entry else []

    checked = set(fandoms)
    categories = {tag: ("fandom" if tag in checked else "freeform") for tag in candidates}
    for extra in (f.strip() for f in other_fandoms.split(",")):
        if extra:
            categories[extra] = "fandom"

    db.set_tag_categories(DB_PATH, categories)
    return RedirectResponse(url=next or "/", status_code=303)


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request, filter: str = "all", page: int = 1):
    result = scanner.load_cached(DB_PATH)
    counts: Counter = Counter()
    for entry in result.entries:
        for tag in entry.fandom_candidates:
            counts[tag] += 1

    explicit = db.get_all_tag_categories(DB_PATH)
    bucket_counts = {"fandom": 0, "character": 0, "freeform": 0, "unclassified": 0}
    for tag in counts:
        bucket_counts[explicit.get(tag, "unclassified")] += 1

    tags = [(tag, count, explicit.get(tag)) for tag, count in counts.items()]
    if filter != "all":
        tags = [(t, c, cat) for t, c, cat in tags if (cat or "unclassified") == filter]
    tags.sort(key=lambda row: (-row[1], row[0].lower()))

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
            "total_tags": len(counts),
            "pager_qs": f"&filter={quote(filter)}",
        },
    )


@app.post("/tags/set_selected")
def set_selected_tags(
    tags: list[str] = Form([]),
    category: str = Form(...),
    filter: str = Form("all"),
    page: int = Form(1),
):
    if category in ("fandom", "character", "freeform") and tags:
        db.set_tag_categories(DB_PATH, {t: category for t in tags})
    return RedirectResponse(url=f"/tags?filter={quote(filter)}&page={page}", status_code=303)


@app.post("/tags/mark_page_freeform")
def mark_page_freeform(tags: list[str] = Form([]), filter: str = Form("all"), page: int = Form(1)):
    explicit = db.get_all_tag_categories(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in tags if t not in explicit})
    return RedirectResponse(url=f"/tags?filter={quote(filter)}&page={page}", status_code=303)


@app.post("/tags/mark_all_unclassified_freeform")
def mark_all_unclassified_freeform():
    result = scanner.load_cached(DB_PATH)
    all_tags = {t for e in result.entries for t in e.fandom_candidates}
    explicit = db.get_all_tag_categories(DB_PATH)
    db.set_tag_categories(DB_PATH, {t: "freeform" for t in all_tags if t not in explicit})
    return RedirectResponse(url="/tags", status_code=303)


@app.get("/fandoms", response_class=HTMLResponse)
def fandoms(request: Request):
    result = scanner.load_cached(DB_PATH)
    counts = Counter()
    for entry in result.entries:
        for name in entry.fandoms:
            counts[name] += 1
    sorted_fandoms = sorted(counts.items(), key=lambda pair: pair[0].lower())
    return templates.TemplateResponse(
        "fandoms.html",
        {
            **_base_context(request),
            "fandoms": sorted_fandoms,
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
