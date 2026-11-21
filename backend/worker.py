"""
Stateless worker entry point.

    python -m backend.worker               # drain the queue and exit
    python -m backend.worker --watch       # keep polling
    python -m backend.worker --once        # process a single job

Each worker is disposable: it claims a job with a conditional update, does the
work, and writes the outcome. Nothing is held in process memory, so any number
can run behind the same Postgres.
"""
import argparse
import time

from . import jobs
from .database import SessionLocal, init_db
from .pipeline import jobs_handlers  # noqa: F401  (registers the handlers)


def main() -> int:
    parser = argparse.ArgumentParser(description="3-way match job worker")
    parser.add_argument("--once", action="store_true", help="process a single job then exit")
    parser.add_argument("--watch", action="store_true", help="keep polling for new jobs")
    parser.add_argument("--kinds", nargs="*", default=None, help="only run these job kinds")
    parser.add_argument("--max-jobs", type=int, default=None, help="stop after this many jobs")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    processed = 0
    try:
        while True:
            if args.once:
                summary = jobs.run_once(db, args.kinds)
            else:
                summary = jobs.run_worker(db, max_jobs=1, kinds=args.kinds)
                summary = summary[0] if summary else None

            if summary:
                processed += 1
                print(f"🧱 [Worker] {summary}")
                if args.max_jobs and processed >= args.max_jobs:
                    break
                continue

            if not args.watch:
                break
            time.sleep(args.interval)
    finally:
        db.close()
    print(f"🧱 [Worker] finished ({processed} job(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
