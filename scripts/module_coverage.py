#!/usr/bin/env python3
"""Enforce per-module coverage thresholds that pytest-cov cannot express."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Maps source-tree-relative file paths to their minimum coverage percentage.
# These mirror the expectation documented in CONTRIBUTING.md.
CRITICAL_THRESHOLDS: dict[str, float] = {
    "src/telegram_sendmail/client.py": 90.0,
    "src/telegram_sendmail/parser.py": 90.0,
    "src/telegram_sendmail/smtp.py": 90.0,
}

COVERAGE_JSON: Path = Path("coverage.json")


def main() -> int:
    if not COVERAGE_JSON.exists():
        print(f"ERROR: {COVERAGE_JSON} not found.", file=sys.stderr)
        return 1

    with COVERAGE_JSON.open() as fh:
        data: dict[str, object] = json.load(fh)

    files = data.get("files")
    if not isinstance(files, dict):
        print("ERROR: unexpected coverage.json structure.", file=sys.stderr)
        return 1

    failed = False

    for module, threshold in sorted(CRITICAL_THRESHOLDS.items()):
        entry = files.get(module)
        if entry is None:
            print(f"FAIL  {module}: not found in coverage report", file=sys.stderr)
            failed = True
            continue

        summary = entry.get("summary", {}) if isinstance(entry, dict) else {}
        pct: float = summary.get("percent_covered", 0.0) if isinstance(summary, dict) else 0.0

        if pct < threshold:
            print(f"FAIL  {module}: {pct:.1f}% < {threshold:.1f}%", file=sys.stderr)
            failed = True
        else:
            print(f"OK    {module}: {pct:.1f}% >= {threshold:.1f}%")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
