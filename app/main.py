import os
from collections import Counter
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, rss, scanner

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")

LAST_REFRESHED_KEY = "last_refreshed_at"

BASE_DIR = os.path.dirname(__file__)

app = FastAPI(title="AO3 Downloads Viewer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.on_event("startup")
def _startup():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db.init_db(DB_PATH)


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


def _base_context(request: Request) -> dict:
    return {
        "request": request,
        "last_refreshed": db.get_meta(DB_PATH, LAST_REFRESHED_KEY),
        "refresh_error": request.query_params.get("refresh_error"),
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, fandom: str | None = None):
    result = scanner.load_cached(DB_PATH)
    entries = result.entries
    if fandom:
        entries = [e for e in entries if fandom in e.fandoms]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            **_base_context(request),
            "entries": entries,
            "stats": result.stats,
            "download_dir": DOWNLOAD_DIR,
            "log_path": LOG_PATH,
            "log_exists": os.path.isfile(LOG_PATH),
            "fandom_filter": fandom,
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
    """Classifies tags globally (see db.set_tag_flags), scoped to this
    work's own candidate tags plus whatever the user typed in "other" --
    it looks like a per-work edit, but the effect applies to every work
    that shares the same tag, since fandom is classified per tag now.
    """
    by_id = {e.work_id: e for e in scanner.load_cached(DB_PATH).entries}
    entry = by_id.get(work_id)
    candidates = entry.fandom_candidates if entry else []

    checked = set(fandoms)
    flags = {tag: (tag in checked) for tag in candidates}
    for extra in (f.strip() for f in other_fandoms.split(",")):
        if extra:
            flags[extra] = True

    db.set_tag_flags(DB_PATH, flags)
    return RedirectResponse(url=next or "/", status_code=303)


@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    result = scanner.load_cached(DB_PATH)
    counts: Counter = Counter()
    is_fandom_now: dict[str, bool] = {}
    for entry in result.entries:
        for tag in entry.fandom_candidates:
            counts[tag] += 1
            is_fandom_now[tag] = is_fandom_now.get(tag, False) or tag in entry.fandoms

    sorted_tags = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))
    return templates.TemplateResponse(
        "tags.html",
        {
            **_base_context(request),
            "tags": [(tag, count, is_fandom_now[tag]) for tag, count in sorted_tags],
        },
    )


@app.post("/tags/set")
def set_tags(all_tags: list[str] = Form([]), fandoms: list[str] = Form([])):
    checked = set(fandoms)
    db.set_tag_flags(DB_PATH, {tag: (tag in checked) for tag in all_tags})
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


@app.get("/tracked", response_class=HTMLResponse)
def tracked(request: Request):
    result = scanner.load_cached(DB_PATH)
    local_by_id = {e.work_id: e for e in result.entries}

    feeds = []
    for feed in db.list_tracked_feeds(DB_PATH):
        feed_result = rss.load_cached_feed(DB_PATH, feed)

        rows = []
        for entry in feed_result.entries:
            local_entry = local_by_id.get(entry.work_id)
            on_disk = bool(local_entry and local_entry.on_disk)
            local_timestamp = scanner.effective_timestamp(local_entry) if local_entry else None
            rows.append({
                "entry": entry,
                "on_disk": on_disk,
                "status": rss.assess_status(entry, on_disk, local_timestamp),
            })
        feeds.append({"feed": feed, "title": feed_result.title, "rows": rows})

    return templates.TemplateResponse(
        "tracked.html",
        {
            **_base_context(request),
            "feeds": feeds,
        },
    )


@app.post("/tracked/add")
def add_tracked_feed(url: str = Form(...), label: str = Form("")):
    db.add_tracked_feed(DB_PATH, url.strip(), label.strip() or None)
    return RedirectResponse(url="/tracked", status_code=303)


@app.post("/tracked/{feed_id}/delete")
def delete_tracked_feed(feed_id: int):
    db.delete_tracked_feed(DB_PATH, feed_id)
    return RedirectResponse(url="/tracked", status_code=303)


@app.post("/refresh")
def refresh(next: str = Form("/")):
    scanner.refresh_cache(DOWNLOAD_DIR, LOG_PATH, DB_PATH)

    errors = []
    for feed in db.list_tracked_feeds(DB_PATH):
        try:
            rss.refresh_feed_cache(DB_PATH, feed)
        except rss.FeedFetchError as exc:
            errors.append(f"{feed.label or feed.url}: {exc}")

    db.set_meta(DB_PATH, LAST_REFRESHED_KEY, datetime.now().isoformat())

    redirect_url = next or "/"
    if errors:
        sep = "&" if "?" in redirect_url else "?"
        redirect_url += f"{sep}refresh_error={quote('; '.join(errors))}"
    return RedirectResponse(url=redirect_url, status_code=303)
