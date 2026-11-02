"""Keep the MCP server's data current from an export folder, with no commands to run.

The Claude Desktop extension starts `health_mcp.py --export ~/Downloads`. That
folder is where AirDrop puts the zip from the phone, so the whole routine for
someone who has never opened a terminal is: export on the phone, AirDrop it,
ask a question. This is the part that makes that work:

* finds the newest Apple Health export in the folder (a zip, an unzipped
  apple_health_export/ folder, or export.xml itself), checking what a zip holds
  rather than its name, since Downloads holds plenty of zips that aren't it;
* runs the converter on it in the background, because a first read takes a
  minute or two and a client gives up on a server that is silent that long;
* swaps the result in whole, under a lock the server holds while it answers,
  so an answer never mixes an old build with a new one;
* looks again on later calls, so the next export is picked up without a restart.

Until the first build is done, every tool answers with what is happening
instead of an error the reader can't act on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile

from health_ingest import EXPORT_NAME, NESTED_DIR, PROFILE_NAME

CONVERTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'convert_health_data.py')
CURRENT = 'current'
NEXT = 'next'
OLD = 'old'
BUILT_FROM = 'built_from.json'
BUILD_LOG = 'build.log'
LOOK_EVERY_S = 30
LOG_TAIL = 8


def default_home() -> str:
    """Where the extension keeps what it builds: outside Documents and Desktop,
    which macOS may be syncing to iCloud, and nowhere the person has to look."""
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
    elif sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'health-context')


def _zip_is_export(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            # Reads the zip's table of contents only, not the gigabyte inside.
            return any(n in (EXPORT_NAME, f'{NESTED_DIR}/{EXPORT_NAME}')
                       for n in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def find_export(source: str, known: dict[tuple, bool] | None = None) -> str | None:
    """The newest export at or in `source`, or None.

    `known` remembers which zips were already opened, keyed by path, size and
    modification time, so looking again every half minute costs a directory
    listing rather than reopening every zip in Downloads.
    """
    source = os.path.abspath(os.path.expanduser(source))
    if os.path.isfile(source):
        if os.path.basename(source) == EXPORT_NAME:
            return os.path.dirname(source)
        return source if source.lower().endswith('.zip') else None
    if not os.path.isdir(source):
        return None
    for candidate in (os.path.join(source, EXPORT_NAME),
                      os.path.join(source, NESTED_DIR, EXPORT_NAME)):
        if os.path.isfile(candidate):
            return os.path.dirname(candidate)
    known = {} if known is None else known
    found = []
    try:
        names = os.listdir(source)
    except OSError:
        return None
    for name in names:
        if not name.lower().endswith('.zip'):
            continue
        path = os.path.join(source, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = (path, st.st_size, st.st_mtime_ns)
        if key not in known:
            known[key] = _zip_is_export(path)
        if known[key]:
            found.append((st.st_mtime_ns, path))
    return max(found)[1] if found else None


def fingerprint(path: str) -> dict:
    """What identifies one export: where it is, its size and when it was written."""
    target = os.path.join(path, EXPORT_NAME) if os.path.isdir(path) else path
    st = os.stat(target)
    return {'path': path, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


class ExportWatcher:
    """Builds `home/current` from the newest export in `source`, in the background."""

    def __init__(self, source: str, home: str | None = None, profile: str | None = None):
        self.source = os.path.abspath(os.path.expanduser(source))
        self.home = os.path.abspath(os.path.expanduser(home or default_home()))
        self.profile = profile
        self.data_dir = os.path.join(self.home, CURRENT)
        # Held by the server while it answers and by a build while it swaps.
        self.lock = threading.Lock()
        self._known: dict[tuple, bool] = {}
        self._thread: threading.Thread | None = None
        self._looked_at = 0.0
        self.building: str | None = None     # the export being read right now
        self.started = 0.0
        self.error = ''
        self._failed: dict | None = None    # the export that error came from
        os.makedirs(self.home, exist_ok=True)
        self._recover()

    def _recover(self) -> None:
        # A swap interrupted between its two renames leaves only `old`.
        old, current = os.path.join(self.home, OLD), self.data_dir
        if os.path.isdir(old) and not os.path.isdir(current):
            os.rename(old, current)
        for leftover in (NEXT, OLD):
            shutil.rmtree(os.path.join(self.home, leftover), ignore_errors=True)

    def built_from(self) -> dict:
        try:
            with open(os.path.join(self.home, BUILT_FROM), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def poll(self, force: bool = False) -> None:
        """Start a build if there is an export newer than the one being served."""
        if self._thread and self._thread.is_alive():
            return
        now = time.monotonic()
        if not force and now - self._looked_at < LOOK_EVERY_S:
            return
        self._looked_at = now
        export = find_export(self.source, self._known)
        if export is None:
            return
        try:
            wanted = fingerprint(export)
        except OSError:
            return
        # A changed profile is a reason to rebuild too: it heads the briefing.
        try:
            wanted['profile_mtime_ns'] = os.stat(self.profile_path()).st_mtime_ns
        except OSError:
            wanted['profile_mtime_ns'] = None
        if wanted == self.built_from() and os.path.isdir(self.data_dir):
            return
        if self.error and wanted == self._failed:
            return  # the same export failed already; don't retry it every 30 s
        self.building, self.started, self.error = export, time.time(), ''
        self._thread = threading.Thread(target=self._build, args=(export, wanted), daemon=True)
        self._thread.start()

    def profile_path(self) -> str:
        return self.profile or os.path.join(self.home, PROFILE_NAME)

    def _command(self, export: str, out_dir: str) -> list[str]:
        command = [sys.executable, CONVERTER, '--data-dir', export, '--out-dir', out_dir,
                   '--skip-legacy']
        profile = self.profile_path()
        if os.path.isfile(profile):
            command += ['--profile', profile]
        return command

    def _build(self, export: str, wanted: dict) -> None:
        nxt = os.path.join(self.home, NEXT)
        shutil.rmtree(nxt, ignore_errors=True)
        try:
            # Its output goes to a log, never to stdout: stdout is the MCP
            # connection, and one stray progress bar would break it.
            with open(os.path.join(self.home, BUILD_LOG), 'w', encoding='utf-8') as log:
                result = subprocess.run(self._command(export, nxt), stdout=log,
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            if result.returncode != 0 or not os.path.isfile(os.path.join(nxt, 'daily_metrics.csv')):
                raise RuntimeError(self._log_tail() or f'the converter exited {result.returncode}')
            # The unpacked export.xml is a gigabyte, and the CSVs are all that is read.
            shutil.rmtree(os.path.join(nxt, '.extracted'), ignore_errors=True)
            old = os.path.join(self.home, OLD)
            with self.lock:
                if os.path.isdir(self.data_dir):
                    os.rename(self.data_dir, old)
                os.rename(nxt, self.data_dir)
                with open(os.path.join(self.home, BUILT_FROM), 'w', encoding='utf-8') as f:
                    json.dump(wanted, f)
            shutil.rmtree(old, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - reported to the reader, the server keeps going
            self.error = str(exc).strip()
            self._failed = wanted
            shutil.rmtree(nxt, ignore_errors=True)
        finally:
            self.building = None

    def _log_tail(self) -> str:
        try:
            with open(os.path.join(self.home, BUILD_LOG), encoding='utf-8', errors='replace') as f:
                lines = [line.rstrip() for line in f if line.strip()]
        except OSError:
            return ''
        # The progress bar redraws one line with carriage returns.
        return '\n'.join(line.split('\r')[-1] for line in lines[-LOG_TAIL:])

    # -- what the reader is told

    def note(self) -> str:
        """One line for above an answer while a newer export is being read."""
        if self.building and os.path.isdir(self.data_dir):
            return ('_A newer export was found and is being read; until that is done, these '
                    'answers come from the previous one._')
        if self.error and os.path.isdir(self.data_dir):
            return (f'_The newest export could not be read, so these answers come from the '
                    f'previous one. The converter said: {self.error.splitlines()[-1]}_')
        return ''

    def waiting_message(self) -> str:
        """What every tool says before there is anything to answer from."""
        if self.building:
            minutes = round((time.time() - self.started) / 60)
            since = 'just now' if minutes < 1 else f'{minutes} min ago'
            return (
                'Reading the Apple Health export for the first time '
                f'({os.path.basename(self.building)}, started {since}). '
                'A large export takes one to three minutes. Tell the user it is nearly ready '
                'and to ask again shortly; nothing is wrong.'
            )
        if self.error:
            return (
                f'The Apple Health export in {self.source} could not be read. '
                f'The converter said:\n\n{self.error}\n\n'
                'Tell the user what it said. A fresh export from the Health app usually fixes it.'
            )
        return (
            f'There is no Apple Health export in {self.source} yet. Tell the user how to make one:\n\n'
            '1. On the iPhone, open the Health app, tap the profile picture (top right), then '
            '"Export All Health Data". It takes a few minutes.\n'
            f'2. Send the zip to this computer, into {self.source}. AirDrop to a Mac puts it '
            'in Downloads. Leave it zipped.\n'
            '3. Ask again. It is picked up automatically, with no restart.\n\n'
            'Nothing is uploaded anywhere: the export is read on this computer.'
        )
