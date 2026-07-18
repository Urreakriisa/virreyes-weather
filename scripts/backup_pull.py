#!/usr/bin/env python3
"""Pull a timestamped backup of the Virreyes training data (event log +
labeled outcomes) off the Railway volume. Stdlib only; safe to run any time.

Writes backups/YYYY-MM-DD_storm_events.jsonl and _outcomes.jsonl next to the
repo (the backups/ dir is gitignored; on this machine it also lives inside
OneDrive, so copies are off-Railway AND cloud-synced). Keeps every daily file;
at current volume that is ~1 MB/day of storm-season data — no rotation needed
for years."""
import datetime
import os
import sys
import urllib.request

BASE = "https://web-production-9aab2.up.railway.app"
HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(os.path.dirname(HERE), "backups")


def pull(path, name):
    url = f"{BASE}{path}?nc=1"
    data = urllib.request.urlopen(url, timeout=180).read()
    day = datetime.date.today().isoformat()
    dest = os.path.join(OUTDIR, f"{day}_{name}")
    tmp = dest + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
    rows = data.count(b"\n")
    print(f"{dest}: {rows} rows, {len(data)/1e6:.2f} MB")
    if rows < 10:
        raise SystemExit(f"suspiciously small export ({rows} rows) — not overwriting trust in it")
    return rows


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    pull("/api/log/export", "storm_events.jsonl")
    pull("/api/outcomes/export", "outcomes.jsonl")
    print("backup ok", datetime.datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    sys.exit(main())
