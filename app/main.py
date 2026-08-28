import os
from collections import Counter

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, rss, scanner

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")
DB_PATH = os.environ.get("DB_PATH", "/data/app.db")

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


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, fandom: str | None = None):
    result = scanner.scan(DOWNLOAD_DIR, LOG_PATH, DB_PATH)
    entries = result.entries
    if fandom:
        entries = [e for e in entries if fandom in e.fandoms]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
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
    result = scanner.scan(DOWNLOAD_DIR, LOG_PATH, DB_PATH)
    issue_entries = [e for e in result.entries if e.issue_type]
    if not show_dismissed:
        issue_entries = [e for e in issue_entries if not e.dismissed]
    return templates.TemplateResponse(
        "issues.html",
        {
            "request": request,
            "entries": issue_entries,
            "show_dismissed": show_dismissed,
        },
    )


@app.post("/issues/{work_id}/dismiss")
def dismiss_issue(work_id: str, dismissed: bool = Form(...)):
    db.set_dismissed(DB_PATH, work_id, dismissed)
    return RedirectResponse(url="/issues", status_code=303)


@app.post("/issues/{work_id}/edit")
def edit_issue(
    work_id: str,
    title: str = Form(""),
    author: str = Form(""),
    fandoms: str = Form(""),
):
    fandom_list = [f.strip() for f in fandoms.split(",") if f.strip()]
    db.set_fields(
        DB_PATH,
        work_id,
        title=title.strip() or None,
        author=author.strip() or None,
        fandoms=fandom_list or None,
    )
    return RedirectResponse(url="/issues", status_code=303)


@app.get("/fandoms", response_class=HTMLResponse)
def fandoms(request: Request):
    result = scanner.scan(DOWNLOAD_DIR, LOG_PATH, DB_PATH)
    counts = Counter()
    for entry in result.entries:
        for name in entry.fandoms:
            counts[name] += 1
    sorted_fandoms = sorted(counts.items(), key=lambda pair: pair[0].lower())
    return templates.TemplateResponse(
        "fandoms.html",
        {
            "request": request,
            "fandoms": sorted_fandoms,
        },
    )


@app.get("/tracked", response_class=HTMLResponse)
def tracked(request: Request):
    result = scanner.scan(DOWNLOAD_DIR, LOG_PATH, DB_PATH)
    local_by_id = {e.work_id: e for e in result.entries}

    feeds = []
    for feed in db.list_tracked_feeds(DB_PATH):
        try:
            feed_result = rss.fetch_feed(feed.url)
        except rss.FeedFetchError as exc:
            feeds.append({"feed": feed, "title": None, "rows": [], "error": str(exc)})
            continue

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
        feeds.append({"feed": feed, "title": feed_result.title, "rows": rows, "error": None})

    return templates.TemplateResponse(
        "tracked.html",
        {
            "request": request,
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
