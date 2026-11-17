"""Probe #2: confirm the reliable thinking-off switch under a reasoning-heavy prompt.

Nothing is hard-coded. Point it at your gateway:

    DGXCHAT_TEST_UPSTREAM_URL   gateway base URL (e.g. http://dgx.local:8000/v1)
    DGX_API_KEY                 bearer token, if the gateway needs one
    DGXCHAT_TEST_MODEL          model id (default: deepseek-v4.1-flash)

Usage:
    python tests/_probe_thinking2.py
"""
from __future__ import annotations

import json
import os
import sys

import httpx

BASE = os.environ.get("DGXCHAT_TEST_UPSTREAM_URL") or os.environ.get("DGX_BASE_URL", "")
KEY = os.environ.get("DGX_API_KEY", "")
MODEL = os.environ.get("DGXCHAT_TEST_MODEL", "deepseek-v4.1-flash")
HARD = "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. How much does the ball cost? Answer with just the number."


def headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"} if KEY else {}


def run(extra: dict, prompt: str = HARD, n: int = 3) -> None:
    for i in range(n):
        body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                "stream": True, "max_tokens": 700}
        body.update(extra)
        rc = cc = 0
        finish = None
        status = 200
        err = None
        try:
            with httpx.Client(timeout=180) as c:
                with c.stream("POST", f"{BASE}/chat/completions", json=body,
                              headers=headers()) as r:
                    status = r.status_code
                    if status != 200:
                        err = r.read().decode("utf-8", "replace")[:300]
                    else:
                        for line in r.iter_lines():
                            if not line.startswith("data: ") or line[6:] == "[DONE]":
                                continue
                            try:
                                ch = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            for c0 in ch.get("choices") or []:
                                d = c0.get("delta") or {}
                                rc += len(d.get("reasoning") or "")
                                cc += len(d.get("content") or "")
                                finish = c0.get("finish_reason") or finish
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        print(f"  run{i}: status={status} reasoning_chars={rc} content_chars={cc} finish={finish} err={err}")


def main() -> int:
    if not BASE:
        print("[SKIP] no gateway configured: set DGXCHAT_TEST_UPSTREAM_URL.", file=sys.stderr)
        return 0

    for label, extra in [
        ("baseline", {}),
        ("reasoning_effort=none", {"reasoning_effort": "none"}),
        ("thinking={type:disabled}", {"thinking": {"type": "disabled"}}),
        ("both none+disabled", {"reasoning_effort": "none", "thinking": {"type": "disabled"}}),
        ("thinking={type:enabled}", {"thinking": {"type": "enabled"}}),
        ("both disabled+chat_template_kwargs.thinking=false",
         {"reasoning_effort": "none", "thinking": {"type": "disabled"},
          "chat_template_kwargs": {"thinking": False}}),
    ]:
        print(f"### {label}")
        run(extra)

    # Does a title-style short non-streaming request respect reasoning_effort=none?
    print("### non-streaming title probe (reasoning_effort=none)")
    msg = [{"role": "user", "content": "为这段对话起一个不超过 30 字的标题，只输出标题：用户问快速排序是什么。"}]
    for extra in ({}, {"reasoning_effort": "none"}):
        body = {"model": MODEL, "messages": msg, "stream": False, "max_tokens": 64}
        body.update(extra)
        with httpx.Client(timeout=120) as c:
            r = c.post(f"{BASE}/chat/completions", json=body, headers=headers())
            print(" flags", extra, "status", r.status_code)
            if r.status_code == 200:
                j = r.json()
                m = j["choices"][0]["message"]
                print("  content:", repr(m.get("content"))[:200])
                print("  reasoning:", repr(m.get("reasoning"))[:120])
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
