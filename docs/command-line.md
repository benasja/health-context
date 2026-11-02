# Using it from the command line

Everything the Claude Desktop extension does, done by hand: convert an export into
CSV files and a written report, then read them yourself, paste them into any
assistant, or serve them over MCP ([mcp.md](mcp.md)). Python 3.10+, no dependencies.

## Try it on demo data first

A generator makes a synthetic two-and-a-half-year export, with seasons, a
training block, two illnesses, and a holiday where the watch stayed at home:

```bash
python3 fixtures/make_demo_export.py --out-dir demo
python3 convert_health_data.py --data-dir demo --out-dir demo
```

Then open `demo/insights_report.md` or `demo/llm_context.md`. The demo data is
fabricated, shaped to be plausible rather than to be anyone's real physiology.

## Convert your own export

1. Health app → profile picture → **Export All Health Data**. AirDrop or copy
   the zip to your computer.
2. Point the tool at it. **No unzipping** — hand it the zip exactly as it arrived:

```bash
python3 convert_health_data.py --data-dir ~/Downloads/export.zip --out-dir output
```

`--data-dir` accepts the `.zip`, an unzipped `apple_health_export/` folder, or
any folder containing either — point it at `~/Downloads` and it picks the newest
export it finds. Only the files actually parsed are extracted (~1 GB of the
~1.4 GB export is route GPX and a clinical-document mirror that is never read),
and the extraction is cached until you export again.

A full run is around two minutes, with a progress bar during the parse.

## Installing

Nothing to install — clone and run, on macOS, Linux or Windows. Python 3.10+,
zero dependencies.

If you would rather have the commands on your `PATH`:

```bash
pip install git+https://github.com/benasja/health-context
health-convert --data-dir ~/Downloads/export.zip --out-dir ~/health
```

That gives you `health-convert` and `health-mcp`. Still no runtime dependencies —
packaging only adds the entry points.

## Options

| Flag | Purpose |
|------|---------|
| `--data-dir DIR` | Where `export.xml` lives (default: next to the script) |
| `--out-dir DIR` | Where to write outputs (default: same as `--data-dir`) |
| `--max-hr BPM` | Max heart rate for zone maths. Default: highest *corroborated* value in the export |
| `--age N` | Sanity-checks max HR against `208 - 0.7 x age` |
| `--skip-legacy` | Skip the large flat `full_health_data.csv` (much faster) |
| `--include-cda` | Also parse `export_cda.xml` — **see the warning below** |

## Outputs

| File | What it is |
|------|-----------|
| `insights_report.md` | **Start here.** Recovery, strain flags, sleep regularity, training load, trends, associations |
| `llm_context.md` | Compact pre-analysed brief to paste into an LLM |
| `daily_metrics.csv` | One row per day, 60+ columns of raw daily aggregates |
| `daily_insights.csv` | Per-day baselines, deviations, z-scores, scores and flags |
| `weekly_summary.csv` | One row per ISO week |
| `workout_summary.csv` | One row per workout: HR and minutes in each zone, cycling power and cadence, which device recorded them, and a flag on records that look wrong |
| `workout_series.csv` | Minute by minute HR, power and cadence for each workout |
| `hr_timeline.csv` | Every heart-rate reading in 5-minute buckets (avg, min, max) with the steps taken in each, labelled with the sleep stage or workout it fell in, or else walking, light activity or stationary from its steps. Steps come from a receiver, when Wariden sends them per 5 minutes; otherwise they are empty and so is the label |
| `strength_sets.csv` | From a receiver, when Wariden sends its log: one row per set — exercise, set type, weight, reps, RPE, volume, estimated 1RM, notes |
| `strength_sessions.csv` | One row per Wariden strength workout: program, sets, reps, volume, each exercise as weight x reps, and the matching Apple Health workout's energy and heart rate |
| `data_status.json` | When the data was built, its newest record and, from a receiver, when the phone last synced |
| `metric_coverage.csv` | Per metric: first date, **reliable start**, coverage % |
| `data_quality_report.txt` | Wear stats, coverage, multi-device warnings, duplicate bursts |
| `available_types.csv` | Every Health type in the export with counts |
| `full_health_data.csv` | Legacy flat export (all records, one row each) |

Units are normalised to **kg, km, kcal, bpm, minutes, %**. Empty cells mean
*not measured* — never zero.

## Sample data

```bash
mkdir -p /tmp/fx && cp fixtures/sample_export.xml /tmp/fx/export.xml
python3 convert_health_data.py --data-dir /tmp/fx
```

## Rebuilding on a schedule

Everything *after* the export can be automated, and the manual part reduces to:
export, AirDrop to your Downloads folder, done.

Point the scheduler at the folder your exports land in:

```bash
python3 convert_health_data.py --data-dir ~/Downloads --out-dir ~/health --print-schedule
```

That prints a ready-to-install monthly job for your OS — a launchd plist on
macOS, a systemd service and timer on Linux, a `schtasks` command on Windows —
with real paths already filled in. Save or run it as the comment says.

From then on it rebuilds monthly from the newest export in that folder. The MCP
server notices rebuilt files on its next call, so it needs no restart, and if you
forget to export it tells the model the data is stale rather than quietly
answering from old numbers.
