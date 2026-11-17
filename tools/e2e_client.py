"""End-to-end acceptance client for the DGX Chat backend.

This deliberately re-implements the *frontend's* wire protocol by hand
(copied from chat-demo/src/services/api/*.ts) so that a passing run proves the
real app will work -- it does not reuse any of the backend's own test helpers.

Usage:
    python e2e_client.py --base http://localhost:8439 --user lead --password '...'  [--all]
    python e2e_client.py --base ... --skip-search

The password can also come from $E2E_PASSWORD. Exit code 0 only if every
enabled check passes.
"""
import argparse
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

SYSTEM_PROMPT = "该助手为DeepSeek，由深度求索公司创造。"  # must be byte-identical to official

PASS, FAIL, SKIP = [], [], []


def record(ok, name, detail=""):
    (PASS if ok else FAIL).append((name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  :: " + str(detail)[:400]) if detail else ""))


def req(base, path, method="GET", body=None, headers=None, raw=False, timeout=180):
    url = base.rstrip("/") + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("content-type", "application/json")
        elif isinstance(body, bytes):
            data = body
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        payload = resp.read()
        return resp.status, dict(resp.headers), (payload if raw else payload.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        payload = e.read()
        return e.code, dict(e.headers), (payload if raw else payload.decode("utf-8", "replace"))


def stream_events(base, path, body, headers, timeout=300):
    """Yield (event_name, data_dict_or_None, raw_line) from an SSE response."""
    url = base.rstrip("/") + path
    data = json.dumps(body).encode()
    hdrs = dict(headers)
    hdrs["content-type"] = "application/json"
    hdrs["accept"] = "text/event-stream"
    r = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    resp = urllib.request.urlopen(r, timeout=timeout)
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" not in ctype:
        raise AssertionError("expected text/event-stream, got %r" % ctype)
    name, buf = None, []
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if buf:
                raw = "\n".join(buf)
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = None
                yield name, payload, raw
            name, buf = None, []
            continue
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            name = text[6:].strip()
        elif text.startswith("data:"):
            buf.append(text[5:].lstrip())
    if buf:
        raw = "\n".join(buf)
        try:
            payload = json.loads(raw)
        except Exception:
            payload = None
        yield name, payload, raw


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def check_health(base):
    st, h, body = req(base, "/health")
    record(st == 200, "health endpoint", "%s %s" % (st, body[:120]))


def check_auth(base, user, pwd):
    st, h, body = req(base, "/auth/register", "POST", {"username": user, "password": pwd})
    if st in (200, 201):
        record(True, "register new user", st)
        tok = json.loads(body).get("token")
    elif st == 409:
        record(True, "register existing user -> 409 (expected on re-run)", st)
        tok = None
    else:
        record(False, "register", "%s %s" % (st, body[:300]))
        tok = None
    if not tok:
        st, h, body = req(base, "/auth/login", "POST", {"username": user, "password": pwd})
        record(st == 200, "login", "%s %s" % (st, body[:200]))
        if st != 200:
            return None
        tok = json.loads(body).get("token")
    record(bool(tok), "got token", (tok or "")[:8] + "...")
    st, h, body = req(base, "/auth/me", headers={"Authorization": "Bearer " + tok})
    record(st == 200, "auth/me", body[:200])
    st, h, body = req(base, "/auth/me", headers={"Authorization": "Bearer deadbeef"})
    record(st == 401, "bad token rejected with 401", st)
    return tok


def check_models(base, tok):
    st, h, body = req(base, "/models", headers={"Authorization": "Bearer " + tok})
    ok = st == 200
    ids = []
    if ok:
        try:
            ids = [m.get("id") for m in json.loads(body).get("data", [])]
        except Exception:
            ok = False
    record(ok, "/models returns OpenAI-style list", ids)


def _assert_anthropic_stream(events, label, require_text=True, require_thinking=False):
    seen = [e[0] for e in events]
    idx = 0
    ok_seq = True
    detail = []
    # message_start must be first meaningful event
    while idx < len(events) and events[idx][0] in (None, "ping"):
        idx += 1
    if idx >= len(events) or events[idx][0] != "message_start":
        ok_seq = False
        detail.append("first event is %r not message_start" % (events[idx][0] if idx < len(events) else None))
    if "message_stop" not in seen:
        ok_seq = False
        detail.append("missing message_stop")
    if "message_delta" not in seen:
        ok_seq = False
        detail.append("missing message_delta")
    # content_block_start/stop must pair up
    opens = {}
    for name, payload, raw in events:
        if name == "content_block_start" and payload:
            opens[payload.get("index")] = payload.get("content_block", {}).get("type")
        elif name == "content_block_stop" and payload:
            opens.pop(payload.get("index"), None)
    if opens:
        ok_seq = False
        detail.append("unclosed blocks: %r" % opens)
    text = []
    for name, payload, raw in events:
        if name == "content_block_delta" and payload:
            d = payload.get("delta") or {}
            if d.get("type") == "text_delta":
                text.append(d.get("text", ""))
    joined = "".join(text)
    if require_text and not joined.strip():
        ok_seq = False
        detail.append("empty answer text")
    types = set(opens.values()) if opens else set()
    block_types = {p.get("content_block", {}).get("type") for n, p, r in events
                   if n == "content_block_start" and p}
    if require_thinking and "thinking" not in block_types:
        ok_seq = False
        detail.append("expected thinking block, got %r" % block_types)
    record(ok_seq, "%s: SSE event sequence valid" % label, "; ".join(detail))
    print("        block types: %s | answer[:80]=%r" % (sorted(t for t in block_types if t), joined[:80]))
    return joined, block_types


def anthropic_body(model, messages, thinking=False, tools=None, system=SYSTEM_PROMPT, stream=True):
    body = {
        "model": model,
        "system": system,
        "messages": messages,
        "thinking": {"type": "enabled" if thinking else "disabled"},
        "tools": tools or [],
        "safety_check": {"level": "high"},
    }
    if stream:
        body["stream"] = True
    return body


def check_messages(base, tok):
    h = {"Authorization": "Bearer " + tok}
    label = "messages(no thinking)"
    events = list(stream_events(base, "/anthropic/v1/messages",
                                anthropic_body("deepseek-flash", [{"role": "user", "content": "说一句：你好，DGX。"}]),
                                h))
    text, types = _assert_anthropic_stream(events, label, require_text=True)
    record("thinking" not in types, "thinking disabled -> no thinking block", types)

    label = "messages(thinking on)"
    events = list(stream_events(base, "/anthropic/v1/messages",
                                anthropic_body("deepseek-flash", [{"role": "user", "content": "12*12 等于多少？先想再答。"}],
                                               thinking=True),
                                h))
    _assert_anthropic_stream(events, label, require_text=True, require_thinking=True)

    # latest_reminder role must be accepted
    label = "messages(latest_reminder role)"
    msgs = [{"role": "latest_reminder", "content": "现在是 2026 年 11 月。"},
            {"role": "user", "content": "现在是哪一年？只回答年份。"}]
    events = list(stream_events(base, "/anthropic/v1/messages", anthropic_body("deepseek-flash", msgs), h))
    _assert_anthropic_stream(events, label)

    # multi-turn history
    label = "messages(multi-turn history)"
    msgs = [{"role": "user", "content": "我叫小明。"},
            {"role": "assistant", "content": [{"type": "text", "text": "你好小明。"}]},
            {"role": "user", "content": "我叫什么？"}]
    events = list(stream_events(base, "/anthropic/v1/messages", anthropic_body("deepseek-flash", msgs), h))
    text, _ = _assert_anthropic_stream(events, label)
    record("小明" in text, "history retained across turns", text[:80])


def check_title(base, tok):
    h = {"Authorization": "Bearer " + tok}
    body = anthropic_body("deepseek-flash", [{"role": "user", "content": "帮我解释一下量子纠缠是什么，要通俗。"}])
    body["title"] = {"enabled": True}
    events = list(stream_events(base, "/anthropic/v1/messages", body, h))
    title = None
    for name, payload, raw in events:
        if name == "message_delta" and payload:
            title = (payload.get("delta") or {}).get("title")
    record(bool(title), "title generated in message_delta.delta.title", title)


def check_files(base, tok):
    h = {"Authorization": "Bearer " + tok}
    boundary = "----dgxchat" + uuid.uuid4().hex
    content = b"# hello\nDGX Chat attachment test\n"
    parts = []
    parts.append(("--%s\r\n" % boundary).encode())
    parts.append(b'Content-Disposition: form-data; name="purpose"\r\n\r\nuser_data\r\n')
    parts.append(("--%s\r\n" % boundary).encode())
    parts.append(b'Content-Disposition: form-data; name="file"; filename="note.md"\r\n')
    parts.append(b"Content-Type: text/markdown\r\n\r\n")
    parts.append(content)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    data = b"".join(parts)
    hh = dict(h)
    hh["content-type"] = "multipart/form-data; boundary=" + boundary
    hh["anthropic-beta"] = "files-api-2025-04-14"
    st, hdr, body = req(base, "/anthropic/v1/files", "POST", data, hh)
    ok = st == 200
    fid = None
    if ok:
        try:
            obj = json.loads(body)
            fid = obj.get("id")
            ok = bool(fid) and obj.get("type") == "file" and isinstance(obj.get("size_bytes"), int)
        except Exception:
            ok = False
    record(ok, "files upload -> FileObject", "%s %s" % (st, body[:200]))
    if fid:
        st, hdr, body = req(base, "/anthropic/v1/files/" + fid, "DELETE", None, h)
        record(st in (200, 204), "files delete", st)
    return fid


def check_sessions(base, tok):
    h = {"Authorization": "Bearer " + tok}
    sid = "e2e-" + uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    session = {
        "id": sid, "title": "E2E 测试会话", "createdAt": now, "updatedAt": now,
        "messages": [
            {"id": "m1", "role": "user", "content": "hello", "files": [], "createdAt": now},
            {"id": "m2", "role": "assistant", "createdAt": now, "model": "deepseek-flash",
             "prefix": False, "status": "done", "blocks": [{"type": "text", "text": "hi"}]},
        ],
        "rootMessageId": None,
    }
    st, hdr, body = req(base, "/api/sessions/" + sid, "PUT", session, h)
    record(st in (200, 201, 204), "session PUT", "%s %s" % (st, body[:200]))
    st, hdr, body = req(base, "/api/sessions", headers=h)
    ok = st == 200
    listed = []
    if ok:
        described = json.loads(body)
        listed = [s.get("id") for s in (described if isinstance(described, list) else described.get("sessions", []))]
        ok = sid in listed
    record(ok, "session appears in GET /api/sessions", listed[:5])
    st, hdr, body = req(base, "/api/sessions/" + sid, headers=h)
    ok = st == 200
    if ok:
        got = json.loads(body)
        ok = got.get("id") == sid and len(got.get("messages") or []) == 2 and got.get("title") == "E2E 测试会话"
    record(ok, "session round-trips verbatim", body[:200])
    st, hdr, body = req(base, "/api/sessions/" + sid, "DELETE", None, h)
    record(st in (200, 204), "session DELETE", st)
    return sid


def check_isolation(base, tok, tok2=None):
    """A second user must not see the first user's data."""
    if not tok2:
        SKIP.append(("multi-user isolation", "no second token"))
        print("  SKIP  multi-user isolation (no second account)")
        return
    h2 = {"Authorization": "Bearer " + tok2}
    sid = "iso-" + uuid.uuid4().hex[:12]
    now = int(time.time() * 1000)
    h = {"Authorization": "Bearer " + tok}
    req(base, "/api/sessions/" + sid, "PUT",
        {"id": sid, "title": "private", "createdAt": now, "updatedAt": now, "messages": []}, h)
    st, hdr, body = req(base, "/api/sessions/" + sid, headers=h2)
    record(st in (403, 404), "other user cannot read my session", st)
    st, hdr, body = req(base, "/api/sessions", headers=h2)
    ok = True
    try:
        listed = json.loads(body)
        ids = [s.get("id") for s in (listed if isinstance(listed, list) else listed.get("sessions", []))]
        ok = sid not in ids
    except Exception:
        pass
    record(ok, "other user's session list excludes mine", st)
    req(base, "/api/sessions/" + sid, "DELETE", None, h)


def check_search(base, tok):
    """Verify the search tool blocks match the client's strict validator."""
    h = {"Authorization": "Bearer " + tok}
    tools = [{"type": "deepseek_web_search", "name": "deepseek_web_search"},
             {"type": "deepseek_read_url", "name": "deepseek_read_url"}]
    body = anthropic_body("deepseek-flash",
                          [{"role": "user", "content": "2026年11月有什么重要的 AI 新闻？请搜索最新信息。"}],
                          tools=tools)
    events = list(stream_events(base, "/anthropic/v1/messages", body, h, timeout=300))
    seen_tool_use, seen_search_result = [], []
    for name, payload, raw in events:
        if name != "content_block_start" or not payload:
            continue
        cb = payload.get("content_block") or {}
        if cb.get("type") == "server_tool_use":
            seen_tool_use.append(cb)
        elif cb.get("type") == "web_search_tool_result":
            seen_search_result.append(cb)
    record(bool(seen_tool_use), "search: server_tool_use block emitted", len(seen_tool_use))
    record(bool(seen_search_result), "search: web_search_tool_result block emitted", len(seen_search_result))
    if seen_tool_use:
        b = seen_tool_use[0]
        ok = (isinstance(b.get("id"), str) and b.get("id", "").startswith("srvtoolu_")
              and b.get("name") in ("deepseek_web_search", "deepseek_read_url")
              and b.get("kind") in ("search", "deep_search", "read_link", "image_search", "deep_image_search")
              and isinstance(b.get("input"), dict)
              and b["input"].get("action") in ("search", "open_page", "find_in_page", "read_link"))
        record(ok, "search: server_tool_use shape valid", {k: b.get(k) for k in ("id", "name", "kind")})
        if b["input"].get("action") == "search":
            record(isinstance(b["input"].get("queries"), list), "search: input.queries is a list",
                   b["input"].get("queries"))
    if seen_search_result:
        b = seen_search_result[0]
        items = b.get("content") or []
        record(isinstance(b.get("tool_use_id"), str) and isinstance(items, list),
               "search: result block carries tool_use_id + content[]", len(items))
        good = [i for i in items if isinstance(i, dict) and i.get("type") == "web_search_result"
                and isinstance(i.get("url"), str) and isinstance(i.get("title"), str)
                and (i.get("snippet") is None or isinstance(i.get("snippet"), str))
                and (i.get("site_name") is None or isinstance(i.get("site_name"), str))
                and (i.get("site_icon") is None or isinstance(i.get("site_icon"), str))
                and (i.get("page_age") is None or isinstance(i.get("page_age"), str))]
        record(len(good) == len(items) and len(items) > 0,
               "search: every result item satisfies the client validator", "%d/%d ok" % (len(good), len(items)))
        if good:
            print("        sample result: %r" % {k: good[0].get(k) for k in ("title", "url", "site_name")})
    text, _ = _assert_anthropic_stream(events, "search: full turn", require_text=True)


def check_speech(base, tok):
    h = {"Authorization": "Bearer " + tok}
    st, hdr, body = req(base, "/v1/tools/tts", "POST",
                        {"text": "你好，这是 DGX Chat 的语音测试。", "voice_id": "stella",
                         "format": "pcm", "filter_markdown": True}, h, raw=True)
    ok = st == 200 and isinstance(body, bytes) and len(body) > 4000 and len(body) % 2 == 0
    record(ok, "tts returns raw PCM bytes", "status=%s bytes=%s" % (st, len(body) if isinstance(body, bytes) else "?"))
    if not ok:
        return
    pcm = body
    # wrap into WAV and feed back to ASR
    import io
    import wave
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(pcm[: len(pcm) - (len(pcm) % 2)])
    w.close()
    wav = buf.getvalue()
    boundary = "----dgxchat" + uuid.uuid4().hex
    data = b"".join([
        ("--%s\r\n" % boundary).encode(),
        b'Content-Disposition: form-data; name="file"; filename="recording.wav"\r\n',
        b"Content-Type: audio/wav\r\n\r\n", wav,
        ("\r\n--%s--\r\n" % boundary).encode(),
    ])
    hh = dict(h)
    hh["content-type"] = "multipart/form-data; boundary=" + boundary
    st, hdr, body = req(base, "/v1/tools/asr", "POST", data, hh)
    text = ""
    try:
        text = json.loads(body).get("text", "")
    except Exception:
        pass
    record(st == 200 and isinstance(text, str) and text.strip() != "",
           "asr transcribes the tts audio", "%s %r" % (st, text[:80]))


def check_cors(base):
    origin = "https://localhost"
    headers = {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,x-api-key,content-type,anthropic-beta",
    }
    st, hdr, body = req(base, "/anthropic/v1/messages", "OPTIONS", None, headers)
    allow_origin = (hdr.get("access-control-allow-origin") or hdr.get("Access-Control-Allow-Origin") or "")
    allow_headers = (hdr.get("access-control-allow-headers") or hdr.get("Access-Control-Allow-Headers") or "").lower()
    ok = st in (200, 204) and (allow_origin in ("*", origin))
    record(ok, "CORS preflight accepted", "status=%s allow-origin=%r" % (st, allow_origin))
    record(all(x in allow_headers for x in ("authorization", "x-api-key")),
           "CORS allows authorization + x-api-key headers", allow_headers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--user", default="lead_e2e")
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD", "dgxchat-e2e"))
    ap.add_argument("--user2", default=None)
    ap.add_argument("--password2", default=None)
    ap.add_argument("--skip-search", action="store_true")
    ap.add_argument("--skip-speech", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    print("=== DGX Chat end-to-end acceptance ===")
    print("target:", base)
    t0 = time.time()
    try:
        check_health(base)
    except Exception as e:
        print("  FAIL  health endpoint unreachable:", e)
        return 2
    check_cors(base)
    tok = check_auth(base, args.user, args.password)
    if not tok:
        return 2
    tok2 = None
    if args.user2:
        tok2 = check_auth(base, args.user2, args.password2 or args.password)
    checks = [lambda: check_models(base, tok),
              lambda: check_messages(base, tok),
              lambda: check_title(base, tok),
              lambda: check_files(base, tok),
              lambda: check_sessions(base, tok),
              lambda: check_isolation(base, tok, tok2)]
    if not args.skip_search:
        checks.append(lambda: check_search(base, tok))
    if not args.skip_speech:
        checks.append(lambda: check_speech(base, tok))
    for fn in checks:
        try:
            fn()
        except Exception as e:
            record(False, getattr(fn, "__name__", "check"), "EXCEPTION: %r" % (e,))
    dt = time.time() - t0
    print("\n=== summary: %d passed, %d failed, %d skipped in %.1fs ===" % (len(PASS), len(FAIL), len(SKIP), dt))
    for name, detail in FAIL:
        print("  FAILED:", name, "::", str(detail)[:300])
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
