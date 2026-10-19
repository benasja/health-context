#!/usr/bin/env python3
"""Keep the context pack up to date from a Health Bridge receiver, unattended.

Meant to run all the time next to the receiver (a home server, a Docker
container): every few minutes it checks whether the receiver's database has
changed, and if so rebuilds the outputs from it. The MCP server is pointed at
`<out-root>/current` and picks the new data up on its next tool call.

    python3 health_sync.py --bridge-db ~/.local/share/health-bridge/health.sqlite \\
        --out-root ~/health-context-data

    python3 health_mcp.py --data-dir ~/health-context-data/current

Two things make this more than a loop around convert_health_data.py:

* **Nothing is ever read half-written.** A rebuild writes a dozen files over
  several seconds. Writing them in place would let an MCP call that lands in
  the middle read this hour's daily metrics against last hour's weekly
  summary. Instead every rebuild goes into a new folder under `snapshots/`,
  and only once it is complete does `current` switch to it, in one atomic
  rename. A failed rebuild switches nothing: `current` keeps serving the last
  good one, and the failure is in the log.
* **The receiver is never held up.** Converting takes seconds, and a reader
  holding the database open that long would make the receiver wait to commit
  -- with the phone retrying meanwhile. So the health tables are first copied
  out in one read transaction, which takes about a second and gives one
  consistent moment across all of them, and the conversion runs on the copy.

Layout of --out-root:

    current -> snapshots/20261016T081500Z
    snapshots/20261016T081500Z/   the newest complete rebuild
    snapshots/20261016T074500Z/   kept so a session reading it can finish
    profile.md                    optional; used for every rebuild if present
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONVERTER = os.path.join(HERE, 'convert_health_data.py')

CURRENT = 'current'
SNAPSHOTS = 'snapshots'
WORK = '.work'
STAMP_FORMAT = '%Y%m%dT%H%M%SZ'
STAMP_RE = re.compile(r'^\d{8}T\d{6}Z$')

# How many complete snapshots to keep. The MCP server re-reads `current` at
# the start of each tool call and then reads that one folder, so the oldest
# kept snapshot only has to outlive a single call. Two would do; three leaves
# room for a rebuild that fails right after one that succeeded.
DEFAULT_KEEP = 3
DEFAULT_INTERVAL = 15 * 60
# Lines of converter output to show when a rebuild fails.
FAILURE_TAIL = 30


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')
    print(f'{stamp} {message}', flush=True)


def fingerprint(db_path: str) -> tuple:
    """Something that changes whenever the receiver commits.

    The size and mtime of the database, plus its -wal file if the receiver
    runs in WAL mode -- there, a commit lands in the -wal file and the main
    file does not change until a checkpoint.
    """
    parts = []
    for path in (db_path, db_path + '-wal'):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            parts.append(None)
            continue
        parts.append((st.st_size, st.st_mtime_ns))
    return tuple(parts)


def copy_database(db_path: str, target: str) -> None:
    """Copy the tables the importer reads into a private file, consistently.

    All of them are copied inside one read transaction, so the copy is a
    single moment even if the phone is syncing right then -- something reading
    the tables one query at a time could not promise. Only the health tables
    and their lookups come across: the receiver's pairing and credential
    tables never leave its folder, not even as a temporary file.
    """
    import health_bridge_import as bridge

    source = bridge.open_readonly(db_path)
    try:
        bridge.check_schema(source)
        source.execute('attach database ? as copy',
                       (pathlib.Path(target).as_uri() + '?mode=rwc',))
        source.execute('begin')
        for table in bridge.REQUIRED_COLUMNS:
            source.execute(f'create table copy.{table} as select * from main.{table}')
        source.execute('commit')
    finally:
        source.close()


def snapshot_names(snapshot_dir: str) -> list[str]:
    try:
        names = os.listdir(snapshot_dir)
    except FileNotFoundError:
        return []
    return sorted(n for n in names if STAMP_RE.match(n))


def current_snapshot(out_root: str) -> str | None:
    link = os.path.join(out_root, CURRENT)
    if not os.path.islink(link):
        return None
    return os.path.basename(os.readlink(link))


def switch_current(out_root: str, name: str) -> None:
    """Point `current` at snapshots/<name> in one step.

    os.replace over an existing symlink is an atomic rename on POSIX: a
    reader resolving `current` gets the old folder or the new one, never
    neither. The link is relative so it still resolves when the folder is
    mounted somewhere else, e.g. on the host outside a container.
    """
    link = os.path.join(out_root, CURRENT)
    if os.path.exists(link) and not os.path.islink(link):
        raise SystemExit(f'{link} exists and is not a link; move it out of the way first')
    tmp = os.path.join(out_root, f'.{CURRENT}.tmp')
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(os.path.join(SNAPSHOTS, name), tmp)
    os.replace(tmp, link)


def prune(out_root: str, keep: int) -> list[str]:
    """Delete all but the newest `keep` snapshots, never the current one."""
    snapshot_dir = os.path.join(out_root, SNAPSHOTS)
    names = snapshot_names(snapshot_dir)
    current = current_snapshot(out_root)
    removed = []
    for name in names[:-keep] if keep else names:
        if name == current:
            continue
        shutil.rmtree(os.path.join(snapshot_dir, name), ignore_errors=True)
        removed.append(name)
    return removed


def clear_leftovers(out_root: str) -> None:
    """Remove what an interrupted rebuild left behind (killed mid-run, power cut)."""
    shutil.rmtree(os.path.join(out_root, WORK), ignore_errors=True)
    snapshot_dir = os.path.join(out_root, SNAPSHOTS)
    for name in os.listdir(snapshot_dir) if os.path.isdir(snapshot_dir) else []:
        if name.endswith('.partial'):
            shutil.rmtree(os.path.join(snapshot_dir, name), ignore_errors=True)


def new_stamp(snapshot_dir: str) -> str:
    stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    # Two rebuilds within the same second (--once in a tight loop, a test)
    # would otherwise reuse a name; wait for the next one.
    while os.path.exists(os.path.join(snapshot_dir, stamp)):
        time.sleep(0.2)
        stamp = datetime.now(timezone.utc).strftime(STAMP_FORMAT)
    return stamp


def converter_command(args: argparse.Namespace, db_copy: str, out_dir: str) -> list[str]:
    command = [sys.executable, CONVERTER, '--bridge-db', db_copy, '--out-dir', out_dir,
               '--skip-legacy']
    if args.timezone:
        command += ['--timezone', args.timezone]
    if args.max_hr:
        command += ['--max-hr', str(args.max_hr)]
    if args.age:
        command += ['--age', str(args.age)]
    profile = args.profile or os.path.join(args.out_root, 'profile.md')
    if os.path.isfile(profile):
        # The converter looks for profile.md next to its output, and a fresh
        # snapshot folder never has one, so name it explicitly.
        command += ['--profile', profile]
    return command


def rebuild(args: argparse.Namespace) -> str | None:
    """Build one snapshot and make it current. Returns its name, or None on failure."""
    out_root = args.out_root
    snapshot_dir = os.path.join(out_root, SNAPSHOTS)
    work = os.path.join(out_root, WORK)
    os.makedirs(snapshot_dir, exist_ok=True)
    os.makedirs(work, exist_ok=True)

    name = new_stamp(snapshot_dir)
    partial = os.path.join(snapshot_dir, name + '.partial')
    db_copy = os.path.join(work, 'health.sqlite')
    started = time.monotonic()
    try:
        try:
            copy_database(args.bridge_db, db_copy)
        except Exception as exc:  # noqa: BLE001 - logged, retried next round
            log(f'could not read {args.bridge_db}: {exc}')
            return None

        result = subprocess.run(converter_command(args, db_copy, partial),
                                capture_output=True, text=True)
        if result.returncode != 0:
            output = (result.stdout + result.stderr).strip().splitlines()
            log(f'rebuild failed (exit {result.returncode}); still serving '
                f'{current_snapshot(out_root) or "nothing yet"}. Converter said:')
            for line in output[-FAILURE_TAIL:]:
                print('    ' + line, flush=True)
            shutil.rmtree(partial, ignore_errors=True)
            return None

        _note_check_interval(partial, args.interval)
        os.rename(partial, os.path.join(snapshot_dir, name))
        switch_current(out_root, name)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    removed = prune(out_root, args.keep)
    summary = next((line.strip() for line in result.stdout.splitlines()
                    if line.strip().startswith('Read ')), '')
    log(f'now serving {name} ({time.monotonic() - started:.1f}s). {summary}'
        + (f' Removed {len(removed)} old snapshot(s).' if removed else ''))
    return name


def _note_check_interval(snapshot: str, interval_s: float) -> None:
    """Add how often this loop looks for new data to the build's status file,
    so an answer can say when newer data could next appear."""
    path = os.path.join(snapshot, 'data_status.json')
    try:
        with open(path, encoding='utf-8') as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return
    status['check_interval_s'] = int(interval_s)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(status, handle, indent=1)


def lock(out_root: str):
    """Refuse to run twice against the same folder.

    Two loops would each prune the other's snapshots. The lock is released by
    the kernel when the process dies, so a crash never leaves it stuck.
    """
    try:
        import fcntl
    except ImportError:  # Windows: no flock; the receiver does not run there either
        return None
    handle = open(os.path.join(out_root, '.lock'), 'w')  # noqa: SIM115 - held for life
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f'Another health_sync is already writing to {out_root}') from None
    return handle


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--bridge-db', required=True,
                   help="Path to the receiver's health.sqlite (opened read-only)")
    p.add_argument('--out-root', required=True,
                   help='Folder to keep snapshots in; point the MCP server at <out-root>/current')
    p.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                   help=f'Seconds between checks for new data (default {DEFAULT_INTERVAL})')
    p.add_argument('--keep', type=int, default=DEFAULT_KEEP,
                   help=f'Complete snapshots to keep, at least 2 (default {DEFAULT_KEEP})')
    p.add_argument('--once', action='store_true',
                   help='Rebuild once and exit; non-zero if that rebuild failed')
    p.add_argument('--timezone', default=None,
                   help="IANA timezone for calendar days (default: the phone's own)")
    p.add_argument('--profile', default=None,
                   help='profile.md to include (default: <out-root>/profile.md if present)')
    p.add_argument('--max-hr', type=float, default=None, help='Passed to the converter')
    p.add_argument('--age', type=int, default=None, help='Passed to the converter')
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.bridge_db = os.path.abspath(os.path.expanduser(args.bridge_db))
    args.out_root = os.path.abspath(os.path.expanduser(args.out_root))
    if args.keep < 2:
        # With one, the folder an MCP call is reading could be deleted under it.
        raise SystemExit('--keep must be at least 2')
    os.makedirs(args.out_root, exist_ok=True)

    # Under Docker this is PID 1, which gets no default SIGTERM handler: without
    # this, `docker stop` waits ten seconds and then kills it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    held = lock(args.out_root)  # noqa: F841 - must stay referenced
    clear_leftovers(args.out_root)

    # The fingerprint is not persisted, so a restart always rebuilds once. That
    # is deliberate: it is the only way a new version of this code ever gets
    # to recompute an unchanged database.
    built_from = None
    log(f'watching {args.bridge_db}' + ('' if args.once else f' every {args.interval:g}s'))
    while True:
        seen = fingerprint(args.bridge_db)
        built = None
        if seen[0] is None:
            log(f'{args.bridge_db} does not exist yet; has the receiver been set up?')
        elif seen != built_from:
            built = rebuild(args)
            if built:
                built_from = seen
        if args.once:
            raise SystemExit(0 if built else 1)
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
