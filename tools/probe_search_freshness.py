"""Diagnose search freshness: does the retrieval query need an explicit date?

Sends the same intent with and without a date to DeepSeek's official
Anthropic-compatible endpoint (tool: deepseek_web_search), aborts as soon as
the web_search_tool_result arrives, and prints the page_age of every source.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE = "https://api.deepseek.com/anthropic/v1"

CASES = [
    ("no date", "最近有什么重要的人工智能新闻？"),
    ("explicit date", None),  # filled below
]


def run(query):
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": query}],
        "thinking": {"type": "disabled"},
        "tools": [{"type": "deepseek_web_search", "name": "deepseek_web_search"}],
        "max_tokens": 64,
        "stream": True,
    }
    req = urllib.request.Request(
        BASE + "/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": KEY, "content-type": "application/json",
                 "accept": "text/event-stream"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=180)
    queries, ages, titles = [], [], []
    try:
        while True:
            line = resp.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data: "):
                continue
            try:
                ev = json.loads(text[6:])
            except Exception:
                continue
            if ev.get("type") != "content_block_start":
                continue
            cb = ev.get("content_block") or {}
            if cb.get("type") == "server_tool_use":
                queries = (cb.get("input") or {}).get("queries") or []
            elif cb.get("type") == "web_search_tool_result":
                for item in cb.get("content") or []:
                    if item.get("type") == "web_search_result":
                        ages.append(item.get("page_age"))
                        titles.append(item.get("title", "")[:40])
                break  # retrieval is done; stop paying for the answer
    finally:
        resp.close()
    return queries, ages, titles


if __name__ == "__main__":
    if not KEY:
        sys.exit("set DEEPSEEK_API_KEY")
    today = datetime.now(timezone.utc).astimezone()
    CASES[1] = ("explicit date",
                "2026年11月17日 今天有什么重要的人工智能新闻？")
    print("server local time:", today.isoformat(timespec="seconds"))
    for name, query in CASES:
        print("\n=== %s ===" % name)
        print("  query sent:", query)
        try:
            queries, ages, titles = run(query)
        except Exception as exc:
            print("  ERROR:", repr(exc))
            continue
        print("  model chose queries:", queries)
        print("  sources: %d" % len(ages))
        known = [a for a in ages if a]
        known.sort(reverse=True)
        print("  newest 5 page_age:", known[:5])
        print("  oldest 3 page_age:", known[-3:] if known else [])
        for t, a in list(zip(titles, ages))[:5]:
            print("    %-44s %s" % (t, a))
