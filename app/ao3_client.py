"""Drives ao3downloader's own internal classes directly instead of its
interactive CLI menu (ao3downloader.actions.*), which every action prompts
through input()/getpass() for and has no non-interactive entry point at
all. The classes underneath that menu -- Repository (web requests),
FileOps (settings/logging/paths), and Ao3 (the actual scrape-and-save
logic) -- have no interactive prompts of their own; the menu layer is only
a thin human-facing shell around them. Repository.login(username,
password) and Ao3.download(url) are plain, ordinary blocking calls.

Login is optional: AO3_USERNAME/AO3_PASSWORD (see main.py) are only needed
for restricted/mature-locked-behind-login works or a logged-in user's own
reading history -- a queue of ordinary public works downloads fine with no
credentials configured at all.

Ao3.download() never raises on a per-work failure -- it catches its own
exceptions and records them via FileOps.write_log (ao3downloader/ao3.py),
the exact log.jsonl format this app's own scanner already parses. That
means a failed download shows up on this app's Issues page automatically,
the same way a failure from a manual ao3downloader run already does --
this module doesn't need its own separate failure-tracking to get that.
"""

import os

from ao3downloader.ao3 import Ao3
from ao3downloader.fileio import FileOps
from ao3downloader.repo import Repository

# Sane, deliberately conservative pacing/retry defaults -- these mirror
# ao3downloader's own packaged settings.ini (see its "MaxRetries"/
# "MaxTimeouts" comments), except ExtraWaitTime: since this runs
# unattended over a queue that can be thousands of works long, a small
# baseline delay between requests (on top of ao3downloader's own
# rate-limit backoff, which only kicks in once AO3 actually throttles)
# is a more polite default than the packaged "0".
_INI_TEMPLATE = """[settings]
ExtraWaitTime={extra_wait}
MaxRetries=30
MaxTimeouts=3
SavePassword=false
FileNameLength=0
FileNamePattern={{worknum}}_{{title}} - {{author}}
EnableDebugLogging=false
DownloadFolder={download_dir}
"""

DEFAULT_EXTRA_WAIT_SECONDS = 2


class Ao3Client:
    """Bundles the Repository + Ao3 pair a download worker needs, plus the
    session cleanup Repository's own context-manager protocol expects.
    Construct one per worker run (see main.py's download worker loop) --
    it holds an open requests.Session for the run's lifetime.
    """

    def __init__(self, repo: Repository, ao3: Ao3):
        self.repo = repo
        self.ao3 = ao3

    def download(self, url: str) -> None:
        self.ao3.download(url)

    def close(self) -> None:
        self.repo.session.close()


def build_client(
    download_dir: str,
    log_path: str,
    state_dir: str,
    username: str = "",
    password: str = "",
    extra_wait: int = DEFAULT_EXTRA_WAIT_SECONDS,
) -> Ao3Client:
    """Points ao3downloader's own paths at this app's paths directly
    (rather than the CWD-relative defaults FileOps.initialize() would set
    up) -- FileOps() alone does no I/O beyond a best-effort read of a
    settings.ini that won't exist yet, so overwriting its path attributes
    immediately after construction is safe; only .initialize() (never
    called here) would have created folders/copied the packaged ini.
    `state_dir` holds a small generated settings.ini (pacing/retry knobs)
    and ao3downloader's own data.json (nothing sensitive -- just its
    saved filetypes list; login isn't cached there since credentials come
    from environment variables here every run instead of its own prompt-
    and-save flow).
    """
    os.makedirs(state_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

    ini_path = os.path.join(state_dir, "ao3downloader_settings.ini")
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write(_INI_TEMPLATE.format(extra_wait=extra_wait, download_dir=download_dir))

    fileops = FileOps()
    fileops.inifile = ini_path
    fileops.settingsfile = os.path.join(state_dir, "ao3downloader_data.json")
    fileops.logfile = log_path
    fileops.downloadfolder = download_dir

    repo = Repository(fileops)
    if username and password:
        repo.login(username, password)

    ao3 = Ao3(repo, fileops, filetypes=["EPUB"], pages=None, series=False, images=False)
    return Ao3Client(repo, ao3)
