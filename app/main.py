import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import scanner

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/downloads")
LOG_PATH = os.environ.get("LOG_PATH", "/logs/log.jsonl")

BASE_DIR = os.path.dirname(__file__)

app = FastAPI(title="AO3 Downloads Viewer")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


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
def dashboard(request: Request):
    result = scanner.scan(DOWNLOAD_DIR, LOG_PATH)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "entries": result.entries,
            "stats": result.stats,
            "download_dir": DOWNLOAD_DIR,
            "log_path": LOG_PATH,
            "log_exists": os.path.isfile(LOG_PATH),
        },
    )
