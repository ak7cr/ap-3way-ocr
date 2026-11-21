#!/usr/bin/env python3
"""
Dev launcher for the API.

    python main.py                 # http://127.0.0.1:8000
    python main.py --reload        # auto-reload on code changes
    python main.py --host 0.0.0.0 --port 8080

`backend/main.py` is a package module (it uses relative imports and has to be
imported as `backend.main`), so `python backend/main.py` cannot work. This thin
launcher is the supported "python main.py" entry point; `uvicorn backend.main:app`
and `python -m backend.main` also work.
"""
import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the invoice/3-way-match API")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="reload on code changes (dev only)")
    args = parser.parse_args()

    uvicorn.run("backend.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
