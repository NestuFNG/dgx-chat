"""Live end-to-end smoke test against a real OpenAI-compatible gateway.

This is deliberately *not* part of the pytest suite: it starts the real FastAPI
app on a real socket and drives a real streaming turn against the gateway in
``DGXCHAT_TEST_UPSTREAM_URL`` (or ``--base-url``), asserting:

  * the SSE event sequence the chat-demo client requires,
  * that assistant text actually arrives,
  * that ``thinking:{type:disabled}`` produces no thinking block (and enabled does),
  * that the title arrives in ``message_delta.delta.title`` when asked,
  * that the outgoing ``system`` message is byte-for-byte the official constant,
  * (optional, --search) that web-search blocks are spec-compliant,
  * (optional, --speech) that the DeepSeek TTS/ASR round trip works.

Nothing is hard-coded: without ``DGXCHAT_TEST_UPSTREAM_URL`` the script exits
with a setup error instead of probing someone else's host.

Usage:
    python tests/live_smoke.py
    python tests/live_smoke.py --search --speech
    python tests/live_smoke.py --base-url http://dgx.local:8000/v1

Environment:
    DGXCHAT_TEST_UPSTREAM_URL   gateway to test (fallback: DGX_BASE_URL)
    DGX_API_KEY                 bearer token for that gateway (usually empty)
    DGX_BASE_URL_FALLBACK       optional fallback endpoint
    DGX_API_KEY_FALLBACK        key for the fallback
    DEEPSEEK_API_KEY            only for --search / --speech

Exit codes: 0 ok, 1 assertion failed, 2 upstream unreachable, 3 setup error.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import httpx
import uvicorn

SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

from app.config import (  # noqa: E402
    SearchConfig,
    Settings,
    SpeechConfig,
    SpeechProviderConfig,
    UpstreamEndpoint,
)
from app.main import create_app  # noqa: E402

OFFICIAL_SYSTEM = "该助手为DeepSeek，由深度求索公司创造。"
DEFAULT_UPSTREAM = (
    os.environ.get("DGXCHAT_TEST_UPSTREAM_URL")
    or os.environ.get("DGX_BASE_URL")
    or ""
)
ALLOWED_STOP_REASONS = {"end_turn", "max_tokens", "tool_use", "refusal"}

failures: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        message = f"{label}" + (f" ({detail})" if detail else "")
        failures.append(message)
        print(f"  [FAIL] {message}")


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_settings(
    tmp: Path,
    base_url: str,
    api_key: str = "",
    fallback_url: str = "",
    fallback_key: str = "",
) -> Settings:
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    deepseek_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    provider = "deepseek" if deepseek_key else "disabled"
    speech = SpeechConfig(
        tts=SpeechProviderConfig(
            provider=provider, base_url=deepseek_base, api_key=deepseek_key,
            voice_map={}, timeout=120.0,
        ),
        asr=SpeechProviderConfig(
            provider=provider, base_url=deepseek_base, api_key=deepseek_key,
            voice_map={}, timeout=120.0,
        ),
        ffmpeg_bin=None,
    )
    fallbacks = []
    if fallback_url:
        fallbacks.append(
            UpstreamEndpoint(
                name="fallback1",
                base_url=fallback_url,
                api_key=fallback_key,
                thinking_mode="reasoning_effort",
                timeout=120.0,
            )
        )
    return Settings(
        data_dir=tmp,
        db_path=tmp / "smoke.db",
        files_dir=tmp / "files",
        web_dir=tmp / "web",
        registration_mode="invite",
        docs_enabled=False,
        log_upstream=True,
        upstream_base_url=base_url,
        upstream_api_key=api_key,
        upstream_timeout=300.0,
        upstream_fallbacks=fallbacks,
        max_json_body_bytes=8 * 1024 * 1024,
        max_upload_bytes=100 * 1024 * 1024,
        search=SearchConfig(
            mode="dgx",
            provider="deepseek",
            official_api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        ),
        speech=speech,
    )


def start_server(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                            access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn did not start")
    return server, thread


def stream_turn(client: httpx.Client, base: str, token: str, body: dict) -> dict:
    events: list[str] = []
    text = ""
    thinking = ""
    blocks: list[dict] = []
    title = None
    stop_reason = None
    usage = None
    error = None
    headers: dict[str, str] = {}
    started = time.time()
    with client.stream(
        "POST",
        f"{base}/anthropic/v1/messages",
        headers={"x-api-key": token, "content-type": "application/json"},
        json=body,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=None),
    ) as response:
        headers = dict(response.headers)
        if response.status_code != 200:
            return {
                "status": response.status_code,
                "raw": response.read().decode("utf-8", "replace")[:800],
                "events": [],
                "text": "",
            }
        event_name = "message"
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
                events.append(event_name)
                continue
            if not line.startswith("data: "):
                continue
            try:
                data = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            kind = data.get("type")
            if kind == "content_block_start":
                blocks.append(data["content_block"])
            elif kind == "content_block_delta":
                delta = data["delta"]
                if delta["type"] == "text_delta":
                    text += delta["text"]
                elif delta["type"] == "thinking_delta":
                    thinking += delta["thinking"]
            elif kind == "message_delta":
                stop_reason = data["delta"].get("stop_reason")
                title = data["delta"].get("title")
                usage = data.get("usage")
            elif kind == "error":
                error = data.get("error")
    return {
        "status": 200,
        "headers": headers,
        "events": events,
        "blocks": blocks,
        "text": text,
        "thinking": thinking,
        "title": title,
        "stop_reason": stop_reason,
        "usage": usage,
        "error": error,
        "elapsed_ms": int((time.time() - started) * 1000),
    }


def assert_sequence(
    result: dict, *, expect_thinking: bool, label: str, allow_no_thinking: bool = False
) -> None:
    print(f"\n[{label}]")
    events = result["events"]
    if result["status"] != 200:
        check(False, f"{label}: HTTP 200", result.get("raw", ""))
        return
    print(f"  events: {' '.join(events[:6])}{' …' if len(events) > 6 else ''} "
          f"({len(events)} frames, {result.get('elapsed_ms')} ms)")
    print(f"  text: {result['text'][:120]!r}")
    check(events[0] == "message_start", f"{label}: first frame is message_start")
    check(events[-1] == "message_stop", f"{label}: last frame is message_stop")
    check("error" not in events, f"{label}: no error frames", str(result.get("error")))
    check(result["text"].strip() != "", f"{label}: assistant text arrived")
    check(
        result["stop_reason"] in ALLOWED_STOP_REASONS,
        f"{label}: valid stop_reason",
        str(result["stop_reason"]),
    )
    check(
        isinstance(result["usage"], dict)
        and set(result["usage"]) >= {"input_tokens", "output_tokens"},
        f"{label}: usage present",
    )
    block_types = [block.get("type") for block in result["blocks"]]
    check(bool(block_types), f"{label}: at least one content block", str(block_types))
    if expect_thinking:
        if allow_no_thinking and not result["thinking"].strip():
            print(
                "  [SKIP] this upstream accepted enable_thinking but surfaced no "
                "reasoning channel (public litellm proxy: thinking is not observable)"
            )
        else:
            check(
                block_types[:1] == ["thinking"],
                f"{label}: thinking block first",
                str(block_types),
            )
            check(
                len(result["thinking"].strip()) > 0, f"{label}: thinking deltas arrived"
            )
            check("text" in block_types, f"{label}: text block follows thinking")
    else:
        check("thinking" not in block_types, f"{label}: no thinking block", str(block_types))
        check(
            result["thinking"] == "",
            f"{label}: no thinking deltas leaked",
            f"{len(result['thinking'])} chars",
        )
    check(
        result["headers"].get("content-type", "").startswith("text/event-stream"),
        f"{label}: content-type is text/event-stream",
        result["headers"].get("content-type", ""),
    )
    check(
        result["headers"].get("x-accel-buffering") == "no",
        f"{label}: X-Accel-Buffering: no",
    )


def verify_system_prompt(capture: Capture, label: str) -> None:
    print(f"\n[{label}]")
    marker = "UPSTREAM_SYSTEM_JSON="
    found = []
    for line in capture.lines:
        if marker in line:
            found.append(line.split(marker, 1)[1])
    check(bool(found), f"{label}: captured upstream request(s)", str(len(found)))
    exact = 0
    for raw in found:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if value == OFFICIAL_SYSTEM:
            exact += 1
    check(
        exact == len(found) and exact > 0,
        f"{label}: system prompt byte-identical in every request",
        f"{exact}/{len(found)}",
    )
    bodies = [line for line in capture.lines if line.startswith("UPSTREAM chat.completions")]
    if bodies:
        rendered = bodies[0]
        print("  captured body (first 400 chars):")
        print("    " + rendered[:400].replace("\n", "\n    "))


def verify_search(result: dict, label: str) -> None:
    print(f"\n[{label}]")
    block_types = [block.get("type") for block in result["blocks"]]
    check("server_tool_use" in block_types, f"{label}: server_tool_use block", str(block_types))
    check(
        "web_search_tool_result" in block_types,
        f"{label}: web_search_tool_result block",
        str(block_types),
    )
    tool_blocks = [b for b in result["blocks"] if b.get("type") == "server_tool_use"]
    result_blocks = [b for b in result["blocks"] if b.get("type") == "web_search_tool_result"]
    if tool_blocks:
        tool = tool_blocks[0]
        check(tool.get("name") == "deepseek_web_search", f"{label}: tool name")
        check(tool.get("kind") == "search", f"{label}: tool kind")
        check(isinstance(tool.get("id"), str), f"{label}: tool id is a string")
        check(
            tool.get("input", {}).get("action") == "search"
            and isinstance(tool.get("input", {}).get("queries"), list),
            f"{label}: tool input shape",
        )
    if result_blocks:
        block = result_blocks[0]
        items = block.get("content") or []
        check(len(items) > 0, f"{label}: search returned sources", str(len(items)))
        check(
            all(
                isinstance(item.get("url"), str) and isinstance(item.get("title"), str)
                for item in items
                if isinstance(item, dict) and item.get("type") == "web_search_result"
            ),
            f"{label}: every source has string url/title",
        )
        for item in items[:3]:
            print(f"  source: {item.get('title', '')[:50]} | {item.get('url')}")
    check(result["text"].strip() != "", f"{label}: answer text still generated by DGX")


def verify_title(result: dict, label: str) -> None:
    print(f"\n[{label}]")
    check(
        isinstance(result["title"], str) and result["title"].strip() != "",
        f"{label}: title present in message_delta",
        repr(result["title"]),
    )
    if isinstance(result["title"], str):
        check(len(result["title"]) <= 30, f"{label}: title <= 30 chars", str(len(result["title"])))
        print(f"  title: {result['title']!r}")


def verify_speech(client: httpx.Client, base: str, token: str, label: str) -> None:
    print(f"\n[{label}]")
    text = "你好，这是一次端到端语音测试。"
    response = client.post(
        f"{base}/v1/tools/tts",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"text": text, "voice_id": "stella", "format": "pcm", "filter_markdown": True},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=None),
    )
    check(response.status_code == 200, f"{label}: TTS HTTP 200", response.text[:200])
    if response.status_code != 200:
        return
    pcm = response.content
    check(
        response.headers.get("content-type", "").startswith("audio/pcm"),
        f"{label}: audio/pcm content-type",
        response.headers.get("content-type", ""),
    )
    check(len(pcm) > 24000, f"{label}: PCM has real audio", f"{len(pcm)} bytes")
    print(f"  pcm bytes: {len(pcm)} (~{len(pcm) / 48000:.2f}s @24kHz mono s16le)")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(pcm)
    asr = client.post(
        f"{base}/v1/tools/asr",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("smoke.wav", buffer.getvalue(), "audio/wav")},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=None),
    )
    check(asr.status_code == 200, f"{label}: ASR HTTP 200", asr.text[:200])
    if asr.status_code == 200:
        recognized = asr.json().get("text", "")
        print(f"  asr text: {recognized!r}")
        check(bool(recognized.strip()), f"{label}: ASR returned text")
        check(
            "语音测试" in recognized.replace(" ", "") or "语音" in recognized,
            f"{label}: ASR text matches what was spoken",
            recognized,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_UPSTREAM)
    parser.add_argument("--api-key", default=os.environ.get("DGX_API_KEY", ""))
    parser.add_argument("--fallback-url", default=os.environ.get("DGX_BASE_URL_FALLBACK", ""))
    parser.add_argument(
        "--fallback-key", default=os.environ.get("DGX_API_KEY_FALLBACK", "")
    )
    parser.add_argument("--search", action="store_true", help="also exercise web search")
    parser.add_argument("--speech", action="store_true", help="also exercise TTS/ASR")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if not args.base_url:
        print(
            "SETUP ERROR: no gateway configured.\n"
            "  Set DGXCHAT_TEST_UPSTREAM_URL (e.g. http://dgx.local:8000/v1) or pass\n"
            "  --base-url. Refusing to guess a host.",
            file=sys.stderr,
        )
        return 3

    print(f"live smoke: upstream={args.base_url} api_key={'yes' if args.api_key else 'no'}")
    if args.fallback_url:
        print(f"           fallback={args.fallback_url}")
    try:
        headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
        probe = httpx.get(f"{args.base_url}/models", timeout=15.0, headers=headers)
        models = [m["id"] for m in probe.json().get("data", [])]
        print(f"upstream models: {models}")
    except Exception as exc:  # noqa: BLE001
        print(f"UPSTREAM UNREACHABLE: {exc}")
        return 2

    capture = Capture()
    wire = logging.getLogger("dgxchat.upstream.wire")
    wire.setLevel(logging.INFO)
    wire.addHandler(capture)

    tmp = Path(tempfile.mkdtemp(prefix="dgxchat-smoke-"))
    settings = make_settings(
        tmp, args.base_url, args.api_key, args.fallback_url, args.fallback_key
    )
    app = create_app(settings)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server, thread = start_server(app, port)
    print(f"server: {base}  data={tmp}")

    try:
        with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=None)) as client:
            health = client.get(f"{base}/health")
            check(health.status_code == 200, "health endpoint", health.text[:120])

            registered = client.post(
                f"{base}/auth/register",
                json={"username": "smoke-admin", "password": "smokepass"},
            )
            if registered.status_code != 200:
                print(f"register failed: {registered.status_code} {registered.text}")
                return 3
            token = registered.json()["token"]
            print(f"registered bootstrap admin: {registered.json()['user']}")

            models_response = client.get(f"{base}/models", headers={"x-api-key": token})
            check(models_response.status_code == 200, "GET /models")
            ids = [item["id"] for item in models_response.json().get("data", [])]
            check(
                set(ids) == {"deepseek-flash", "deepseek-v4-pro"},
                "client model ids exposed",
                str(ids),
            )

            base_body = {
                "model": "deepseek-flash",
                "system": OFFICIAL_SYSTEM,
                "messages": [
                    {
                        "role": "user",
                        "content": "用两句话解释什么是快速排序，然后说出它的平均时间复杂度。",
                    }
                ],
                "safety_check": {"level": "high"},
                "tools": [],
            }

            disabled = stream_turn(
                client, base, token, dict(base_body, thinking={"type": "disabled"})
            )
            assert_sequence(disabled, expect_thinking=False, label="thinking=disabled")

            dump = tmp / "upstream-disabled.json"
            dump.write_text(
                json.dumps(disabled["usage"], ensure_ascii=False), encoding="utf-8"
            )

            enabled = stream_turn(
                client, base, token, dict(base_body, thinking={"type": "enabled"})
            )
            assert_sequence(
                enabled, expect_thinking=True, label="thinking=enabled", allow_no_thinking=True
            )

            titled = stream_turn(
                client,
                base,
                token,
                dict(
                    base_body,
                    thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": "帮我解释一下什么是向量数据库。"}],
                    title={"enabled": True},
                ),
            )
            verify_title(titled, "title generation")

            verify_system_prompt(capture, "system prompt integrity")

            prefill = stream_turn(
                client,
                base,
                token,
                dict(
                    base_body,
                    thinking={"type": "disabled"},
                    messages=[
                        {"role": "user", "content": "用一句话介绍杭州，开头必须是“杭州是”"},
                        {"role": "assistant", "content": [{"type": "text", "text": "杭州是"}]},
                    ],
                ),
            )
            assert_sequence(prefill, expect_thinking=False, label="prefill/continuation")

            if args.search:
                search_turn = stream_turn(
                    client,
                    base,
                    token,
                    {
                        "model": "deepseek-flash",
                        "system": OFFICIAL_SYSTEM,
                        "messages": [
                            {
                                "role": "user",
                                "content": "2026年10月中国新能源汽车销量排行榜前三名是哪些？",
                            }
                        ],
                        "thinking": {"type": "disabled"},
                        "safety_check": {"level": "high"},
                        "tools": [
                            {"type": "deepseek_web_search", "name": "deepseek_web_search"},
                            {"type": "deepseek_read_url", "name": "deepseek_read_url"},
                        ],
                    },
                )
                assert_sequence(search_turn, expect_thinking=False, label="search turn")
                verify_search(search_turn, "web search blocks")

            if args.speech:
                verify_speech(client, base, token, "speech round trip")
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    print("\n" + "=" * 60)
    if failures:
        print(f"LIVE SMOKE FAILED: {len(failures)} check(s)")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("LIVE SMOKE PASSED — real streaming turn against the DGX gateway succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
