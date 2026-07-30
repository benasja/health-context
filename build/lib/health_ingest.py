"""Locate and unpack an Apple Health export, wherever the user left it.

Apple's export arrives as a zip containing an `apple_health_export/` folder, and
lands in Downloads, or on the Desktop, or wherever AirDrop put it. Requiring
someone to unzip it and pass exactly the right folder is the step that makes an
otherwise simple tool feel like homework, so this accepts any of:

    a .zip file                      -> extracted
    a folder holding export.xml      -> used directly
    a folder holding apple_health_export/ -> descends into it
    a folder holding one or more .zip -> picks the newest and extracts it

Only the files actually parsed are extracted. A full export is ~1.4 GB unpacked,
of which the converter reads `export.xml` and the (tiny) ECG folder; the rest is
route GPX and a clinical-document mirror that is skipped by default.
"""

from __future__ import annotations

import glob
import os
import shutil
import zipfile
from dataclasses import dataclass

EXPORT_NAME = 'export.xml'
NESTED_DIR = 'apple_health_export'
CACHE_DIR = '.extracted'

# Extracted from the zip. export_cda.xml is deliberately absent: it is a
# clinical-document mirror that double-counts summed metrics and is off by
# default, and it is 400 MB.
WANTED_PREFIXES = ('export.xml', 'electrocardiograms/')


@dataclass
class Resolved:
    directory: str
    source: str          # what the user actually pointed at
    extracted: bool      # whether a zip had to be unpacked
    note: str = ''


def _is_wanted(member: str) -> bool:
    rel = member.split(f'{NESTED_DIR}/', 1)[-1]
    return any(rel == p or rel.startswith(p) for p in WANTED_PREFIXES)


def _safe_target(base: str, member: str) -> str | None:
    """Resolve a zip member to a path inside base, or None if it escapes.

    A zip can contain `../` entries or absolute paths; extracting those writes
    outside the destination. The archive here is one the user downloaded from
    Apple, but a tool other people run should not assume that.
    """
    rel = member.split(f'{NESTED_DIR}/', 1)[-1]
    if not rel or rel.endswith('/'):
        return None
    target = os.path.normpath(os.path.join(base, rel))
    if os.path.commonpath([os.path.abspath(base), os.path.abspath(target)]) != os.path.abspath(base):
        return None
    return target


def extract_export(zip_path: str, cache_dir: str, force: bool = False) -> str:
    """Unpack the parts of an export zip that the converter actually reads."""
    os.makedirs(cache_dir, exist_ok=True)
    marker = os.path.join(cache_dir, EXPORT_NAME)

    # Re-extracting a gigabyte on every run would be a poor trade for a file
    # that only changes when the user exports again.
    if not force and os.path.exists(marker):
        if os.path.getmtime(marker) >= os.path.getmtime(zip_path):
            return cache_dir
        shutil.rmtree(cache_dir, ignore_errors=True)
        os.makedirs(cache_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.namelist() if _is_wanted(m) and not m.endswith('/')]
        if not members:
            raise ValueError(
                f'{os.path.basename(zip_path)} contains no export.xml. '
                'Is it really an Apple Health export?')
        for member in members:
            target = _safe_target(cache_dir, member)
            if target is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)

    if not os.path.exists(marker):
        raise ValueError(f'{os.path.basename(zip_path)} did not yield an export.xml.')
    return cache_dir


def newest_zip(directory: str) -> str | None:
    zips = glob.glob(os.path.join(directory, '*.zip'))
    if not zips:
        return None
    return max(zips, key=os.path.getmtime)


def resolve(path: str, cache_root: str, force: bool = False) -> Resolved:
    """Turn whatever the user pointed at into a directory holding export.xml."""
    path = os.path.abspath(os.path.expanduser(path))
    cache_dir = os.path.join(cache_root, CACHE_DIR)

    if os.path.isfile(path):
        if not path.lower().endswith('.zip'):
            raise ValueError(f'{path} is a file, not a folder or a .zip export.')
        return Resolved(extract_export(path, cache_dir, force), path, True,
                        f'extracted from {os.path.basename(path)}')

    if not os.path.isdir(path):
        raise ValueError(f'{path} does not exist.')

    if os.path.exists(os.path.join(path, EXPORT_NAME)):
        return Resolved(path, path, False)

    nested = os.path.join(path, NESTED_DIR)
    if os.path.exists(os.path.join(nested, EXPORT_NAME)):
        return Resolved(nested, path, False, f'found {NESTED_DIR}/ inside')

    found = newest_zip(path)
    if found:
        return Resolved(extract_export(found, cache_dir, force), path, True,
                        f'newest export in that folder: {os.path.basename(found)}')

    raise ValueError(
        f'No Apple Health export in {path}.\n'
        '  Expected export.xml, an apple_health_export/ folder, or a .zip.\n'
        '  Export from the iPhone Health app: profile icon -> Export All Health Data.')
