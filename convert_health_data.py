#!/usr/bin/env python3

"""Convert an Apple Health export into flat CSV, coaching aggregates and insights."""

import argparse
import csv
import glob
import os
import xml.etree.ElementTree as ET
from datetime import datetime

from health_coaching import (
    COACHING_RECORD_TYPES,
    ExportData,
    HealthRecord,
    ingest_record,
    iter_export_xml,
    parse_float,
    parse_health_datetime,
    records_to_legacy_rows,
    write_coaching_outputs,
)
from health_insights import write_insight_outputs


def format_cda_date(date_str: str) -> str:
    if not date_str:
        return ''
    try:
        if len(date_str) >= 14:
            base = date_str[:14]
            tz = date_str[14:] if len(date_str) > 14 else ''
            dt = datetime.strptime(base, '%Y%m%d%H%M%S')
            formatted = dt.strftime('%Y-%m-%d %H:%M:%S')
            if tz:
                formatted += f' {tz}'
            return formatted
    except ValueError:
        pass
    return date_str


def parse_export_cda_into_data(filepath: str, data: ExportData) -> int:
    """Parse export_cda.xml (HL7 CDA) and merge matching records into data."""
    print(f"Processing: {filepath}")
    count = 0
    ns = {'cda': 'urn:hl7-org:v3'}

    try:
        context = ET.iterparse(filepath, events=('end',))
        for _event, elem in context:
            if elem.tag != '{urn:hl7-org:v3}observation':
                continue
            text_elem = elem.find('cda:text', ns)
            if text_elem is None:
                elem.clear()
                continue
            type_elem = text_elem.find('cda:type', ns)
            if type_elem is None or type_elem.text not in COACHING_RECORD_TYPES:
                elem.clear()
                continue

            record_type = type_elem.text
            data.type_counts[record_type] += 1

            value_elem = text_elem.find('cda:value', ns)
            value_raw = value_elem.text if value_elem is not None else ''

            effective_time = elem.find('cda:effectiveTime', ns)
            start_date = ''
            end_date = ''
            if effective_time is not None:
                low = effective_time.find('cda:low', ns)
                high = effective_time.find('cda:high', ns)
                if low is not None:
                    start_date = format_cda_date(low.get('value', ''))
                if high is not None:
                    end_date = format_cda_date(high.get('value', ''))

            if data.mark_seen(start_date, start_date, end_date, record_type, value_raw or '', 'cda'):
                elem.clear()
                continue

            start_dt = parse_health_datetime(start_date)
            end_dt = parse_health_datetime(end_date)
            if start_dt and end_dt:
                numeric = parse_float(value_raw)
                ingest_record(data, record_type, value_raw or '', '', 'cda', start_dt, end_dt)
                data.records.append(
                    HealthRecord(
                        type=record_type,
                        value=numeric,
                        category_value=value_raw if numeric is None else '',
                        unit='',
                        source_name='cda',
                        start=start_dt,
                        end=end_dt,
                        creation=start_date,
                        start_raw=start_date,
                        end_raw=end_date,
                    )
                )
                count += 1
            elem.clear()
    except ET.ParseError as e:
        print(f"  Warning: CDA file has malformed XML, skipping. Error: {e}")

    print(f"  Extracted {count:,} new records from {os.path.basename(filepath)}")
    return count


def parse_ecg_legacy(ecg_dir: str, legacy_rows: list, seen_keys: set) -> int:
    print(f"Processing ECG files from: {ecg_dir}")
    count = 0
    ecg_files = glob.glob(os.path.join(ecg_dir, 'ecg_*.csv'))

    for filepath in ecg_files:
        try:
            with open(filepath, encoding='utf-8') as f:
                lines = [line.strip() for i, line in enumerate(f) if i <= 10]
            metadata = {}
            for line in lines:
                if ',' in line:
                    parts = line.split(',', 1)
                    if len(parts) == 2:
                        key, value = parts[0].strip(), parts[1].strip().strip('"')
                        metadata[key] = value

            recorded_date = metadata.get('Recorded Date', '')
            classification = metadata.get('Classification', '')
            if not recorded_date:
                continue
            record_key = (recorded_date, recorded_date, recorded_date, 'ECG', classification)
            if record_key in seen_keys:
                continue
            seen_keys.add(record_key)
            legacy_rows.append({
                'creationDate': recorded_date,
                'startDate': recorded_date,
                'endDate': recorded_date,
                'type': 'ECG',
                'value': classification,
            })
            count += 1
        except OSError as e:
            print(f"  Warning: Could not parse {filepath}: {e}")

    print(f"  Extracted {count} ECG records")
    return count


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Convert an Apple Health export into CSVs and an insights report.',
    )
    p.add_argument('--data-dir', default=None,
                   help='Directory holding export.xml (default: alongside this script)')
    p.add_argument('--out-dir', default=None,
                   help='Where to write outputs (default: same as --data-dir)')
    p.add_argument('--max-hr', type=float, default=None,
                   help='Max heart rate for zone maths. Default: highest value seen in the export.')
    p.add_argument('--age', type=int, default=None,
                   help='Age, used to sanity-check max HR when no higher value was recorded.')
    p.add_argument('--skip-legacy', action='store_true',
                   help='Skip the large flat full_health_data.csv (much faster).')
    p.add_argument('--include-cda', action='store_true',
                   help='Also parse export_cda.xml. Off by default: it is a clinical-document '
                        'mirror of export.xml whose records cannot be matched against the main '
                        'export for de-duplication, so including it double-counts summed metrics '
                        'like steps and calories.')
    return p


def main() -> None:
    args = build_parser().parse_args()

    base_dir = os.path.abspath(args.data_dir or os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.abspath(args.out_dir or base_dir)
    os.makedirs(out_dir, exist_ok=True)

    export_xml = os.path.join(base_dir, 'export.xml')
    export_cda_xml = os.path.join(base_dir, 'export_cda.xml')
    ecg_dir = os.path.join(base_dir, 'electrocardiograms')

    data = ExportData()

    print('=' * 60)
    print('Apple Health Data Converter')
    print('=' * 60)

    if os.path.exists(export_xml):
        iter_export_xml(export_xml, data)
    else:
        print(f"Error: {export_xml} not found. Point --data-dir at your unzipped export.")
        return

    if not args.include_cda:
        print('Skipping export_cda.xml (pass --include-cda to parse it as well)')
    elif os.path.exists(export_cda_xml):
        parse_export_cda_into_data(export_cda_xml, data)
    else:
        print(f"Note: {export_cda_xml} not found (optional)")

    output_csv = os.path.join(out_dir, 'full_health_data.csv')
    legacy_rows = []
    if args.skip_legacy:
        print('Skipping full_health_data.csv (--skip-legacy)')
    else:
        legacy_rows = records_to_legacy_rows(data)
        ecg_seen = {(r['creationDate'], r['startDate'], r['endDate'], r['type'], r['value'])
                    for r in legacy_rows}
        if os.path.exists(ecg_dir):
            parse_ecg_legacy(ecg_dir, legacy_rows, ecg_seen)
        legacy_rows.sort(key=lambda x: x['startDate'])
        print(f"\nWriting legacy export: {output_csv}")
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(
                f, fieldnames=['creationDate', 'startDate', 'endDate', 'type', 'value'])
            writer.writeheader()
            writer.writerows(legacy_rows)

    print('\nBuilding coaching-ready outputs...')
    result = write_coaching_outputs(out_dir, data, max_hr_override=args.max_hr, age=args.age)

    print('Building insights...')
    insight_paths = write_insight_outputs(
        out_dir, result.daily_rows, result.coverage, result.analysis_start,
        result.workout_rows, result.max_hr)

    print()
    print('=' * 60)
    print('COMPLETE')
    print('=' * 60)
    if legacy_rows:
        print(f"Legacy rows: {len(legacy_rows):,} -> {output_csv}")
    for name, path in {**result.paths, **insight_paths}.items():
        print(f"  {name}: {path}")
    print(f"\nStart with {insight_paths['insights_report']}")
    print(f"Paste {insight_paths['llm_context']} into an LLM for coaching.")


if __name__ == '__main__':
    main()
