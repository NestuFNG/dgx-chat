"""Search / read_url tests, including validators ported from parseEvent.ts.

The validators below mirror ``chat-demo/src/services/api/parseEvent.ts``:
``readBlock`` decides whether the client keeps a block, so anything that fails
here is silently dropped by the real app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.search import (
    FetchedPage,
    SearchPlan,
    SearchService,
    Source,
    extract_urls,
    html_to_text,
    parse_page_age,
    site_icon_for,
    synth_search_blocks,
    truncate,
)

CAPTURE = Path(__file__).resolve().parents[2] / "tools" / "probe_search.sse"


# ---------------------------------------------------------------------------
# validators ported from parseEvent.ts
# ---------------------------------------------------------------------------


def _is_str(value) -> bool:
    return isinstance(value, str)


def _nullable_str(value) -> bool:
    return value is None or isinstance(value, str)


SERVER_TOOL_NAMES = {"deepseek_web_search", "deepseek_read_url"}
SERVER_TOOL_KINDS = {"search", "deep_search", "read_link", "image_search", "deep_image_search"}
STOP_REASONS = {"end_turn", "max_tokens", "tool_use", "refusal"}


def valid_block(block) -> bool:
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind == "text":
        return _is_str(block.get("text"))
    if kind == "thinking":
        return _is_str(block.get("thinking"))
    if kind == "server_tool_use":
        tool_input = block.get("input")
        if not (
            _is_str(block.get("id"))
            and block.get("kind") in SERVER_TOOL_KINDS
            and block.get("name") in SERVER_TOOL_NAMES
            and isinstance(tool_input, dict)
        ):
            return False
        action = tool_input.get("action")
        if action == "search":
            queries = tool_input.get("queries")
            return isinstance(queries, list) and all(_is_str(q) for q in queries)
        if action in ("open_page", "read_link"):
            return _is_str(tool_input.get("url"))
        if action == "find_in_page":
            return _is_str(tool_input.get("url")) and _is_str(tool_input.get("pattern"))
        return False
    if kind == "web_search_tool_result":
        if not (
            _is_str(block.get("tool_use_id"))
            and block.get("kind") in {"search", "deep_search", "image_search", "deep_image_search"}
            and isinstance(block.get("content"), list)
        ):
            return False
        for item in block["content"]:
            if not isinstance(item, dict):
                return False
            if item.get("type") == "web_search_tool_result_error":
                if not _is_str(item.get("error_code")):
                    return False
                continue
            if not (
                item.get("type") == "web_search_result"
                and _is_str(item.get("url"))
                and _is_str(item.get("title"))
                and _nullable_str(item.get("snippet"))
                and _nullable_str(item.get("site_name"))
                and _nullable_str(item.get("site_icon"))
                and _nullable_str(item.get("page_age"))
            ):
                return False
        return True
    if kind == "web_fetch_tool_result":
        content = block.get("content")
        if not (
            _is_str(block.get("tool_use_id"))
            and block.get("kind") in {"read_link", "deep_search"}
            and isinstance(content, dict)
        ):
            return False
        if content.get("type") == "web_fetch_tool_result_error":
            return _is_str(content.get("error_code"))
        return (
            content.get("type") == "web_fetch_result"
            and _is_str(content.get("url"))
            and isinstance(content.get("content"), dict)
            and _nullable_str(content["content"].get("title"))
            and _nullable_str(content.get("published_at"))
        )
    return False


def valid_citation(citation) -> bool:
    return (
        isinstance(citation, dict)
        and citation.get("type") == "web_search_result_location"
        and _is_str(citation.get("url"))
        and _nullable_str(citation.get("title"))
    )


# ---------------------------------------------------------------------------
# block builders
# ---------------------------------------------------------------------------


def test_synth_search_blocks_pass_client_validators():
    sources = [
        Source(
            title="标题一",
            url="https://example.com/1",
            snippet="摘要",
            site_name="example.com",
            site_icon="https://cdn.deepseek.com/site-icons/example.com",
            page_age="2026-11-10T16:00:00+00:00",
        ),
        Source(title="标题二", url="https://example.com/2", snippet="", page_age=None),
    ]
    tool, result = synth_search_blocks(["查询一", "查询二"], sources)
    assert valid_block(tool), tool
    assert valid_block(result), result
    assert tool["id"].startswith("srvtoolu_s_00_")
    assert len(tool["id"].split("_")[-1]) == 32
    assert tool["input"] == {"action": "search", "queries": ["查询一", "查询二"]}
    assert tool["caller"] == {"type": "direct"}
    assert result["tool_use_id"] == tool["id"]
    assert valid_citation(
        {
            "type": "web_search_result_location",
            "url": sources[0].url,
            "title": sources[0].title,
            "cited_text": "文本",
            "encrypted_index": "",
        }
    )


def test_bad_block_is_rejected_by_validator():
    assert not valid_block({"type": "server_tool_use", "id": 1, "kind": "search",
                            "name": "deepseek_web_search", "input": {"action": "search",
                                                                     "queries": []}})
    assert not valid_block({"type": "web_search_tool_result", "tool_use_id": "x",
                            "kind": "search",
                            "content": [{"type": "web_search_result", "url": 5,
                                         "title": "t"}]})
    assert not valid_block({"type": "web_fetch_tool_result", "tool_use_id": "x",
                            "kind": "read_link", "content": {"type": "unknown"}})


# ---------------------------------------------------------------------------
# captured official frames
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not CAPTURE.is_file(), reason="captured official SSE not available")
def test_captured_official_blocks_pass_validators():
    blocks = []
    for line in CAPTURE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("data: "):
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "content_block_start":
            blocks.append(payload["content_block"])
    assert blocks, "no content blocks found in capture"
    for block in blocks:
        assert valid_block(block), block


@pytest.mark.skipif(not CAPTURE.is_file(), reason="captured official SSE not available")
def test_read_url_blocks_pass_validators(monkeypatch):
    import asyncio

    server_settings = type(
        "S",
        (),
        {"search": type("C", (), {"max_pages_per_turn": 3, "fetch_timeout": 2.0})()},
    )()
    service = SearchService.__new__(SearchService)
    service.settings = server_settings

    async def fake_fetch(self, url, config):
        if url.endswith("bad.example"):
            return FetchedPage(url=url, error="url_not_accessible")
        return FetchedPage(url=url, title="Example Domain", text="Example body")

    monkeypatch.setattr(SearchService, "_fetch_page", fake_fetch)

    class Cfg:
        fetch_max_chars = 8000
        fetch_timeout = 2.0

    async def run():
        return await SearchService._read_urls(
            service, ["https://example.com", "https://bad.example"], Cfg()
        )

    blocks, returned, fetches = asyncio.run(run())
    assert fetches == 1
    for block in blocks:
        assert valid_block(block), block
    types = [block["type"] for block in blocks]
    assert types == [
        "server_tool_use",
        "web_fetch_tool_result",
        "server_tool_use",
        "web_fetch_tool_result",
    ]
    error_content = blocks[3]["content"]
    assert error_content == {
        "type": "web_fetch_tool_result_error",
        "error_code": "url_not_accessible",
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_extract_urls():
    text = "看 https://a.example/x 和 http://b.example/y，还有 https://a.example/x。"
    assert extract_urls(text, 3) == ["https://a.example/x", "http://b.example/y"]


def test_html_to_text_strips_scripts_styles():
    html = (
        "<html><head><title>标题</title>"
        "<meta property='article:published_time' content='2026-11-01T10:00:00Z'>"
        "<style>body{color:red}</style></head>"
        "<body><script>alert(1)</script><p>正文 &amp; 内容</p><div>第二段</div></body></html>"
    )
    text, title, published = html_to_text(html)
    assert title == "标题"
    assert published == "2026-11-01T10:00:00Z"
    assert "alert" not in text and "color:red" not in text
    assert "正文 & 内容" in text
    assert "第二段" in text


def test_site_icon():
    assert site_icon_for("https://www.askci.com/news/1") == "https://cdn.deepseek.com/site-icons/www.askci.com"
    assert site_icon_for("not a url") is None


def test_parse_page_age():
    assert parse_page_age("2026-11-10T16:00:00+00:00") == "2026-11-10T16:00:00+00:00"
    assert parse_page_age("2026-11-10") == "2026-11-10T00:00:00+00:00"
    assert parse_page_age("昨天") is None
    assert parse_page_age(None) is None


def test_truncate():
    assert truncate("abcdef", 3) == "abc…"
    assert truncate("ab", 3) == "ab"


@pytest.mark.asyncio
async def test_plan_unavailable_without_key(settings):
    settings.search.mode = "dgx"
    settings.search.provider = "deepseek"
    settings.search.official_api_key = ""
    service = SearchService.__new__(SearchService)
    service.settings = settings

    class Upstream:
        pass

    service.upstream = Upstream()
    available, reason = service.available(None)
    assert available is False
    assert reason == "unavailable"


@pytest.mark.asyncio
async def test_plan_off_mode(settings):
    settings.search.mode = "off"
    service = SearchService.__new__(SearchService)
    service.settings = settings
    service.upstream = object()
    plan = await service.plan(query="q", urls=[], want_search=True, want_read_url=False)
    assert plan.notice == "off"
    assert plan.blocks == []


@pytest.mark.asyncio
async def test_plan_with_stub_provider(settings, monkeypatch):
    settings.search.mode = "dgx"
    settings.search.provider = "brave"
    settings.search.provider_api_key = "k"
    settings.search.provider_base_url = "https://api.search.brave.com/res/v1/web/search"
    settings.search.max_results = 2
    service = SearchService.__new__(SearchService)
    service.settings = settings
    service.upstream = object()

    class FakeProvider:
        enabled = True

        async def search(self, query, limit):
            return [Source(title="A", url="https://a.example", snippet="aa")][:limit]

    monkeypatch.setattr(service, "_provider", lambda provider, config: FakeProvider())
    plan = await service.plan(
        query="问题", urls=[], want_search=True, want_read_url=False
    )
    assert plan.notice == "brave"
    assert len(plan.blocks) == 2
    assert valid_block(plan.blocks[0]) and valid_block(plan.blocks[1])
    assert "[1] A" in plan.context_text
    assert "https://a.example" in plan.context_text


@pytest.mark.asyncio
async def test_plan_official_retrieval_replays_captured_blocks(settings, monkeypatch):
    """The retrieval path must forward the official blocks verbatim."""
    settings.search.mode = "dgx"
    settings.search.provider = "deepseek"
    settings.search.official_api_key = "sk-test"
    service = SearchService.__new__(SearchService)
    service.settings = settings

    captured_blocks = []
    if CAPTURE.is_file():
        for line in CAPTURE.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "content_block_start":
                captured_blocks.append(payload["content_block"])
    if not captured_blocks:
        pytest.skip("no capture available")

    class FakeUpstream:
        def iter_events(self, payload, api_key):
            async def gen():
                for block in captured_blocks:
                    yield "content_block_start", {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": block,
                    }
                    if block.get("type") == "web_search_tool_result":
                        return

            return gen()

    service.upstream = FakeUpstream()
    plan = await service.plan(query="新能源汽车销量", urls=[], want_search=True, want_read_url=False)
    assert plan.notice == "official_retrieval"
    assert plan.web_search_requests == 1
    assert plan.sources, "sources should be extracted from the captured block"
    assert plan.blocks[0]["type"] == "server_tool_use"
    assert plan.blocks[1]["type"] == "web_search_tool_result"
    assert "[1]" in plan.context_text


@pytest.mark.asyncio
async def test_plan_read_url_uses_mocked_fetch(settings, monkeypatch):
    settings.search.mode = "dgx"
    settings.search.provider = "deepseek"
    settings.search.official_api_key = "sk-test"
    service = SearchService.__new__(SearchService)
    service.settings = settings
    service.upstream = object()

    async def fake_fetch(self, url, config):
        return FetchedPage(url=url, title="Example Domain", text="Example body text")

    monkeypatch.setattr(SearchService, "_fetch_page", fake_fetch)
    plan = await service.plan(
        query="看 https://example.com",
        urls=["https://example.com"],
        want_search=False,
        want_read_url=True,
    )
    assert plan.web_fetch_requests == 1
    assert [block["type"] for block in plan.blocks] == [
        "server_tool_use",
        "web_fetch_tool_result",
    ]
    assert valid_block(plan.blocks[0]) and valid_block(plan.blocks[1])
    assert "Example body text" in plan.context_text


def test_search_plan_dataclass_defaults():
    plan = SearchPlan()
    assert plan.blocks == [] and plan.notice == "off"


def test_citation_marker_regex():
    from app.anthropic_compat import _CITATION_RE, _PARTIAL_CITATION_RE

    assert _CITATION_RE.findall("a[1]b[22]c") == ["1", "22"]
    assert _PARTIAL_CITATION_RE.search("tail [") is not None
    assert _PARTIAL_CITATION_RE.search("tail [1") is not None
    assert _PARTIAL_CITATION_RE.search("tail text") is None
