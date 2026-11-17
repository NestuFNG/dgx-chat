"""Probe: which enable-thinking switch works on your public gateway vs the LAN one?

Nothing is hard-coded. Configure the endpoints you want to probe:

    DGXCHAT_TEST_UPSTREAM_URL   LAN / local gateway
    DGXCHAT_TEST_PUBLIC_URL     public gateway
    DGXCHAT_TEST_PUBLIC_KEY     key for the public gateway (if any)
    DGXCHAT_TEST_MODEL          model id (default: deepseek-v4.1-flash-official)

Usage:
    python tests/_probe_enable_thinking.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

LAN = (
    os.environ.get("DGXCHAT_TEST_UPSTREAM_URL")
    or os.environ.get("DGX_BASE_URL", ""),
    os.environ.get("DGX_API_KEY", ""),
)
PUBLIC = (
    os.environ.get("DGXCHAT_TEST_PUBLIC_URL", ""),
    os.environ.get("DGXCHAT_TEST_PUBLIC_KEY", ""),
)
MODEL = os.environ.get("DGXCHAT_TEST_MODEL", "deepseek-v4.1-flash-official")

CASES = [
    ("baseline", {}),
    ("enable_thinking:true", {"enable_thinking": True}),
    ("chat_template_kwargs:{thinking:true}", {"chat_template_kwargs": {"thinking": True}}),
    ("thinking:{type:enabled}", {"thinking": {"type": "enabled"}}),
    ("reasoning_effort:high", {"reasoning_effort": "high"}),
    ("reasoning_effort:medium", {"reasoning_effort": "medium"}),
    ("both enable hints", {"enable_thinking": True, "chat_template_kwargs": {"thinking": True}}),
]


def probe(base: str, key: str, label: str) -> None:
    print(f"\n=== {label} {base} ===")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    for name, extra in CASES:
        body = {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "A bat and ball cost $1.10 total. The bat costs $1 more than the ball. Ball price? Answer with the number only."}
            ],
            "stream": True,
            "max_tokens": 600,
        }
        body.update(extra)
        reasoning = content = ""
        status = None
        error = None
        started = time.time()
        try:
            with httpx.Client(timeout=120) as client:
                with client.stream(
                    "POST", f"{base}/chat/completions", json=body, headers=headers
                ) as response:
                    status = response.status_code
                    if status != 200:
                        error = response.read().decode("utf-8", "replace")[:200]
                    else:
                        for line in response.iter_lines():
                            if not line.startswith("data: ") or line[6:] == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            for choice in chunk.get("choices") or []:
                                delta = choice.get("delta") or {}
                                reasoning += delta.get("reasoning") or ""
                                content += delta.get("content") or ""
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        elapsed = int((time.time() - started) * 1000)
        print(
            f"  {name:42s} status={status} reasoning={len(reasoning):4d} "
            f"content={len(content):3d} {elapsed:5d}ms {('err=' + error) if error else ''}"
        )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ran = False
    for target, label in ((LAN, "LAN"), (PUBLIC, "PUBLIC")):
        if not target[0]:
            print(f"[SKIP] {label}: no URL configured "
                  f"({'DGXCHAT_TEST_UPSTREAM_URL' if label == 'LAN' else 'DGXCHAT_TEST_PUBLIC_URL'})")
            continue
        probe(*target, label)
        ran = True
    if not ran:
        print("\nNothing to probe. Set DGXCHAT_TEST_UPSTREAM_URL and/or "
              "DGXCHAT_TEST_PUBLIC_URL.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
