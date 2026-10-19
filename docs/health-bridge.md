# Automatic sync with Health Bridge

The one manual step in this project has always been the export: Apple offers
no way to trigger "Export All Health Data" from a script. [Health Bridge for
AI](https://github.com/roian6/apple-health-ai-bridge) removes it. Its iPhone
app syncs Apple Health in the background to a receiver on a Mac or Linux
machine you own, which keeps the records in a SQLite database.

health-context can read that database instead of an export. Put the two
together on a home server and the context pack stays current on its own:

```
iPhone ── Health Bridge app ──▶ receiver ──▶ health.sqlite
                                                   │  read-only, every 15 min
                                                   ▼
                                health_sync.py ──▶ current/  ◀── health_mcp.py ◀── your assistant
```

The two do different jobs. Health Bridge moves the data and gives an assistant
the raw records. health-context adds what raw records cannot tell a model:
your own baselines, how this month compares with what you have sustained
before, which days the watch was not worn, and the scores and flags built on
those.

## What you need first

A working Health Bridge receiver, set up with [its own
guide](https://github.com/roian6/apple-health-ai-bridge/blob/main/docs/setup.md),
with the phone already syncing. That part is not repeated here: pairing, the
private route the phone uses to reach the receiver, and running the receiver
as a service are all covered there.

Then, on the receiver's machine:

```bash
ls -l ~/.local/share/health-bridge/health.sqlite   # the database; note its owner
id -u; id -g                                        # that user's ids
```

The database is readable by its owner only (mode `0600`), and that is how it
should stay. Whatever reads it has to run as the same user.

## With Docker

```bash
git clone https://github.com/benasja/health-context.git
cd health-context/deploy
cp .env.example .env
```

Edit `.env`: the user and group ids from above, the full path of the
receiver's folder, and a folder for the output. Create the output folder as
that user, then start it:

```bash
mkdir -p ~/health-context-data
docker compose up -d --build
docker compose logs -f
```

Within a few seconds:

```
watching /bridge/health.sqlite every 900s
now serving 20261016T081500Z (6.1s). Read 36,512 samples (5,668 of them daily totals from the phone), 4,760 sleep stages, 340 workouts
```

From then on a new line appears whenever the phone has synced something.

The container cannot alter your data even by mistake: the receiver's folder is
mounted read-only, the database is opened read-only on top of that, and only
the tables holding health records are read; the pairing and credential tables
are never touched, nor copied anywhere. It has no network access at all, because it needs
none.

## Without Docker

The same thing as a systemd user service. Save as
`~/.config/systemd/user/health-sync.service`, with your own paths:

```ini
[Unit]
Description=Keep the health-context pack up to date from Health Bridge

[Service]
ExecStart=/usr/bin/python3 /home/you/health-context/health_sync.py \
    --bridge-db /home/you/.local/share/health-bridge/health.sqlite \
    --out-root /home/you/health-context-data
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now health-sync
journalctl --user -u health-sync -f
```

## Connecting an assistant

Point the MCP server at `current` inside the output folder. On the server
itself, for Claude Code:

```bash
claude mcp add apple-health -- docker exec -i health-context \
    python3 /app/health_mcp.py --data-dir /output/current
```

From a laptop, over SSH. Nothing to install on the laptop, and the data never
leaves the server except as the answers the assistant asks for:

```bash
claude mcp add apple-health -- ssh homeserver docker exec -i health-context \
    python3 /app/health_mcp.py --data-dir /output/current
```

Without Docker, the same with `python3 ~/health-context/health_mcp.py
--data-dir ~/health-context-data/current` in place of the `docker exec` part.

A session that is already open picks up new data on its next question; nothing
needs restarting.

## How a rebuild works, and why

Each rebuild writes a complete new folder under `snapshots/`, and only then
switches `current` to it in one atomic rename. A question that arrives in the
middle of a rebuild is answered entirely from the previous one, never from a
mix of the two. A rebuild that fails switches nothing: `current` keeps serving
the last good one, and the reason is in the log. The last three snapshots are
kept.

The database is never held open while converting. The health tables are first
copied out in one read transaction, which takes about a second and gives one
consistent moment across all of them, and the conversion runs on the copy. A long read on the
live database would make the receiver wait to commit while the phone retried.

Checking for new data costs one `stat()` call, so the interval only decides how
stale the pack can get. Without changes, nothing is rebuilt.

A `profile.md` in the output folder is included in every rebuild, the same as
next to an export.

## What is different from an export

The importer translates everything that differs in shape: UTC timestamps to
your local day (using the timezone the phone reports, not the server's, which
in a container is usually UTC), percentages sent as 0-100 to the fractions the
export uses, the phone's de-duplicated daily totals in place of raw samples on
days it sent one.

That last one matters most. For summed metrics -- steps, energy, distance,
exercise minutes -- the phone sends Apple's own daily total, already merged
across iPhone and Watch, and may send the raw samples as well. Counting both
would add the same walk twice. So for each metric and local day the phone's
total is used if it sent one, and its raw samples for that day are skipped;
days without a total fall back to the raw samples, de-duplicated across
devices the same way as an export. The phone sends one total per metric per
day and replaces it when it changes, and the receiver deletes records removed
on the phone, so neither a corrected total nor a deleted workout is counted.
This is the same rule the receiver's own daily summary uses.

With that done, the same synthetic two-and-a-half-year export
gives identical results by either route: every day, every score, every flag,
every week, every workout. CI checks this on each change
(`fixtures/check_bridge_equivalence.py`).

What the app does not sync, and so cannot appear here:

- **Stand hours** (the hourly ring). The app syncs stand *time* in minutes,
  which comes through as `stand_minutes`, but not the stand-hour category
  `stand_hours_met` is built from, so that column stays blank.
- **Cycling power.** The same; the report's cycling power section is left out.
- **Flights climbed on a day with none.** The app sends nothing rather than a
  zero, so the day reads as not measured.

Sleep and workouts are labelled with the companion app as their source rather
than the Watch. Only the label changes.

## Which receiver versions work

Tested against receiver **v1.1.2**, whose storage schema is unchanged on its
current main. That is the tested baseline, not a promise: Health Bridge has no
compatibility contract for programs that read its database directly, so a
future release could change a table this reads. If it does, the importer
refuses to run and names the table or column it could not find, rather than
producing numbers from a layout it does not understand. Open an issue with
that message and the receiver version.

## When it goes wrong

**`could not read /bridge/health.sqlite: ... unable to open database file`**
— almost always the user. The container must run as the database's owner; check
`HEALTH_UID` and `HEALTH_GID` against `ls -ln` on the database.

**`... it is in WAL mode`** — the receiver uses SQLite's default journal, which
reads fine from a read-only folder. If its database has been switched to WAL
mode, SQLite has to write a small `-shm` file beside it even to read, and the
error says so. Change `:ro` to `:rw` on the `/bridge` line in
`docker-compose.yml`. The database itself is still opened read-only.

**`does not exist yet; has the receiver been set up?`** — the path in `.env`
does not point at the receiver's folder, or setup has not created the database
yet.

**`rebuild failed`** — followed by what the converter said. `current` is still
the last good snapshot, so the assistant keeps working; it is just not getting
newer data until this is fixed.
