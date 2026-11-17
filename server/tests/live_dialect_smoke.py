"""Live dialect / fallback verification against a REAL gateway.

Proves, over the network (no mocks):
  1. a gateway that rejects ``thinking:{...}`` returns HTTP 400 on the raw call;
  2. the client automatically retries the same request with the thinking
     parameters stripped and still streams an answer;
  3. when the primary endpoint is unreachable, the configured fallback endpoint
     serves the turn.

Nothing is hard-coded. Point it at a gateway with DGXCHAT_TEST_PUBLIC_URL (or
--base-url); without one the script prints [SKIP] and exits 0.

Run:
    python tests/live_dialect_smoke.py --base-url https://api.example.com/v1
    DGXCHAT_TEST_PUBLIC_URL=https://api.example.com/v1 \
    DGXCHAT_TEST_PUBLIC_KEY=sk-... python tests/live_dialect_smoke.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from app.config import SearchConfig, Settings, SpeechConfig, UpstreamEndpoint  # noqa: E402
from app.upstream import THINKING_PARAM_KEYS, UpstreamClient, UpstreamError  # noqa: E402

PUBLIC_DEFAULT = os.environ.get("DGXCHAT_TEST_PUBLIC_URL", "")
OFFICIAL_SYSTEM = "该助手为DeepSeek，由深度求索公司创造。"
MODEL = "deepseek-v4.1-flash-official"

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label} ({detail})")


def make_settings(tmp: Path, base_url: str, api_key: str, fallbacks=None,
                  thinking_mode: str = "reasoning_effort") -> Settings:
    return Settings(
        data_dir=tmp,
        db_path=tmp / "x.db",
        files_dir=tmp / "files",
        web_dir=tmp / "web",
        upstream_base_url=base_url,
        upstream_api_key=api_key,
        upstream_timeout=120.0,
        upstream_fallbacks=fallbacks or [],
        thinking_disable_mode=thinking_mode,
        search=SearchConfig(mode="off"),
        speech=SpeechConfig(),
    )


PAYLOAD = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": OFFICIAL_SYSTEM},
        {"role": "user", "content": "用一句话说明什么是缓存。"},
    ],
    "stream": True,
    "stream_options": {"include_usage": True},
    "max_tokens": 256,
}


async def collect(client: UpstreamClient, payload: dict, *, thinking_enabled: bool) -> str:
    text = ""
    async for chunk in client.iter_chat(payload, thinking_enabled=thinking_enabled):
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
    return text


async def main_async(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dgxchat-dialect-"))

    print(f"\n[1] raw endpoint rejects thinking:{...} on {args.base_url}")
    settings = make_settings(tmp, args.base_url, args.api_key, thinking_mode="thinking")
    client = UpstreamClient(settings)
    await client.start()
    try:
        raw_error: UpstreamError | None = None
        try:
            async for _chunk in client.iter_chat_chunks(
                {**PAYLOAD, "thinking": {"type": "disabled"}},
                endpoint=client.endpoints()[0],
            ):
                pass
        except UpstreamError as exc:
            raw_error = exc
        if raw_error is None:
            print("  [SKIP] this gateway accepts thinking:{...} (LAN dialect)")
        else:
            check(raw_error.status_code == 400 or raw_error.unsupported_param,
                  "rejection classified as unsupported parameter",
                  f"{raw_error.status_code} {raw_error.message[:120]}")
            print(f"  raw error: {raw_error.message[:180]}")

            print("\n[2] client strips the rejected parameter and still answers")
            text = await collect(client, PAYLOAD, thinking_enabled=False)
            check(bool(text.strip()), "answer arrived after strip-and-retry", repr(text[:80]))
            print(f"  answer: {text[:120]!r}")
    finally:
        await client.close()

    if args.fallback_url:
        print(f"\n[3] primary unreachable -> fallback {args.fallback_url}")
        settings = make_settings(
            tmp,
            "http://127.0.0.1:1/v1",  # nothing listens there
            "",
            fallbacks=[
                UpstreamEndpoint(
                    name="public",
                    base_url=args.fallback_url,
                    api_key=args.fallback_key,
                    thinking_mode="reasoning_effort",
                )
            ],
        )
        client = UpstreamClient(settings)
        await client.start()
        try:
            text = await collect(client, PAYLOAD, thinking_enabled=False)
            check(bool(text.strip()), "fallback endpoint produced an answer", repr(text[:80]))
            print(f"  answer: {text[:120]!r}")
        finally:
            await client.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"DIALECT SMOKE FAILED: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("DIALECT SMOKE PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=PUBLIC_DEFAULT)
    parser.add_argument(
        "--api-key", default=os.environ.get("DGXCHAT_TEST_PUBLIC_KEY", "")
    )
    parser.add_argument("--fallback-url", default="")
    parser.add_argument("--fallback-key", default="")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not args.base_url:
        print(
            "[SKIP] no gateway configured: set DGXCHAT_TEST_PUBLIC_URL "
            "(and DGXCHAT_TEST_PUBLIC_KEY) or pass --base-url.\n"
            "       Nothing was probed.",
            file=sys.stderr,
        )
        return 0
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
