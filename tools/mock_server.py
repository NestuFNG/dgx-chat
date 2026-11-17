"""Minimal DeepSeek-compatible mock, for verifying the FRONTEND early.

This is NOT the product backend (that is dgxchat/server). It exists so the
Lead can prove in a real browser that the modified chat-demo:
  - resolves the runtime API origin
  - speaks the Anthropic SSE protocol we require
  - streams from the real DGX gateway
before the production backend is finished.

Implements: GET /models, POST /anthropic/v1/messages, POST /v1/tools/tts,
POST /v1/tools/asr, and static hosting of app/dist. No auth, no persistence.

Run:  python mock_server.py [--port 8440] [--dist <dir>]

Environment:
  DGX_BASE_URL       OpenAI-compatible inference endpoint (default localhost:8041)
  DGX_MODEL          upstream model id
  DEEPSEEK_API_KEY   only for the TTS/ASR passthrough (optional)
"""
import argparse
import json
import os
import re
import sys
import uuid
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Point these at your own gateway / official key via the environment:
#   DGX_BASE_URL  OpenAI-compatible inference endpoint (default: localhost)
#   DGX_MODEL     upstream model id
#   DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL  only needed for the TTS/ASR proxy
DGX = os.environ.get("DGX_BASE_URL", "http://localhost:8041/v1")
DGX_MODEL = os.environ.get("DGX_MODEL", "deepseek-v4.1-flash-official")
DS_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DS_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

DIST = ""
MIME = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".map": "application/json", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock] %s %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "*")

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.cors()
        self.end_headers()

    def sse(self, name, obj):
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(("event: %s\ndata: %s\n\n" % (name, data)).encode())
        self.wfile.flush()

    # ---------- routes ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/models":
            return self.send_json(200, {"object": "list", "data": [
                {"id": "deepseek-flash", "object": "model", "created": 0, "owned_by": "dgx"},
                {"id": "deepseek-v4-pro", "object": "model", "created": 0, "owned_by": "dgx"},
            ]})
        return self.serve_static(p)

    def do_DELETE(self):
        self.send_json(200, {"id": "x", "type": "file_deleted"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/anthropic/v1/messages":
            return self.messages()
        if p == "/v1/tools/tts":
            return self.tts()
        if p == "/v1/tools/asr":
            return self.asr()
        if p.startswith("/anthropic/v1/files"):
            return self.send_json(200, {"id": "file-mock", "type": "file", "size_bytes": 1,
                                        "created_at": "2026-01-01T00:00:00Z",
                                        "filename": "mock", "mime_type": "text/plain"})
        return self.send_json(404, {"error": {"message": "no route " + p}})

    def serve_static(self, p):
        if not DIST or not os.path.isdir(DIST):
            return self.send_json(404, {"error": {"message": "no dist"}})
        rel = p.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(DIST, rel))
        if not full.startswith(os.path.abspath(DIST)):
            return self.send_json(403, {"error": {"message": "forbidden"}})
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            full = os.path.join(DIST, "index.html")
        with open(full, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.cors()
        self.end_headers()
        self.wfile.write(data)

    # ---------- anthropic -> openai ----------
    def messages(self):
        req = json.loads(self.read_body().decode() or "{}")
        system = req.get("system") or ""
        thinking = (req.get("thinking") or {}).get("type") == "enabled"
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        for m in req.get("messages") or []:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, list):
                text = "".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
            else:
                text = content or ""
            if role == "latest_reminder":
                msgs.append({"role": "user", "content": "[reminder] " + text})
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": text})
        upstream = {"model": DGX_MODEL, "messages": msgs, "stream": True,
                    "max_tokens": 3000,
                    "stream_options": {"include_usage": True}}
        if not thinking:
            upstream["thinking"] = {"type": "disabled"}
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                DGX + "/chat/completions", data=json.dumps(upstream).encode(),
                headers={"content-type": "application/json"}), timeout=300)
        except urllib.error.HTTPError as e:
            return self.send_json(e.code, {"error": {"message": e.read().decode()[:500]}})

        self.sse_headers()
        mid = str(uuid.uuid4())
        self.sse("message_start", {"type": "message_start", "message": {
            "id": mid, "model": req.get("model"), "usage": {
                "input_tokens": 0, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}}})
        state = {"block": None, "index": -1, "text": [], "usage": {}}

        def close_block():
            if state["block"] is not None:
                self.sse("content_block_stop", {"type": "content_block_stop", "index": state["index"]})
                state["block"] = None

        def open_block(kind):
            close_block()
            state["index"] += 1
            state["block"] = kind
            cb = {"type": "thinking", "thinking": "", "signature": ""} if kind == "thinking" else {"type": "text", "text": ""}
            self.sse("content_block_start", {"type": "content_block_start", "index": state["index"], "content_block": cb})

        stop_reason = "end_turn"
        try:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if obj.get("usage"):
                    state["usage"] = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if ch.get("finish_reason"):
                        stop_reason = "max_tokens" if ch["finish_reason"] == "length" else "end_turn"
                    if d.get("reasoning"):
                        if state["block"] != "thinking":
                            open_block("thinking")
                        self.sse("content_block_delta", {"type": "content_block_delta", "index": state["index"],
                                                         "delta": {"type": "thinking_delta", "thinking": d["reasoning"]}})
                    if d.get("content"):
                        if state["block"] != "text":
                            open_block("text")
                        state["text"].append(d["content"])
                        self.sse("content_block_delta", {"type": "content_block_delta", "index": state["index"],
                                                         "delta": {"type": "text_delta", "text": d["content"]}})
        except Exception as e:
            self.sse("error", {"type": "error", "error": {"message": "upstream: %r" % (e,)}})
        close_block()
        u = state["usage"]
        self.sse("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                                   "usage": {"input_tokens": u.get("prompt_tokens", 0),
                                             "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": 0,
                                             "output_tokens": u.get("completion_tokens", 0)}})
        self.sse("message_stop", {"type": "message_stop"})
        sys.stderr.write("[mock] answered: %r\n" % "".join(state["text"])[:120])

    def _proxy_binary(self, path, data, ctype):
        if not DS_KEY:
            return self.send_json(
                503, {"error": {"message": "DEEPSEEK_API_KEY is not set; "
                                           "the TTS/ASR proxy is disabled"}})
        try:
            req = urllib.request.Request(DS_BASE + path, data=data,
                                         headers={"Authorization": "Bearer " + DS_KEY,
                                                  "content-type": ctype})
            r = urllib.request.urlopen(req, timeout=120)
            payload = r.read()
            self.send_response(200)
            self.send_header("Content-Type", r.headers.get("content-type", "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.cors()
            self.end_headers()
            self.wfile.write(payload)
        except urllib.error.HTTPError as e:
            self.send_json(e.code, {"error": {"message": e.read().decode()[:400]}})
        except Exception as e:
            self.send_json(500, {"error": {"message": repr(e)}})

    def tts(self):
        body = self.read_body()
        self._proxy_binary("/v1/tools/tts", body, "application/json")

    def asr(self):
        body = self.read_body()
        self._proxy_binary("/v1/tools/asr", body, self.headers.get("content-type", "multipart/form-data"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8440)
    ap.add_argument("--dist", default=os.path.join(os.path.dirname(__file__), "..", "app", "dist"))
    a = ap.parse_args()
    DIST = os.path.abspath(a.dist)
    print("mock server on http://127.0.0.1:%d  (dist=%s)" % (a.port, DIST))
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
