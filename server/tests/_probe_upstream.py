"""Throwaway probe: measure upstream behaviour for thinking on/off + continuation.

Not part of the test suite; kept as the provenance for the README conclusions.
Nothing is hard-coded: set DGXCHAT_TEST_UPSTREAM_URL (and DGX_API_KEY if the
gateway needs auth).

Run: python tests/_probe_upstream.py
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("DGXCHAT_TEST_UPSTREAM_URL") or os.environ.get("DGX_BASE_URL", "")
KEY = os.environ.get("DGX_API_KEY", "")
MODEL = os.environ.get("DGXCHAT_TEST_MODEL", "deepseek-v4.1-flash")


def headers() -> dict:
    return {"Authorization": f"Bearer {KEY}"} if KEY else {}


def stream(payload: dict, timeout: float = 120.0) -> dict:
    """POST a streaming chat completion, summarise what came back."""
    url = f"{BASE}/chat/completions"
    body = {"model": MODEL, "messages": [{"role": "user", "content": "1+1=?"}], "stream": True,
            "max_tokens": 200}
    body.update(payload)
    t0 = time.time()
    reasoning, content, finish, usage, err = [], [], None, None, None
    status = None
    try:
        with httpx.Client(timeout=timeout) as c:
            with c.stream("POST", url, json=body, headers=headers()) as r:
                status = r.status_code
                if r.status_code != 200:
                    err = r.read().decode("utf-8", "replace")[:600]
                else:
                    for line in r.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for ch in chunk.get("choices") or []:
                            d = ch.get("delta") or {}
                            if d.get("reasoning"):
                                reasoning.append(d["reasoning"])
                            if d.get("content"):
                                content.append(d["content"])
                            if ch.get("finish_reason"):
                                finish = ch["finish_reason"]
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "reasoning_chars": sum(len(x) for x in reasoning),
        "content_chars": sum(len(x) for x in content),
        "content": "".join(content)[:120],
        "finish": finish,
        "usage": usage,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "error": err,
    }


CASES: list[tuple[str, dict]] = [
    ("baseline (no flags)", {}),
    ("chat_template_kwargs.thinking=false", {"chat_template_kwargs": {"thinking": False}}),
    ("chat_template_kwargs.thinking=true", {"chat_template_kwargs": {"thinking": True}}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("enable_thinking=true", {"enable_thinking": True}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("reasoning_effort=low", {"reasoning_effort": "low"}),
    ("reasoning_effort=medium", {"reasoning_effort": "medium"}),
    ("reasoning_effort=high", {"reasoning_effort": "high"}),
    ("thinking={type:disabled}", {"thinking": {"type": "disabled"}}),
    ("thinking={type:enabled}", {"thinking": {"type": "enabled"}}),
    ("stream_options.include_usage", {"stream_options": {"include_usage": True}}),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not BASE:
        print("[SKIP] no gateway configured: set DGXCHAT_TEST_UPSTREAM_URL.", file=sys.stderr)
        return 0
    for name, extra in CASES:
        r = stream(extra)
        print(f"### {name}")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        print()

    # continuation: prefill a trailing assistant turn
    print("### continue_final_message probe")
    body = {
        "model": MODEL,
        "messages": [
            {"role": "user", "content": "Write a haiku about the sea."},
            {"role": "assistant", "content": "The sea is "},
        ],
        "stream": True,
        "max_tokens": 80,
        "continue_final_message": True,
        "add_generation_prompt": False,
    }
    try:
        out = []
        with httpx.Client(timeout=120) as c:
            with c.stream("POST", f"{BASE}/chat/completions", json=body, headers=headers()) as r:
                print("status", r.status_code)
                if r.status_code != 200:
                    print(r.read().decode("utf-8", "replace")[:600])
                else:
                    for line in r.iter_lines():
                        if line.startswith("data: ") and line[6:] != "[DONE]":
                            try:
                                ch = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            for c0 in ch.get("choices") or []:
                                d = c0.get("delta") or {}
                                if d.get("content"):
                                    out.append(d["content"])
        print("continuation:", "".join(out)[:300])
    except Exception as exc:  # noqa: BLE001
        print("continuation failed:", type(exc).__name__, exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
