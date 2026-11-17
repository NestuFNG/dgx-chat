"""Ad-hoc manual driver: register/login, then stream one real turn from the DGX gateway.

Usage: python tests\\_manual_turn.py [--search] [--thinking] [--title] [--plain]
"""
from __future__ import annotations

import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8439"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    flags = set(sys.argv[1:])
    question = next((f.split("=", 1)[1] for f in flags if f.startswith("--q=")), None) or (
        "用两句话解释什么是快速排序。"
    )
    username = f"dev{int(time.time()) % 100000}"
    with httpx.Client(timeout=60) as client:
        r = client.post(f"{BASE}/auth/register", json={"username": username, "password": "devpass123"})
        print("register:", r.status_code, r.text[:200])
        token = r.json()["token"]
        headers = {"x-api-key": token, "content-type": "application/json"}

        r = client.get(f"{BASE}/models", headers=headers)
        print("models:", r.status_code, r.text[:300])
        r = client.get(f"{BASE}/auth/me", headers=headers)
        print("me:", r.status_code, r.text[:200])

        body = {
            "model": "deepseek-flash",
            "system": "该助手为DeepSeek，由深度求索公司创造。",
            "messages": [{"role": "user", "content": question}],
            "thinking": {"type": "enabled" if "--thinking" in flags else "disabled"},
            "safety_check": {"level": "high"},
            "tools": [],
        }
        if "--search" in flags:
            body["tools"] = [
                {"type": "deepseek_web_search", "name": "deepseek_web_search"},
                {"type": "deepseek_read_url", "name": "deepseek_read_url"},
            ]
        if "--title" in flags:
            body["title"] = {"enabled": True}
        if "--plain" in flags:
            body["tools"] = []

        started = time.time()
        events: list[str] = []
        text = ""
        thinking = ""
        title = None
        stop_reason = None
        error = None
        with client.stream(
            "POST", f"{BASE}/anthropic/v1/messages", headers=headers, json=body
        ) as response:
            print("stream status:", response.status_code)
            print("headers:", {k: v for k, v in response.headers.items() if k.startswith("x-")})
            event_name = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event_name = line[7:]
                    events.append(event_name)
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
                    if data.get("type") == "content_block_delta":
                        delta = data["delta"]
                        if delta["type"] == "text_delta":
                            text += delta["text"]
                        elif delta["type"] == "thinking_delta":
                            thinking += delta["thinking"]
                        elif delta["type"] == "citations_delta":
                            print("  citation:", delta["citation"]["url"], "|", delta["citation"]["title"][:40])
                    elif data.get("type") == "content_block_start":
                        block = data["content_block"]
                        summary = block.get("type")
                        if block.get("type") == "server_tool_use":
                            summary += f" id={block.get('id')} name={block.get('name')} input={json.dumps(block.get('input'), ensure_ascii=False)[:120]}"
                        if block.get("type") == "web_search_tool_result":
                            items = block.get("content") or []
                            summary += f" items={len(items)}"
                            for item in items[:3]:
                                if isinstance(item, dict):
                                    print("   source:", item.get("title", "")[:60], "|", item.get("url"))
                        if block.get("type") == "web_fetch_tool_result":
                            summary += f" kind={block.get('kind')} content={block.get('content', {}).get('type')}"
                            print("   fetch content:", json.dumps(block.get("content"), ensure_ascii=False)[:200])
                        print("block_start:", summary)
                    elif data.get("type") == "message_delta":
                        stop_reason = data["delta"].get("stop_reason")
                        title = data["delta"].get("title")
                        print("usage:", data.get("usage"))
                    elif data.get("type") == "error":
                        error = data["error"]
        elapsed = int((time.time() - started) * 1000)
        print("\n--- event sequence ---")
        print(" -> ".join(events))
        print(f"\nelapsed={elapsed}ms stop={stop_reason} title={title!r} thinking_chars={len(thinking)} text_chars={len(text)}")
        print("text:", text[:400])
        if error:
            print("ERROR:", error)
        assert events[0] == "message_start" and events[-1] == "message_stop", events
        assert text, "no text arrived"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
