#!/usr/bin/env python3
"""
Run the AP sample-set evaluation.

    python tests/ap_samples/run_eval.py --mode truth     # matching engine on ground truth
    python tests/ap_samples/run_eval.py --mode pdf       # digital-PDF extraction accuracy
    python tests/ap_samples/run_eval.py --mode scanned   # OCR accuracy (needs EasyOCR)
    python tests/ap_samples/run_eval.py --mode all
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_harness as harness  # noqa: E402


def _session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from backend.database import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def run_truth(verbose: bool = False) -> int:
    print("=== 3-way match · ground truth (no OCR) ===")
    db = _session()
    failures = 0
    try:
        results = harness.run_all_truth_scenarios(db)
    finally:
        db.close()

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"  {result.scenario:<3} {status}  {result.status:<9} "
            f"codes={', '.join(result.reason_codes) or '-'}"
        )
        if result.duplicate_status:
            print(f"        duplicate -> {result.duplicate_status} ({', '.join(result.duplicate_reason_codes) or '-'})")
        if verbose:
            for line_no, codes in sorted(result.line_codes.items()):
                print(f"        line {line_no}: {', '.join(codes) or 'ok'}")
        for failure in result.failures:
            print(f"        ✗ {failure}")
        failures += len(result.failures)

    print(f"\n  {len(results) - sum(1 for r in results if not r.passed)}/{len(results)} scenarios passed · {failures} assertion failure(s)")
    return 1 if failures else 0


def run_fields(mode: str, verbose: bool = False) -> int:
    print(f"=== Field accuracy · {mode} ===")
    try:
        report = harness.evaluate_pdf_invoices() if mode == "pdf" else harness.evaluate_scanned_invoices()
    except ImportError as exc:
        print(f"  SKIPPED: {exc}")
        return 0
    except Exception as exc:  # extraction errors shouldn't hide the score line
        print(f"  ERROR: {exc}")
        return 1

    for row in report["rows"]:
        passed = sum(1 for ok in row["checks"].values() if ok)
        total = len(row["checks"])
        engine = f"  [{row['engine']}]" if row.get("engine") else ""
        print(f"  {row['file']:<28} {passed}/{total} fields{engine}")
        if verbose:
            for note in row["notes"]:
                print(f"        {note}")

    print(f"\n  accuracy: {report['fields_passed']}/{report['fields_total']} = {report['accuracy'] * 100:.1f}%")
    if report.get("engines"):
        print(f"  engines used: {report['engines']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the AP sample set")
    parser.add_argument("--mode", choices=["truth", "pdf", "scanned", "all"], default="truth")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    exit_code = 0
    if args.mode in ("truth", "all"):
        exit_code |= run_truth(args.verbose)
    if args.mode in ("pdf", "all"):
        exit_code |= run_fields("pdf", args.verbose)
    if args.mode in ("scanned", "all"):
        exit_code |= run_fields("scanned", args.verbose)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
