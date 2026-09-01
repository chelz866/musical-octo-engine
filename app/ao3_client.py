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

A redownload (Queue/Incomplete Works "Download Selected" on a work you
already have) only actually replaces the existing file in place if
ao3downloader computes the exact same filename for it as last time --
otherwise it just writes a second file under a different name, leaving
the original (with its original, increasingly stale mtime) sitting there
too. Since this app can't know for certain what FileNamePattern a given
library's files were originally created with (a hand-run ao3downloader
might have used the packaged default, a customized one, or been changed
over time), Ao3Client.download() closes that gap itself afterward: it
looks at every file matching this work_id (scanner.FILENAME_RE, the same
"<id> or <id>_ prefix" rule the scanner itself uses) and removes all but
the most recently written one, so a redownload can never silently leave
a stale duplicate behind for the scanner to pick over the fresh copy.
Nothing is removed unless the download actually produced a file --
a failed attempt leaves the sole existing copy untouched.
"""

import os

from ao3downloader.ao3 import Ao3
from ao3downloader.fileio import FileOps
from ao3downloader.repo import Repository

from . import scanner

# Sane, deliberately conservative pacing/retry defaults -- these mirror
# ao3downloader's own packaged settings.ini (see its "MaxRetries"/
# "MaxTimeouts" comments), except ExtraWaitTime: since this runs
# unattended over a queue that can be thousands of works long, a small
# baseline delay between requests (on top of ao3downloader's own
# rate-limit backoff, which only kicks in once AO3 actually throttles)
# is a more polite default than the packaged "0". FileNamePattern matches
# ao3downloader's own packaged default exactly (not a pattern of this
# app's own invention) so a redownload is more likely to compute the same
# filename an existing hand-run download already used -- see the "not
# guaranteed" caveat above, which is what the post-download dedupe below
# is actually for.
_INI_TEMPLATE = """[settings]
ExtraWaitTime={extra_wait}
MaxRetries=30
MaxTimeouts=3
SavePassword=false
FileNameLength=0
FileNamePattern={{worknum}} {{title}} - {{author}}
EnableDebugLogging=false
DownloadFolder={download_dir}
"""

DEFAULT_EXTRA_WAIT_SECONDS = 2


def _work_id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _remove_stale_duplicate_files(download_dir: str, work_id: str) -> None:
    matches = []
    try:
        names = os.listdir(download_dir)
    except OSError:
        return
    for name in names:
        match = scanner.FILENAME_RE.match(name)
        if match and match.group(1) == work_id:
            path = os.path.join(download_dir, name)
            try:
                matches.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if len(matches) <= 1:
        return
    matches.sort(reverse=True)
    for _, stale_path in matches[1:]:
        try:
            os.remove(stale_path)
        except OSError:
            pass


class Ao3Client:
    """Bundles the Repository + Ao3 pair a download worker needs, plus the
    session cleanup Repository's own context-manager protocol expects.
    Construct one per worker run (see main.py's download worker loop) --
    it holds an open requests.Session for the run's lifetime.

    login_error is None whenever login wasn't attempted (no credentials
    configured) or succeeded; otherwise it's Repository.login's own
    exception message (wrong password, an AO3-side challenge blocking an
    automated login, etc.) -- see build_client, which catches that
    exception itself so a bad login can't take the whole client down.
    """

    def __init__(self, repo: Repository, ao3: Ao3, download_dir: str, login_error: str | None = None):
        self.repo = repo
        self.ao3 = ao3
        self.download_dir = download_dir
        self.login_error = login_error

    def download(self, url: str) -> None:
        self.ao3.download(url)
        _remove_stale_duplicate_files(self.download_dir, _work_id_from_url(url))

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

    A failed login (wrong password, or AO3 blocking what looks like an
    automated login) doesn't raise out of here -- Repository.login raises
    on failure, and letting that propagate would previously take the
    whole client-build step down before a single item was even attempted,
    silently killing the download worker with nothing downloaded and
    nothing logged (see main.py's login-error banner on Queue for how
    this surfaces instead). Ordinary public works need no login at all,
    so it's better to still build a working (unauthenticated) client and
    let those keep downloading than to fail the whole batch over it.
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
    login_error = None
    if username and password:
        try:
            repo.login(username, password)
        except Exception as exc:
            login_error = str(exc)

    ao3 = Ao3(repo, fileops, filetypes=["EPUB"], pages=None, series=False, images=False)
    return Ao3Client(repo, ao3, download_dir, login_error=login_error)
