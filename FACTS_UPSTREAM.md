# Upstream API behaviour (measured, 2026-11-17)

> Everything in this file was verified with real requests against a local
> OpenAI-compatible gateway and the official DeepSeek API. Do not guess field
> names — copy them from here.
> Raw captures: `tools/probe_search.sse`, `tools/probe_plain.sse`,
> `tools/tts_probe.pcm`, `tools/tts_roundtrip.wav`
> (these are sample files; the values below are the contract.)

## A. Local inference gateway (the only source of answer text)

- Base: the OpenAI-compatible endpoint you configure in `DGX_BASE_URL`
  (default in this repo: `http://localhost:8041/v1`), **no auth**
- `/models` → `deepseek-v4.1-flash`、`deepseek-v4.1-flash-official`（vLLM 0.29.1rc1，max_model_len=1048576）
- `/chat/completions` 流式：
  - `data: {"choices":[{"delta":{"reasoning":"…"}}]}` ← 思维链
  - `data: {"choices":[{"delta":{"content":"…"}}]}`  ← 正文
  - 末尾 `data: [DONE]`
- **关闭思维链**：`"thinking":{"type":"disabled"}` —— 实测 3/3 次 reasoning 长度为 0。
  反例：`chat_template_kwargs:{thinking:false}`、`chat_template_kwargs:{enable_thinking:false}` 都**无效**（仍产出 reasoning）。
- ⚠️ 非流式请求里加 `stream_options` 会 400。
- 模型映射建议：客户端 `deepseek-flash` / `deepseek-v4-pro` → DGX `deepseek-v4.1-flash-official`

## B. DeepSeek official API (only for search retrieval / ASR / TTS, never for answer text)

Key: read from `DEEPSEEK_API_KEY` — there is no key in this repository.
(Measured with a small prepaid account; keep test calls cheap.)

### B1. Anthropic 兼容 Messages（搜索检索用）
`POST https://api.deepseek.com/anthropic/v1/messages`
Header：`x-api-key: <KEY>`、`content-type: application/json`、`accept: text/event-stream`
Body 里的 `model` 用官方 id：`deepseek-flash` / `deepseek-v4-pro`（**注意与 DGX 的 id 不同**）

实测 SSE（纯文本，`thinking:{type:"disabled"}`）：
```
event: message_start
data: {"type":"message_start","message":{"id":"<uuid>","type":"message","role":"assistant","model":"deepseek-flash","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":22,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0,"service_tier":"standard"}}}
event: content_block_start   data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
event: ping                   data: {"type":"ping"}
event: content_block_delta   data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}
event: content_block_stop    data: {"type":"content_block_stop","index":0}
event: message_delta         data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":22,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":1,"service_tier":"standard"}}
event: message_stop          data: {"type":"message_stop"}
```

### B2. 联网搜索（实测，`tools:[{"type":"deepseek_web_search","name":"deepseek_web_search"}]`）
真实顺序：`server_tool_use` → `web_search_tool_result` → `text`
```json
{"type":"content_block_start","index":0,"content_block":{
  "type":"server_tool_use","id":"srvtoolu_s_00_4c5c0262caf04ca4a3f5e97e5e5ba338",
  "name":"deepseek_web_search",
  "input":{"action":"search","queries":["…","…"]},
  "caller":{"type":"direct"},"kind":"search"}}
```
```json
{"type":"content_block_start","index":1,"content_block":{
  "type":"web_search_tool_result",
  "tool_use_id":"srvtoolu_s_00_4c5c0262caf04ca4a3f5e97e5e5ba338",
  "content":[
    {"type":"web_search_result",
     "title":"…","url":"https://…",
     "encrypted_content":"aBBGM81R405…(base64，客户端不解密)",
     "page_age":"2026-11-10T16:00:00+00:00",
     "snippet":"…",
     "site_name":"中商情报网",
     "site_icon":"https://cdn.deepseek.com/site-icons/askci.com"}
  ],"kind":"search"}}
```
- 单次检索返回 **11 条**结果；`site_icon` 是 `https://cdn.deepseek.com/site-icons/<注册域名>`
- 检索结果块**先于**正文块出现 ⇒ 想要「只检索不问答」，读到 `web_search_tool_result` 后立刻 abort 即可，省 token。

### B3. TTS（实测，完全匹配客户端需求）
`POST https://api.deepseek.com/v1/tools/tts`
Header：`Authorization: Bearer <KEY>`、`content-type: application/json`
Body：`{"text":"…","voice_id":"stella","format":"pcm","filter_markdown":true}`
响应：`HTTP 200`
```
content-type: audio/pcm; rate=24000; encoding=signed-integer; bits=16; channels=1
```
151680 bytes ≈ 3.16 s。**这就是客户端要的裸 PCM，无需转码。**

### B4. ASR（实测）
`POST https://api.deepseek.com/v1/tools/asr`
Header：`Authorization: Bearer <KEY>`，multipart 字段名 **`file`**
响应：`{"request_id":"…","text":"你好，这是一次语音合成测试。","duration_ms":3160}`
- 闭环已验证：TTS 产出的 PCM 包成 WAV 再喂 ASR，文本完全正确。
- 缺 file → 400。

## C. 给后端的结论

1. **正文永远由 DGX 生成**（用户明确要求），官方 key 只做检索/ASR/TTS。
2. 检索链路：向官方端点发「只带 web_search 工具」的请求 → 读到 `web_search_tool_result` 就掐断 →
   把该块**原样**下发给客户端（字段名逐字照抄上面 B2）→ 同时把 title/snippet/url 拼进给 DGX 的上下文。
3. `encrypted_content` 客户端不解密，但**类型必须是 string**（空串会导致校验？实测 fill 的是 base64，转给我们自己合成的结果时给 `""` 即可满足 `isString` 校验 —— parseEvent 只对这些字段做 nullable 检查，`encrypted_content` 没查类型，给什么都不会丢卡片）。
4. TTS/ASR：`provider=deepseek` 时就是**纯代理**，不要二次编码 PCM。
5. 自定义端点：`provider=openai` 走 `/v1/audio/speech` + `/v1/audio/transcriptions`，此时才需要 ffmpeg 转 24k/mono/s16le。
