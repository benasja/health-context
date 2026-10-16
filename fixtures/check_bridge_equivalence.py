#!/usr/bin/env python3
"""Check that a Health Bridge database gives the same answers as the export.

Takes a demo folder that has already been converted from export.xml, turns
that same export into a Health Bridge receiver database (fixtures/
make_bridge_db.py), converts the database, and compares the two sets of
outputs: every daily metric, every daily insight, every week, every workout,
and both reports.

The point is not that the two inputs look alike -- they do not: one is local
time with offsets, fractions and per-sample steps, the other UTC, 0-100
percentages and the phone's daily totals. The point is that after translation
nothing downstream can tell which one it was given.

The only differences allowed are listed below, each with its reason. Anything
else fails the run.

    python3 fixtures/check_bridge_equivalence.py demo-run
"""

from __future__ import annotations

import argparse
import csv
import difflib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Columns that may differ, and why.
ALLOWED_DAILY = {
    # The companion app attributes sleep sessions to itself rather than to the
    # Watch; the sleep numbers are unaffected, only this label changes.
    'sleep_source',
    # Stand hours come from a HealthKit category type the app does not sync.
    'stand_hours_met',
}
ALLOWED_WORKOUT = {
    'workout_id',      # derived partly from the source name
    'source_name',     # same reason as sleep_source
    'cycling_power_avg_w', 'cycling_power_max_w',   # power is not synced
}
# A day whose summed total is zero is not sent by the app at all, so the
# export's explicit 0 reads as "not measured". (Real Apple exports also write
# nothing on a day with no flights; the synthetic demo writes zeros.)
ZERO_DROPPED = {'flights_climbed'}
# Report sections that exist only because the export carries data the bridge
# does not.
ALLOWED_REPORT_SECTIONS = ('### Cycling power',)


def rows(path: str) -> list[dict[str, str]]:
    with open(path, newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def same(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
    except ValueError:
        return False


def compare_table(name: str, export_rows, bridge_rows, key: str, allowed: set[str],
                  zero_dropped: set[str] = frozenset()) -> list[str]:
    problems: list[str] = []
    a = {r[key]: r for r in export_rows}
    b = {r[key]: r for r in bridge_rows}
    if set(a) != set(b):
        problems.append(f'{name}: {len(set(a) ^ set(b))} rows present on one side only')
    for k in sorted(set(a) & set(b)):
        for column, value in a[k].items():
            other = b[k].get(column, '')
            if column in allowed or same(value, other):
                continue
            if column in zero_dropped and other == '' and value != '' and float(value) == 0:
                continue
            problems.append(f'{name} {k} {column}: export={value!r} bridge={other!r}')
    return problems


def compare_report(name: str, export_text: str, bridge_text: str) -> list[str]:
    problems: list[str] = []
    export_lines, bridge_lines = export_text.splitlines(), bridge_text.splitlines()
    matcher = difflib.SequenceMatcher(a=export_lines, b=bridge_lines, autojunk=False)
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == 'equal':
            continue
        removed = export_lines[a0:a1]
        if op == 'delete' and removed and any(
                line.strip() in ALLOWED_REPORT_SECTIONS for line in removed):
            continue
        problems.append(f'{name}: {op} at line {a0 + 1}: '
                        f'{" | ".join(removed)[:200]!r} -> {" | ".join(bridge_lines[b0:b1])[:200]!r}')
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('demo_dir', help='folder holding export.xml and its converted outputs')
    args = parser.parse_args()
    demo = os.path.abspath(args.demo_dir)

    with tempfile.TemporaryDirectory() as work:
        db = os.path.join(work, 'health.sqlite')
        out = os.path.join(work, 'out')
        subprocess.run([sys.executable, os.path.join(HERE, 'make_bridge_db.py'),
                        '--export', os.path.join(demo, 'export.xml'), '--out', db], check=True)
        subprocess.run([sys.executable, os.path.join(ROOT, 'convert_health_data.py'),
                        '--bridge-db', db, '--out-dir', out], check=True,
                       stdout=subprocess.DEVNULL)

        problems: list[str] = []
        problems += compare_table('daily_metrics', rows(f'{demo}/daily_metrics.csv'),
                                  rows(f'{out}/daily_metrics.csv'), 'date', ALLOWED_DAILY, ZERO_DROPPED)
        problems += compare_table('daily_insights', rows(f'{demo}/daily_insights.csv'),
                                  rows(f'{out}/daily_insights.csv'), 'date', set())
        problems += compare_table('weekly_summary', rows(f'{demo}/weekly_summary.csv'),
                                  rows(f'{out}/weekly_summary.csv'), 'iso_week', set())
        problems += compare_table('workout_summary', rows(f'{demo}/workout_summary.csv'),
                                  rows(f'{out}/workout_summary.csv'), 'start_datetime', ALLOWED_WORKOUT)
        for report in ('insights_report.md', 'llm_context.md'):
            with open(f'{demo}/{report}', encoding='utf-8') as a, open(f'{out}/{report}', encoding='utf-8') as b:
                problems += compare_report(report, a.read(), b.read())

    if problems:
        print(f'FAIL: the bridge path disagrees with the export in {len(problems)} place(s):')
        for line in problems[:40]:
            print('  ' + line)
        raise SystemExit(1)
    print('ok: the Health Bridge path matches the export on every compared value')


if __name__ == '__main__':
    main()
