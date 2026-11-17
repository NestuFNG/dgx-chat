"""Ad-hoc speech check: real TTS -> WAV -> real ASR round trip against DeepSeek official.

Usage: python tests\\_manual_speech.py
"""
from __future__ import annotations

import io
import json
import struct
import sys
import time
import wave

import httpx

BASE = "http://127.0.0.1:8439"
SENTENCE = "你好，这是一次语音合成测试。"


def pcm_to_wav(pcm: bytes, rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    with httpx.Client(timeout=180) as client:
        username = f"spch{int(time.time() * 1000) % 1000000}"
        registered = client.post(
            f"{BASE}/auth/register", json={"username": username, "password": "devpass123"}
        )
        registered.raise_for_status()
        token = registered.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        print("=== TTS ===")
        response = client.post(
            f"{BASE}/v1/tools/tts",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "text": f"{SENTENCE}\n\n```python\nprint('should not be read')\n```\n\n"
                        "| a | b |\n|---|---|\n| 1 | 2 |\n\n> 引用不应朗读\n\n$\\frac{1}{2}$",
                "voice_id": "stella",
                "format": "pcm",
                "filter_markdown": True,
            },
        )
        print("status:", response.status_code)
        print("content-type:", response.headers.get("content-type"))
        print("x-headers:", {k: v for k, v in response.headers.items() if k.startswith("x-dgx")})
        if response.status_code != 200:
            print("body:", response.text[:600])
            return 1
        pcm = response.content
        print("pcm bytes:", len(pcm), "duration:", round(len(pcm) / 48000, 2), "s")
        assert pcm, "empty pcm"
        assert len(pcm) % 2 == 0, "s16le must be even length"
        assert len(pcm) > 24000, "audio suspiciously short"

        print("\n=== ASR round trip ===")
        wav = pcm_to_wav(pcm)
        response = client.post(
            f"{BASE}/v1/tools/asr",
            headers=headers,
            files={"file": ("recording.wav", wav, "audio/wav")},
        )
        print("status:", response.status_code)
        print("body:", response.text[:400])
        assert response.status_code == 200, response.text
        text = response.json()["text"]
        print("text:", text)
        normalized = text.replace(" ", "")
        assert "语音合成测试" in normalized or "语音" in normalized, text

        print("\n=== markdown filter unit ===")
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from app.speech import filter_markdown_for_speech

        sample = (
            "# 标题\n\n**粗体**与*斜体*，还有 `inline code`。\n\n"
            "```js\nconsole.log(1)\n```\n\n"
            "| 列1 | 列2 |\n| --- | --- |\n| a | b |\n\n"
            "> 引用行\n\n- 列表项\n\n[链接](https://example.com)\n\n"
            "$$E=mc^2$$ 长公式 $x^2+y^2=z^2$ 之后\n\n---\n"
        )
        filtered = filter_markdown_for_speech(sample)
        print("filtered:", json.dumps(filtered, ensure_ascii=False))
        for banned in ("```", "console.log", "| 列1", "> 引用", "$$", "**", "[链接]", "---"):
            assert banned not in filtered, f"markdown residue: {banned}"
        assert "标题" in filtered and "链接" in filtered
    print("\nSPEECH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
