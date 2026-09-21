"""Read-only job metadata diagnostics: python -m asr_gateway.jobs [JOB_ID]."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_id", nargs="?")
    parser.add_argument("--recent", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.recent <= 100:
        parser.error("--recent must be between 1 and 100")
    path = Path(os.environ.get("ASR_DATABASE", "/data/jobs.sqlite3")).resolve()
    fields = "id,state,attempts,created,updated,error,backend_status,body IS NOT NULL AS has_audio"
    try:
        db = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            if args.job_id:
                rows = db.execute("SELECT " + fields + " FROM jobs WHERE id=?", (args.job_id,)).fetchall()
            else:
                rows = db.execute("SELECT " + fields + " FROM jobs ORDER BY created DESC LIMIT ?", (args.recent,)).fetchall()
            print(json.dumps([dict(row) for row in rows], indent=2))
        finally:
            db.close()
    except sqlite3.Error:
        parser.exit(1, "Cannot read job metadata; check the configured database and its permissions.\n")


if __name__ == "__main__":
    main()
