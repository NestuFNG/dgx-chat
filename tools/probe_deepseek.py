"""Probe the official DeepSeek Anthropic-compatible endpoint and record raw SSE.

Usage:
    DEEPSEEK_API_KEY=... python probe_deepseek.py search
    DEEPSEEK_API_KEY=... python probe_deepseek.py plain

The key is read from DEEPSEEK_API_KEY only -- there is no built-in default.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("DEEPSEEK_ANTHROPIC_BASE_URL",
                      "https://api.deepseek.com/anthropic/v1")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
SYSTEM = "该助手为DeepSeek，由深度求索公司创造。"


def key():
    value = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not value:
        sys.exit("probe_deepseek: DEEPSEEK_API_KEY is not set (get one at "
                 "https://platform.deepseek.com/ then export it).")
    return value


def post(path, body, stream=True):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={
            "x-api-key": key(),
            "content-type": "application/json",
            "accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=180)


def dump_stream(resp, out_path):
    lines = []
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace")
        lines.append(text)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return lines


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "search":
        body = {
            "model": MODEL,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "2026年10月中国新能源汽车销量排行前三是什么？请搜索。"}],
            "thinking": {"type": "disabled"},
            "tools": [{"type": "deepseek_web_search", "name": "deepseek_web_search"}],
            "max_tokens": 800,
            "stream": True,
        }
        resp = post("/messages", body)
        lines = dump_stream(resp, "probe_search.sse")
        print("lines:", len(lines))
        for line in lines[:40]:
            print(line.rstrip()[:400])
    elif mode == "plain":
        body = {
            "model": MODEL,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": "说一句 OK"}],
            "thinking": {"type": "disabled"},
            "tools": [],
            "max_tokens": 50,
            "stream": True,
        }
        resp = post("/messages", body)
        lines = dump_stream(resp, "probe_plain.sse")
        print("lines:", len(lines))
        for line in lines:
            print(line.rstrip()[:300])
    else:
        sys.exit("usage: probe_deepseek.py search|plain")
