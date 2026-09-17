#!/usr/bin/env python3
"""Assert that a demo run actually computed context, not just output files.

The existing end-to-end check proves the pipeline runs to completion. It cannot
prove the pipeline computed anything: percentiles, 60-day baselines and 28-day
best windows all need history, and against the two-day fixture every one of them
reads `n/a`. A run like that produces ten non-empty files and a report full of
blanks, and passes every check that only asks whether files exist.

This asks the harder question. Run it after generating the demo export and
converting it:

    python3 fixtures/make_demo_export.py --out-dir demo-run
    python3 convert_health_data.py --data-dir demo-run --out-dir demo-run
    python3 fixtures/check_demo_output.py demo-run

Exits non-zero with a specific reason on the first failed assertion.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

MIN_DAYS = 900
MIN_FULL_WEAR_DAYS = 800
MIN_CAPACITY_ROWS = 8
# The generator plants an illness here that moves five signals at once.
PLANTED_STRAIN_MONTH = '2024-11'


def fail(message: str) -> None:
    print(f'FAIL: {message}', file=sys.stderr)
    raise SystemExit(1)


def check_daily_rows(out_dir: str) -> list[dict[str, str]]:
    path = os.path.join(out_dir, 'daily_metrics.csv')
    if not os.path.exists(path):
        fail(f'{path} does not exist')
    with open(path, encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < MIN_DAYS:
        fail(f'daily_metrics.csv has {len(rows)} days, expected at least {MIN_DAYS}')
    print(f'ok: {len(rows)} days parsed')
    return rows


def check_wear(rows: list[dict[str, str]]) -> None:
    """A fixture whose every day reads `none` means the wear signal is wrong.

    Wear gates a lot of downstream analysis, so getting this wrong does not
    raise an error anywhere -- it quietly measures nothing.
    """
    full = sum(1 for r in rows if r.get('wear_class') == 'full')
    if full < MIN_FULL_WEAR_DAYS:
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.get('wear_class', '?')] = counts.get(r.get('wear_class', '?'), 0) + 1
        fail(f'only {full} full-wear days (classes: {counts}); wear detection is not seeing the watch')
    print(f'ok: {full}/{len(rows)} days classified as full wear')


def capacity_rows(text: str) -> list[str]:
    section = re.search(r'## Capacity gap.*?(?=\n## |\Z)', text, re.S)
    if not section:
        fail('llm_context.md has no "Capacity gap" section')
    body = section.group(0)  # type: ignore[union-attr]
    table = [line for line in body.splitlines() if line.startswith('| ') and '---' not in line]
    if len(table) < 2:
        fail('capacity-gap section contains no table')
    return table[1:]


def check_capacity(out_dir: str) -> str:
    """The capacity table is the differentiator; an all-n/a one is decoration."""
    path = os.path.join(out_dir, 'llm_context.md')
    if not os.path.exists(path):
        fail(f'{path} does not exist')
    with open(path, encoding='utf-8') as handle:
        text = handle.read()

    rows = capacity_rows(text)
    if len(rows) < MIN_CAPACITY_ROWS:
        fail(f'capacity table has {len(rows)} metric rows, expected at least {MIN_CAPACITY_ROWS}')

    # A row that could not be computed carries n/a in both the current and the
    # percentile column. One such row is tolerable (a metric may genuinely be
    # unmeasured in the closing window); a table full of them is the bug.
    uncomputed = [r for r in rows if re.search(r'\|\s*n/a\s*\|[^|]*\|\s*n/a\s*\|', r)]
    if len(uncomputed) > 1:
        print('\n'.join(rows), file=sys.stderr)
        fail(f'{len(uncomputed)}/{len(rows)} capacity rows have no percentile')
    print(f'ok: {len(rows) - len(uncomputed)}/{len(rows)} capacity rows carry a percentile')
    return text


def check_strain(text: str) -> None:
    if PLANTED_STRAIN_MONTH not in text:
        fail(f'the strain episode planted at {PLANTED_STRAIN_MONTH} was not surfaced in llm_context.md')
    print(f'ok: strain episode at {PLANTED_STRAIN_MONTH} surfaced')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('out_dir', help='Directory holding a completed demo run')
    args = parser.parse_args()

    rows = check_daily_rows(args.out_dir)
    check_wear(rows)
    text = check_capacity(args.out_dir)
    check_strain(text)
    print('\nDemo output computes context.')


if __name__ == '__main__':
    main()
