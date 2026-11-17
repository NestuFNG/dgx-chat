"""Speech tool tests: markdown filtering, PCM handling, providers, quotas."""

from __future__ import annotations

import asyncio
import json

import pytest

import app.speech as speech
from app.models import ApiError
from app.security import add_counter
from app.speech import (
    PCM_MEDIA_TYPE,
    ensure_pcm,
    filter_markdown_for_speech,
    openai_path,
    pcm_duration_bytes,
    sniff_audio,
    tools_path,
)


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None, json_body=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._json = json_body
        self.text = content.decode("utf-8", "replace") if content else ""

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)


class FakeAsyncClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        return self.response


def install_http(monkeypatch, response: FakeResponse) -> FakeAsyncClient:
    holder = FakeAsyncClient(response)

    def factory(**kwargs):  # noqa: ANN003
        holder.kwargs = kwargs
        return holder

    monkeypatch.setattr(speech.httpx, "AsyncClient", factory)
    return holder


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tts_body(text: str = "你好世界", **overrides):
    body = {"text": text, "voice_id": "stella", "format": "pcm", "filter_markdown": True}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# markdown filtering
# ---------------------------------------------------------------------------


def test_markdown_filter_removes_code_tables_quotes_latex():
    text = (
        "# 标题\n\n"
        "**加粗** *斜体* ~~删除~~ `inline`\n\n"
        "```python\nprint('x')\n```\n\n"
        "| 列1 | 列2 |\n| --- | --- |\n| a | b |\n\n"
        "> 引用内容\n\n"
        "- 列表项\n"
        "1. 有序项\n\n"
        "$$E=mc^2$$\n\n"
        "[链接文本](https://example.com)\n"
        "![图片](https://example.com/a.png)\n\n"
        "---\n"
    )
    filtered = filter_markdown_for_speech(text)
    for banned in ("```", "print('x')", "| 列1", "---", "$$", "**", "`", "](http", ">"):
        assert banned not in filtered, banned
    for kept in ("标题", "加粗", "inline", "引用内容", "列表项", "有序项", "链接文本"):
        assert kept in filtered, kept


def test_markdown_filter_empty_input():
    assert filter_markdown_for_speech("") == ""
    assert filter_markdown_for_speech("   \n\n  ") == ""


def test_markdown_filter_collapses_blank_lines():
    filtered = filter_markdown_for_speech("第一行\n\n\n\n\n第二行")
    assert filtered == "第一行\n\n第二行"


# ---------------------------------------------------------------------------
# audio format handling
# ---------------------------------------------------------------------------


def test_sniff_pcm_with_rate_header():
    kind, rate, channels = sniff_audio(
        b"\x00\x01\x02\x03", "audio/pcm; rate=16000; channels=2"
    )
    assert (kind, rate, channels) == ("pcm", 16000, 2)


def test_sniff_wav_mp3_ogg():
    assert sniff_audio(b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")[0] == "wav"
    assert sniff_audio(b"OggS\x00\x02", "application/octet-stream")[0] == "ogg"
    assert sniff_audio(b"ID3\x04", "application/octet-stream")[0] == "mp3"


@pytest.mark.asyncio
async def test_ensure_pcm_passthrough(monkeypatch, settings):
    payload = b"\x00\x01" * 100
    called = False

    async def fake_transcode(*args, **kwargs):
        nonlocal called
        called = True
        return b""

    monkeypatch.setattr(speech, "transcode_to_pcm", fake_transcode)
    result = await ensure_pcm(settings, payload, "audio/pcm; rate=24000; channels=1")
    assert result == payload
    assert called is False


@pytest.mark.asyncio
async def test_ensure_pcm_transcodes_wrong_rate(monkeypatch, settings):
    async def fake_transcode(cfg, data, *, kind, rate, channels):
        assert kind == "pcm"
        assert rate == 16000
        return b"converted"

    monkeypatch.setattr(speech, "transcode_to_pcm", fake_transcode)
    result = await ensure_pcm(settings, b"\x00\x01", "audio/pcm; rate=16000")
    assert result == b"converted"


@pytest.mark.asyncio
async def test_ensure_pcm_wav_without_ffmpeg_is_clear_error(settings):
    settings.speech.ffmpeg_bin = None
    with pytest.raises(ApiError) as excinfo:
        await ensure_pcm(settings, b"RIFF....WAVEfmt ", "audio/wav")
    assert excinfo.value.status_code == 502
    assert "ffmpeg" in excinfo.value.message


@pytest.mark.asyncio
async def test_transcode_command_shape(monkeypatch, settings):
    captured = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, payload):
            captured["payload"] = payload
            return b"\x00\x01" * 10, b""

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        return FakeProc()

    settings.speech.ffmpeg_bin = "ffmpeg"
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await speech.transcode_to_pcm(
        settings, b"RIFF", kind="wav", rate=None, channels=None
    )
    assert result == b"\x00\x01" * 10
    args = captured["args"]
    assert args[0] == "ffmpeg"
    assert "-f" in args and "s16le" in args
    assert "24000" in args
    assert args[-1] == "pipe:1"


def test_provider_paths():
    assert tools_path("https://api.deepseek.com", "tts") == "https://api.deepseek.com/v1/tools/tts"
    assert tools_path("https://api.deepseek.com/v1/", "asr") == "https://api.deepseek.com/v1/tools/asr"
    assert openai_path("https://api.openai.com/v1", "speech") == "https://api.openai.com/v1/audio/speech"
    assert openai_path("http://x:8080", "transcriptions") == "http://x:8080/v1/audio/transcriptions"


def test_pcm_duration_helper():
    assert pcm_duration_bytes(b"\x00" * 48000) == 48000


# ---------------------------------------------------------------------------
# endpoints with a stubbed upstream
# ---------------------------------------------------------------------------


def configure_deepseek(admin_harness):
    settings = admin_harness.app.state.settings
    settings.speech.tts.provider = "deepseek"
    settings.speech.tts.base_url = "https://api.deepseek.com"
    settings.speech.tts.api_key = "sk-test"
    settings.speech.tts.voice_map = {}
    settings.speech.asr.provider = "deepseek"
    settings.speech.asr.base_url = "https://api.deepseek.com"
    settings.speech.asr.api_key = "sk-test"


def test_tts_passthrough_pcm(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    pcm = bytes(range(256)) * 40
    holder = install_http(
        monkeypatch,
        FakeResponse(
            content=pcm,
            headers={
                "content-type": "audio/pcm; rate=24000; encoding=signed-integer; bits=16; channels=1"
            },
        ),
    )
    response = admin_harness.client.post(
        "/v1/tools/tts",
        headers=auth(admin_harness.admin_token),
        json=tts_body("你要读这句话"),
    )
    assert response.status_code == 200, response.text
    assert response.content == pcm  # bit-for-bit passthrough, no transcoding
    assert response.headers["content-type"] == PCM_MEDIA_TYPE
    assert response.headers["x-dgx-tts-provider"] == "deepseek"
    assert response.headers["x-dgx-tts-voice"] == "stella"

    post = holder.posts[0]
    assert post["url"] == "https://api.deepseek.com/v1/tools/tts"
    assert post["json"]["voice_id"] == "stella"
    assert post["json"]["format"] == "pcm"
    assert post["json"]["filter_markdown"] is False  # filtered server-side already
    assert post["headers"]["Authorization"] == "Bearer sk-test"


def test_tts_filters_markdown_before_upstream(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    holder = install_http(
        monkeypatch,
        FakeResponse(content=b"\x00\x01" * 10, headers={"content-type": "audio/pcm"}),
    )
    text = "解释一下：\n```js\nconsole.log(1)\n```\n表格：\n| a | b |\n|---|---|\n| 1 | 2 |\n结论。"
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body(text)
    )
    assert response.status_code == 200
    sent = holder.posts[0]["json"]["text"]
    assert "console.log" not in sent
    assert "| a | b |" not in sent
    assert "解释一下" in sent and "结论" in sent


def test_tts_voice_passthrough_for_official_ids(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    holder = install_http(
        monkeypatch, FakeResponse(content=b"\x00\x01", headers={"content-type": "audio/pcm"})
    )
    admin_harness.client.post(
        "/v1/tools/tts",
        headers=auth(admin_harness.admin_token),
        json=tts_body("嗨", voice_id="tide"),
    )
    assert holder.posts[0]["json"]["voice_id"] == "tide"


def test_tts_openai_provider_requests_pcm_and_transcodes(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.speech.tts.provider = "openai"
    settings.speech.tts.base_url = "https://api.openai.com/v1"
    settings.speech.tts.api_key = "sk-openai"
    settings.speech.tts.model = "gpt-4o-mini-tts"
    settings.speech.tts.voice_map = {"stella": "alloy", "tide": "echo"}
    monkeypatch.setattr(
        speech, "transcode_to_pcm", _fake_transcode("transcoded-pcm")
    )
    holder = install_http(
        monkeypatch,
        FakeResponse(content=b"mp3-bytes", headers={"content-type": "audio/mpeg"}),
    )
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 200
    assert response.content == b"transcoded-pcm"
    post = holder.posts[0]
    assert post["url"] == "https://api.openai.com/v1/audio/speech"
    assert post["json"]["voice"] == "alloy"
    assert post["json"]["response_format"] == "pcm"
    assert post["json"]["model"] == "gpt-4o-mini-tts"


def test_tts_missing_ffmpeg_for_container_audio(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.speech.tts.provider = "openai"
    settings.speech.tts.base_url = "https://api.openai.com/v1"
    settings.speech.ffmpeg_bin = None
    install_http(
        monkeypatch,
        FakeResponse(content=b"ID3....", headers={"content-type": "audio/mpeg"}),
    )
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 502
    assert "ffmpeg" in response.json()["error"]["message"]


def test_tts_provider_disabled(admin_harness):
    admin_harness.app.state.settings.speech.tts.provider = "disabled"
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 503


def test_tts_daily_quota(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    settings = admin_harness.app.state.settings
    settings.daily_tts_chars = 5
    install_http(
        monkeypatch, FakeResponse(content=b"\x00\x01", headers={"content-type": "audio/pcm"})
    )

    async def seed():
        await add_counter(admin_harness.app.state.db, admin_harness.admin_user["id"], "tts_chars", 5)

    # use the portal of TestClient to run inside the app loop
    admin_harness.client.portal.call(seed)
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 429
    assert "配额" in response.json()["error"]["message"]


def test_tts_error_from_upstream(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    install_http(
        monkeypatch,
        FakeResponse(
            status=400,
            content=b'{"error":{"message":"bad voice"}}',
            json_body={"error": {"message": "bad voice"}},
        ),
    )
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 502
    assert "bad voice" in response.json()["error"]["message"]


def test_asr_roundtrip_with_stub(admin_harness, monkeypatch):
    configure_deepseek(admin_harness)
    holder = install_http(
        monkeypatch,
        FakeResponse(
            content=b'{"request_id":"r1","text":"\xe4\xbd\xa0\xe5\xa5\xbd","duration_ms":1000}',
            headers={"content-type": "application/json"},
            json_body={"request_id": "r1", "text": "你好", "duration_ms": 1000},
        ),
    )
    wav = b"RIFF....WAVEfmt "
    response = admin_harness.client.post(
        "/v1/tools/asr",
        headers=auth(admin_harness.admin_token),
        files={"file": ("rec.wav", wav, "audio/wav")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "你好"
    assert response.json()["request_id"] == "r1"
    post = holder.posts[0]
    assert post["url"] == "https://api.deepseek.com/v1/tools/asr"
    assert "file" in post["files"]


def test_asr_requires_file(admin_harness):
    response = admin_harness.client.post(
        "/v1/tools/asr", headers=auth(admin_harness.admin_token)
    )
    assert response.status_code == 400


def test_asr_size_limit(admin_harness):
    admin_harness.app.state.settings.speech.max_asr_bytes = 10
    response = admin_harness.client.post(
        "/v1/tools/asr",
        headers=auth(admin_harness.admin_token),
        files={"file": ("rec.wav", b"x" * 100, "audio/wav")},
    )
    assert response.status_code == 413


def test_asr_provider_disabled(admin_harness):
    admin_harness.app.state.settings.speech.asr.provider = "disabled"
    response = admin_harness.client.post(
        "/v1/tools/asr",
        headers=auth(admin_harness.admin_token),
        files={"file": ("rec.wav", b"RIFF", "audio/wav")},
    )
    assert response.status_code == 503


def test_per_user_tts_override(admin_harness, monkeypatch):
    settings = admin_harness.app.state.settings
    settings.speech.tts.provider = "deepseek"
    settings.speech.tts.base_url = "https://api.deepseek.com"
    settings.speech.tts.api_key = "sk-env"
    admin_harness.client.put(
        "/api/me/settings",
        headers=auth(admin_harness.admin_token),
        json={"tts": {"provider": "deepseek_tools", "base_url": "http://custom:9000",
                      "api_key": "sk-user", "voice": "tide"}},
    )
    holder = install_http(
        monkeypatch, FakeResponse(content=b"\x00\x01", headers={"content-type": "audio/pcm"})
    )
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json=tts_body("你好")
    )
    assert response.status_code == 200
    post = holder.posts[0]
    assert post["url"] == "http://custom:9000/v1/tools/tts"
    assert post["headers"]["Authorization"] == "Bearer sk-user"
    assert post["json"]["voice_id"] == "tide"


def test_tts_request_validation(admin_harness):
    response = admin_harness.client.post(
        "/v1/tools/tts", headers=auth(admin_harness.admin_token), json={"text": ""}
    )
    assert response.status_code == 400


def test_tts_empty_after_markdown_filter(admin_harness):
    response = admin_harness.client.post(
        "/v1/tools/tts",
        headers=auth(admin_harness.admin_token),
        json=tts_body("```\ncode only\n```"),
    )
    assert response.status_code == 400
    assert "可朗读" in response.json()["error"]["message"]


def _fake_transcode(payload: bytes):
    async def fake(settings, data, *, kind, rate, channels):
        fake.calls = getattr(fake, "calls", []) + [(kind, rate, channels, data)]
        return payload

    return fake
