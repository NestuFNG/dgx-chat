"""Verify the forwarded system prompt is byte-for-byte identical to the client's.

Reads every ``UPSTREAM_SYSTEM_JSON=`` line the server logged under
``DGX_LOG_UPSTREAM=1`` and compares it with the official constant.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EXPECTED = "该助手为DeepSeek，由深度求索公司创造。"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parent.parent
    logs = list((root / "data").glob("*.log"))
    if not logs:
        print("no log files found under data/")
        return 2
    total = 0
    exact = 0
    for log in logs:
        text = log.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            marker = "UPSTREAM_SYSTEM_JSON="
            if marker not in line:
                continue
            total += 1
            raw = line.split(marker, 1)[1]
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                print("unparseable log line:", raw[:120])
                continue
            if value == EXPECTED:
                exact += 1
            else:
                print("MISMATCH:", repr(value), "vs", repr(EXPECTED))
    print(f"occurrences={total} byte_identical={exact}")
    if total == 0:
        print("no UPSTREAM_SYSTEM_JSON lines: rerun with DGX_LOG_UPSTREAM=1")
        return 3
    return 0 if exact == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
