"""Ad-hoc checks for prefill, images/documents, read_url and the system-prompt diff.

Usage: python tests\\_manual_features.py
"""
from __future__ import annotations

import json
import struct
import sys
import time
import zlib

import httpx

BASE = "http://127.0.0.1:8439"
SYSTEM = "该助手为DeepSeek，由深度求索公司创造。"


def make_png(width: int = 64, height: int = 64, rgb=(220, 40, 40)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def login(client: httpx.Client) -> str:
    username = f"feat{int(time.time() * 1000) % 1000000}"
    response = client.post(
        f"{BASE}/auth/register", json={"username": username, "password": "devpass123"}
    )
    response.raise_for_status()
    return response.json()["token"]


def stream(client: httpx.Client, token: str, body: dict) -> dict:
    events: list[str] = []
    text = ""
    thinking = ""
    blocks: list[str] = []
    title = None
    error = None
    with client.stream(
        "POST",
        f"{BASE}/anthropic/v1/messages",
        headers={"x-api-key": token, "content-type": "application/json"},
        json=body,
    ) as response:
        if response.status_code != 200:
            return {"status": response.status_code, "body": response.text}
        event_name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event_name = line[7:]
                events.append(event_name)
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if data.get("type") == "content_block_start":
                    blocks.append(data["content_block"].get("type"))
                elif data.get("type") == "content_block_delta":
                    delta = data["delta"]
                    if delta["type"] == "text_delta":
                        text += delta["text"]
                    elif delta["type"] == "thinking_delta":
                        thinking += delta["thinking"]
                elif data.get("type") == "message_delta":
                    title = data["delta"].get("title")
                elif data.get("type") == "error":
                    error = data["error"]
    return {
        "status": 200,
        "events": events,
        "text": text,
        "thinking": thinking,
        "blocks": blocks,
        "title": title,
        "error": error,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with httpx.Client(timeout=180) as client:
        token = login(client)
        base_body = {
            "model": "deepseek-flash",
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "你好"}],
            "thinking": {"type": "disabled"},
            "safety_check": {"level": "high"},
            "tools": [],
        }

        print("=== 1. prefill / continuation ===")
        body = dict(base_body, messages=[
            {"role": "user", "content": "用一句话介绍杭州。开头必须是：杭州是"},
            {"role": "assistant", "content": "杭州是"},
        ])
        result = stream(client, token, body)
        print("blocks:", result["blocks"], "title:", result["title"])
        print("text:", result["text"][:200])
        assert result["status"] == 200, result
        assert result["text"].strip(), "prefill produced no text"

        print("\n=== 2. image via Files API ===")
        png = make_png()
        upload = client.post(
            f"{BASE}/anthropic/v1/files",
            headers={"x-api-key": token, "anthropic-beta": "files-api-2025-04-14"},
            files={"file": ("red.png", png, "image/png")},
        )
        print("upload:", upload.status_code, upload.text[:200])
        upload.raise_for_status()
        file_id = upload.json()["id"]
        body = dict(base_body, messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图片是什么颜色？只回答一个词。"},
                {"type": "image", "source": {"type": "file", "file_id": file_id}},
            ],
        }])
        result = stream(client, token, body)
        print("text:", result["text"][:120])
        assert result["status"] == 200, result
        assert result["text"].strip(), "image turn produced no text"

        print("\n=== 3. document (text) via Files API ===")
        doc = "季度销售额：Q1=100，Q2=150，Q3=90。"
        upload = client.post(
            f"{BASE}/anthropic/v1/files",
            headers={"x-api-key": token},
            files={"file": ("sales.csv", doc.encode("utf-8"), "text/csv")},
        )
        file_id = upload.json()["id"]
        body = dict(base_body, messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "文档里 Q2 的销售额是多少？只回答数字。"},
                {"type": "document", "source": {"type": "file", "file_id": file_id}},
            ],
        }])
        result = stream(client, token, body)
        print("text:", result["text"][:120])
        assert "150" in result["text"], result["text"]

        print("\n=== 4. latest_reminder folding ===")
        body = dict(base_body, messages=[
            {"role": "latest_reminder", "content": "当前时间是 2026-11-17 13:00（Asia/Shanghai）。"},
            {"role": "user", "content": "现在几号？只回答日期。"},
        ])
        result = stream(client, token, body)
        print("text:", result["text"][:150])
        assert result["status"] == 200, result

        print("\n=== 5. read_url ===")
        body = dict(base_body, tools=[
            {"type": "deepseek_read_url", "name": "deepseek_read_url"},
        ], messages=[{
            "role": "user",
            "content": "阅读 https://example.com 并告诉我这个页面的标题是什么。",
        }])
        result = stream(client, token, body)
        print("blocks:", result["blocks"])
        print("text:", result["text"][:200])
        assert result["status"] == 200, result

        print("\n=== 6. client read_link history is accepted ===")
        body = dict(base_body, messages=[
            {"role": "user", "content": "总结一下 https://example.com 的内容。"},
            {"role": "assistant", "content": [
                {"type": "server_tool_use", "id": "srvtoolu_rl_00_x", "name": "deepseek_read_url",
                 "input": {"action": "open_page", "url": "https://example.com"},
                 "caller": {"type": "direct"}, "kind": "read_link"},
                {"type": "web_fetch_tool_result", "tool_use_id": "srvtoolu_rl_00_x",
                 "kind": "read_link",
                 "content": {"type": "web_fetch_result", "url": "https://example.com",
                             "content": {"type": "document",
                                         "source": {"type": "text", "media_type": "text/plain",
                                                    "data": "Example Domain page text"},
                                         "title": "Example Domain"},
                             "published_at": None}},
                {"type": "text", "text": "这是 Example Domain 页面。"},
            ]},
            {"role": "user", "content": "谢谢"},
        ])
        result = stream(client, token, body)
        print("text:", result["text"][:120])
        assert result["status"] == 200, result

        print("\n=== 7. unknown/extra blocks are ignored, not fatal ===")
        body = dict(base_body, messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "说\"好的\"两个字。"},
                {"type": "some_future_block", "payload": {"x": 1}},
            ],
        }])
        result = stream(client, token, body)
        print("text:", result["text"][:80])
        assert result["status"] == 200, result
    print("\nALL FEATURE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
