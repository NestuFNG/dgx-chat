# DGX Chat — 后端服务（server/）

FastAPI 实现的 DeepSeek 兼容网关 + 多用户鉴权 + 云端会话存储。
前端（chat-demo fork / Android APK）走 **Anthropic Messages 兼容协议**，
推理走局域网 DGX 网关（OpenAI 兼容，vLLM），联网检索/语音走 DeepSeek 官方 key。

```
┌──────────────┐   Anthropic Messages SSE    ┌────────────────────┐   OpenAI chat/completions   ┌──────────────┐
│ Android APK  │ ──────────────────────────► │  server/ (本项目)  │ ──────────────────────────► │ DGX 网关     │
│ 浏览器 Web UI│ ◄────────────────────────── │  翻译 + 鉴权 + 存储│ ◄────────────────────────── │ dgx.local    │
└──────────────┘                             └─────────┬──────────┘                             └──────────────┘
                                                       │ 检索 / TTS / ASR（官方 key）
                                                       ▼
                                              https://api.deepseek.com
```

---

## 1. 本地运行

```bash
cd server

# Python 3.12（本机默认 python 是 3.8，太旧）。任选一种方式建虚拟环境：
uv venv .venv --python 3.12
uv pip install -r requirements.txt --python python   # Windows
# uv pip install -r requirements.txt --python .venv/bin/python          # Linux/macOS

# 配置
cp .env.example .env
#   必填：DGX_BASE_URL（你自己的 OpenAI 兼容网关，例如 http://dgx.local:8000/v1）
#   可选：DEEPSEEK_API_KEY（联网检索 / TTS / ASR）

# 启动
python -m uvicorn app.main:app --host 0.0.0.0 --port 8439
# 或
python -m uvicorn app.main:app --host 127.0.0.1 --port 8439 --no-server-header
```

打开 <http://127.0.0.1:8439/> 只会看到一句 `Service is running.`（故意不暴露任何信息）。
第一次使用先注册管理员：

```bash
curl -s http://127.0.0.1:8439/auth/register \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"你的密码"}'
# -> {"token":"<64位hex>","user":{"id":"u_...","username":"admin","role":"admin",...}}
```

**第一个注册的用户自动成为管理员，且不需要邀请码**（bootstrap 例外）。
之后的注册由 `REGISTRATION_MODE` 控制（默认 `invite`），在 <http://127.0.0.1:8439/admin>
页面里点几下就能生成邀请码、建号、重置密码、吊销 token。

> `/admin` 页面是纯静态 HTML + 原生 fetch，可离线使用，不依赖 CDN。
> 页面本身公开可访问，但所有操作都需要管理员 token。

### 1.1 本地测试 / 验证命令

```bash
# 单元测试（141 个，全部离线，不需要上游）
python -m pytest -q

# 真实网关端到端流式验证（会真的请求 DGXCHAT_TEST_UPSTREAM_URL）
export DGXCHAT_TEST_UPSTREAM_URL=http://dgx.local:8000/v1
python tests/live_smoke.py
python tests/live_smoke.py --search --speech   # 追加检索与语音（需 DEEPSEEK_API_KEY）

# 公网网关（不同参数方言）与"主/备端点"验证
export DGXCHAT_TEST_PUBLIC_URL=https://api.example.com/v1
export DGXCHAT_TEST_PUBLIC_KEY=...        # 该网关的 key，没有就留空
python tests/live_smoke.py
python tests/live_dialect_smoke.py
python tests/live_dialect_smoke.py --fallback-url https://api.example.com/v1

# 前端协议兼容性（纯标准库客户端，可用于任何实例）
python ../tools/e2e_client.py --base-url http://127.0.0.1:8439
```

`tests/live_*.py` 与 `tests/_probe_*.py` **不内置任何地址或 key**：缺少对应变量时打印
`[SKIP]`/`SETUP ERROR` 并退出，不会去探测别人的机器。

| 环境变量 | 用途 |
|---|---|
| `DGXCHAT_TEST_UPSTREAM_URL` | 要验证的网关地址（未设置时回退到 `DGX_BASE_URL`） |
| `DGX_API_KEY` | 该网关的 Bearer token（局域网网关通常留空） |
| `DGXCHAT_TEST_PUBLIC_URL` / `DGXCHAT_TEST_PUBLIC_KEY` | 第二个（公网）网关及其 key，用于方言/回退验证 |
| `DGXCHAT_TEST_MODEL` | 探测脚本请求的模型 id |
| `DGX_BASE_URL_FALLBACK` / `DGX_API_KEY_FALLBACK` | 主备切换验证 |
| `DEEPSEEK_API_KEY` | 联网检索 / TTS / ASR（`--search`、`--speech`） |

---

## 2. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | `{username,password,invite_code?}` → `{token,user}`；空库首个注册者=admin |
| POST | `/auth/login` | `{username,password}` → `{token,user}` |
| GET | `/auth/me` | 当前用户 |
| POST | `/auth/logout` | 吊销本次使用的 token |
| GET | `/models` | 客户端模型列表（需鉴权） |
| POST | `/anthropic/v1/messages` | **核心**：Anthropic Messages SSE，正文由 DGX 生成 |
| POST | `/anthropic/v1/files` | multipart 上传（`file`, `purpose?`） |
| GET/DELETE | `/anthropic/v1/files/{id}` | 元数据 / 删除 |
| POST | `/v1/tools/asr` | multipart `file` → `{"text":...}` |
| POST | `/v1/tools/tts` | `{text,voice_id,format,filter_markdown}` → `audio/pcm` 24kHz mono s16le |
| GET/PUT/DELETE | `/api/sessions[/{id}]` | 会话云端同步（Session 原样存取） |
| GET | `/api/sessions/sync?since=<ms>` | 多端增量同步：变更的会话 + 已删除的会话 id 墓碑 |
| GET/PUT | `/api/files/{local_id}` | 客户端本地附件 id 的二进制同步 |
| GET/PUT | `/api/me/settings` | 每用户设置（含**只写**的官方 Key / 语音端点凭证，见 §2.2） |
| GET | `/api/me/capabilities` | 语音/官方 Key/搜索的就绪状态（不泄露任何密钥） |
| GET | `/api/me/tokens` | 我的登录设备/凭证列表（`id/label/createdAt/lastUsedAt/current`，**永不返回 token 本身**） |
| DELETE | `/api/me/tokens/{id}` | 吊销我的某一个凭证 |
| POST | `/api/me/tokens/revoke-others` | 吊销除当前凭证外的全部 |
| DELETE | `/api/me` | 注销账号（body `{password}` 确认；最后一个管理员会被拒绝） |
| GET | `/api/me/stats` | 我的用量：会话/文件/字节/请求/tokens/TTS 字符 + 近 14 天曲线 |
| GET | `/admin/users` | 用户列表（管理员） |
| POST | `/admin/users` | 建用户 |
| DELETE | `/admin/users/{id}` | 删用户（连带会话/文件/token） |
| POST | `/admin/users/{id}/reset-password` | 重置密码（`{password}` 指定或空=随机，返回新密码并吊销旧 token） |
| POST | `/admin/users/{id}/revoke-tokens` | 吊销该用户全部 token |
| POST | `/admin/users/{id}/disable` | 禁用账号（立即吊销其全部 token） |
| POST | `/admin/users/{id}/enable` | 解除禁用 |
| GET/POST | `/admin/invites` | 邀请码列表 / 生成 |
| DELETE | `/admin/invites/{code}` | 吊销邀请码 |
| GET | `/admin/audit?limit=&offset=` | 审计日志（管理员/安全相关操作的完整记录） |
| GET | `/admin/diagnostics` | 上游连通性、DB/磁盘占用、ffmpeg、限流器计数、配置摘要 |
| POST | `/admin/maintenance/cleanup?dry_run=1` | 清理孤儿文件/行/会话（默认 dry-run，`dry_run=0` 才删除） |
| GET | `/admin/stats` | 统计（用户/会话/文件/token 用量/邀请码） |
| GET | `/admin` | 管理页面（HTML） |
| GET | `/health` | 公开：`{"ok":true,"status":"up"}`；管理员额外返回配置摘要 |
| GET | `/version` | `{"version":...}` |

鉴权头：`Authorization: Bearer <token>`、`x-api-key: <token>`、
`Authorization: Bearer <username>:<password>`（方便手填账号密码），三者都接受。

**给前端同事的云同步响应约定**（SPEC §2.3 未写死的部分）：
- `PUT /api/sessions/{id}` 成功返回 `{id,title,createdAt,updatedAt}`；
  若云端已有更新的 `updatedAt`（后写覆盖规则），返回同上再加 `"conflict": true`，**不覆盖**。
- `DELETE /api/sessions/{id}` 返回 `{"ok":true,"id":...,"deleted":true|false}`（幂等）。
- `PUT /api/files/{local_id}` 返回 `{"ok":true,"id","size","contentType","sha256","updatedAt"}`；
  `local_id` 只允许 `[A-Za-z0-9._-]{1,120}`。
- `PUT /api/me/settings` 原样返回保存的 JSON；`GET` 未设置时返回 `{}`。
- 错误统一 `{"error":{"message":"...","type":"...","param":...,"code":...}}`。

### 2.1 每用户凭证：双 Key 模型（官方 Key + 语音端点）

系统里有两个**互相独立**的凭证概念：

1. **账号 token**：客户端访问本后端用（`Authorization: Bearer <token>` / `x-api-key`）。
2. **DeepSeek 官方 API Key**：只用于 **联网检索 / TTS / ASR**（正文永远由 DGX 生成）。
   每个用户可以填自己的 Key，没填就回落到服务器环境变量 `DEEPSEEK_API_KEY`。

`GET/PUT /api/me/settings` 中的凭证块（**GET 永远只返回掩码**）：

```json
{
  "official": {"api_key": "sk-ab…12cd", "has_key": true, "base_url": null},
  "tts": {"provider": "deepseek", "base_url": null, "api_key": "sk-…cd",
          "has_key": true, "model": null, "voice": null},
  "asr": {"provider": null, "base_url": null, "api_key": null,
          "has_key": false, "model": null},
  "search": {"mode": null, "provider": null, "base_url": null,
             "api_key": "sk-…ef", "has_key": true, "max_results": null}
}
```

- **写入语义**（四个块 `official` / `tts` / `asr` / `search` 相同）：
  - 传完整字符串 → 设置；
  - 传 `""` 或 `null` → 清除；
  - 传 `"__keep__"` → 保持不变（UI 可以把掩码字段原样回传）；
  - **字段不传 → 保持不变**，整个块不传 → 块保留（避免 UI 漏传导致密钥丢失）。
- 只校验形状/长度：字符串 ≤512、`provider` ∈ `deepseek|openai|deepseek_tools|disabled`、
  `search.provider` ∈ `deepseek|brave|tavily|searxng|google_cse|none`、`search.mode` ∈ `dgx|official|off`、
  `max_results` ∈ 1..20；**不校验 Key 是否有效**（用 `/api/me/capabilities` 看"配没配"）。
- 其它设置键（`model`、`ui`…）仍是**整体替换**语义（与旧版本一致）。
- 非密钥字段（`provider`/`base_url`/`model`）在 GET 中原样返回（`null` 表示未设置）。

**解析优先级（高 → 低）**

| 用途 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|
| TTS/ASR（自定义端点） | 用户 `tts/asr.base_url` + 自己的 `api_key` | — | — | — |
| TTS/ASR（未配自定义端点） | 用户 `tts/asr.api_key` | 用户 `official.api_key`（+`official.base_url` 用于官方 provider） | 服务器 `TTS_*/ASR_*/DEEPSEEK_API_KEY` | provider=disabled |
| 联网检索 / `SEARCH_MODE=official` | 用户 `official.api_key` | 服务器 `DEEPSEEK_API_KEY` | 插件 provider（brave/tavily/searxng/google_cse）自带 Key | 无 → 报错 |

**安全边界：** 用户配置了自定义语音端点（`base_url`）却没给该端点 Key 时，后端**不会**把服务器/官方
Key 发给该第三方端点（只发送用户自己的 Key，可能为空）——避免把官方 Key 泄露给任意地址。

**搜索缺 Key 的行为（不再静默降级）**：请求声明了 `deepseek_web_search` / `deepseek_read_url` 但
既无用户官方 Key 也无服务器 Key（且 `SEARCH_MODE != off`）时，返回 SSE：

```
event: error
data: {"type":"error","error":{"message":"联网搜索需要 DeepSeek 官方 API Key…",
       "type":"authentication_error","code":"official_key_required"}}
```

响应头 `x-dgx-search: official_key_required`。客户端可用 `code` 定位提示文案。
`SEARCH_MODE=off`（管理员显式关闭）时不会报错，静默不带检索结果（`x-dgx-search: off`）。

**`GET /api/me/capabilities` 形状（无密钥材料）**

```json
{
  "official": {"configured": true, "source": "user"},
  "tts": {"configured": true, "source": "server"},
  "asr": {"configured": false, "source": null},
  "search": {"available": true, "requires_official": true}
}
```

- `source` 取值 `"user" | "server" | null`：`user` = 用户自己配的；`server` = 服务器环境变量
  （或管理员配置的插件 provider）；`null` = 没配。
- `search.available` 与真实检索能力一致（`mode=off` 时为 `false`）。

**Key 存放位置与加密**：密钥**明文**存放在 SQLite `user_settings.data`（JSON）里，**静态不加密**；
本项目**不做数据库静态加密**（见 §11 非目标）。生产部署请依赖：
NAS 磁盘/卷加密、`data/` 目录权限（仅容器用户可读）、备份文件加密。
密钥**不会**出现在 API 响应、`/admin/diagnostics`、审计日志或请求日志中（`DGX_LOG_UPSTREAM`
只记录发往上游的**请求体**，不含鉴权头）。

**想保持"完全服务器端"**：不要调用 `PUT /api/me/settings` 写 `official`/`tts`/`asr`，
即自动使用 `DEEPSEEK_API_KEY` 等环境变量，行为与旧版本完全一致。

### 2.2 账号自助 / 多端同步 / 管理附加接口

**个人凭证管理**（`/api/me/tokens*`）
- `GET` 返回 `{"tokens":[{id,label,createdAt,lastUsedAt,expiresAt,revoked,current}],"total":n}`，
  只列出**未吊销且未过期**的凭证。
  `id` 是 token 哈希的前 16 位（**不是 token 本身**，泄露也无法登录）。`lastUsedAt`
  在每次成功鉴权时刷新，**最多每分钟写一次**（避免写放大）。
- `DELETE /api/me/tokens/{id}` 精确删除自己名下的一条凭证；不存在返回 404，别人的返回 404（不泄露存在性）。
- `POST /api/me/tokens/revoke-others` 吊销除当前凭证外的全部，返回 `{"ok":true,"revokedOtherTokens":n}`。
- 修改密码（`POST /auth/change-password`，已有）会吊销**其它**凭证，保留当前。

**注销账号**（`DELETE /api/me`，body `{"password":"..."}`）
- 校验当前密码；删除该用户的会话、会话摘要、墓碑、磁盘文件、服务端文件行、本地附件行、
  设置、全部 token，最后删除用户行（外键级联兜底）。
- **最后一个启用中的管理员不能删除自己**（400），否则部署会失去管理入口。
- `usage_log` 保留（用于管理员计费/审计统计，不含聊天内容）。

**多端删除传播**（`GET /api/sessions/sync?since=<ms>`）
- 返回 `{"sessions":[更新的摘要],"deleted":[删除的会话 id],"serverTime":ms,"since":ms}`。
- `DELETE /api/sessions/{id}` 会写墓碑（即使该会话本就不存在），因此另一台设备能删掉本地缓存。
- 墓碑 90 天后自动清理（每次 sync 顺手清理）；`GET /api/sessions` 的形状**保持不变**。

**管理附加**
- 禁用/启用账号：`POST /admin/users/{id}/disable|enable`；禁用会立即吊销该用户全部 token，
  且不能禁用自己或最后一个管理员。
- 审计：`GET /admin/audit`，记录用户创建/删除/禁用/启用、密码重置、密码修改、
  凭证吊销、邀请码创建/吊销、账号自删、维护清理；**不记录密码或 token 明文**。
- 诊断：`GET /admin/diagnostics`（上游 `/models` 各 3 秒超时探测、DB/磁盘大小、ffmpeg、
  限流器计数、配置摘要；不含任何密钥）。
- 维护：`POST /admin/maintenance/cleanup` 默认 dry-run，`?dry_run=0` 执行；清理
  磁盘上无行的文件、有行但无文件的记录、无主会话/摘要/设置/token，以及过期墓碑。
- 管理页（`/admin`）新增「诊断」「审计日志」「指定密码」「禁用/启用」区块，仍然零 CDN。

---

## 3. 思考（reasoning）开关：两个网关的实测结论

现在有两个可用上游，**参数方言不同**：

| 端点 | 性质 | `thinking:{"type":"disabled"}` | `reasoning_effort:"none"` | 默认是否思考 | 会吐 reasoning 吗 |
|---|---|---|---|---|---|
| LAN `http://dgx.local:8000/v1` | vLLM，无鉴权，~0.6s | ✅ 0 reasoning（3/3） | ✅ 0 reasoning（3/3） | 是 | 会 |
| 公网 `https://api.example.com/v1` | litellm 代理 + 鉴权，~4s | ❌ **HTTP 400**（`litellm.UnsupportedParamsError: openai does not support parameters: ['thinking']`） | ✅ 接受 | 否 | **从不**（reasoning 通道不暴露，7 种参数组合实测均为 0） |

**因此默认 `DGX_THINKING_DISABLE_MODE=reasoning_effort`** —— 这是唯一在两个网关上都不报错的关闭开关。

补充实测：

| 参数 | LAN | 公网 |
|---|---|---|
| `enable_thinking:false` | 无效（仍思考） | 接受 |
| `chat_template_kwargs:{"thinking":false}` | 无效（仍思考） | 接受 |
| `enable_thinking:true` | 接受（思考） | 接受（但无 reasoning 输出） |
| `reasoning_effort:"high"` | 思考 | 接受（无 reasoning 输出） |
| `thinking:false`（布尔） | HTTP 400（要求对象） | HTTP 400 |

**本后端的做法：**

1. 关闭思考 → 只发 `reasoning_effort:"none"`（`DGX_THINKING_DISABLE_MODE` 可切换成
   `thinking` / `both` / `enable_thinking` / `chat_template_kwargs` / `none`，见 `.env.example`）。
2. 开启思考 → 只发 `enable_thinking:true`（两个网关都接受；公网代理会忽略，因此**公网路由不会出现思考块**，
   这是上游限制，不是协议错误；`tests/live_smoke.py` 会自动识别并跳过该断言）。
3. **方言自适应**：每个端点有自己的 `thinking_disable_mode`；如果某端点返回 400/422 并且错误信息
   看起来是"未知/不支持的参数"，后端会**把该请求去掉全部 thinking 参数后在同一端点重试一次**，
   成功就继续（日志里会打印 `endpoint … rejected thinking parameters …; retrying without them`）。
   公网网关的 `thinking` 400 就是这样被自动吸收的（`tests/live_dialect_smoke.py` 用真实请求验证过）。
4. **端点回退**：主端点连不上 / 5xx / 401 / 429 时，按配置顺序尝试备用端点
   （`DGX_BASE_URL_FALLBACK`、`_2`、`_3` 或 `DGX_FALLBACKS` JSON 列表），并记录用的是哪个端点。
5. 客户端关闭思考时**不发送 thinking 块**；即使上游仍然吐 `reasoning`，后端会**静默丢弃**，
   绝不混进正文。
6. **上游空响应 = 错误**：如果一轮里既没有正文也没有思考（公网代理偶发返回空 `content`），
   后端会下发 `event: error`（`code: "empty_response"`），而不是假装成功返回空回答。

> ⚠️ 一个真实坑：LAN 网关思考开启时，模型可能把**全部输出放进 `reasoning`**，`content` 为空——
> 标题生成如果沿用默认参数就会拿到空标题。所以标题请求固定走"关闭思考"，并且 `content` 为空时
> 回退读取 `message.reasoning`。

### 3.1 部署推荐配置（主 = 局域网，备 = 公网）

```dotenv
DGX_BASE_URL=http://dgx.local:8000/v1
DGX_API_KEY=
DGX_BASE_URL_FALLBACK=https://api.example.com/v1
DGX_API_KEY_FALLBACK=${DGX_API_KEY_FALLBACK}   # 部署时注入，不要提交进仓库
DGX_THINKING_DISABLE_MODE=reasoning_effort
```

等价的 JSON 写法（可逐端点指定方言/超时）：

```dotenv
DGX_FALLBACKS=[{"name":"public","base_url":"https://api.example.com/v1","api_key":"${FALLBACK_API_KEY}","thinking_disable_mode":"reasoning_effort","timeout":120}]
```

---

## 4. 系统提示词：逐字节透传（硬约束）

`chat-demo/src/services/chat/store.ts` 的常量：

```ts
const SYSTEM_PROMPT = '该助手为DeepSeek，由深度求索公司创造。'
```

后端把它**原样**作为唯一一条 `{"role":"system"}` 消息放在 `messages[0]`：

- 不裁剪、不加前缀/后缀、不注入日期、不加任何"你是 DGX…"的说明；
- **不会额外插入自己的 system 消息**；
- `latest_reminder` 也**不会**并进 system（vLLM 的 chat template 只允许开头一条 system），
  而是折叠进**紧随其后的那条 user 消息**，形如：

```
reminder: 当前时间：2026-11-17 13:00（Asia/Shanghai）

用户原本的问题
```

这样 system 保持官方原样，reminder 的"时效性"语义也仍紧贴对应轮次。
（SPEC §3.2 允许这两种处理，这里选后者，原因就是 chat template 限制。）

**核对方式**：`DGX_LOG_UPSTREAM=1` 会把每一份发往上游的完整请求体写进日志，
并额外单独打印一行 `UPSTREAM_SYSTEM_JSON=...`，便于 diff：

```bash
DGX_LOG_UPSTREAM=1 python -m uvicorn app.main:app --port 8439
# 另开一个终端发一轮对话，然后：
python tests/_check_system_log.py
# occurrences=7 byte_identical=7
```

**真实抓包（2026-11-17，`tests/live_smoke.py` 期间由 `DGX_LOG_UPSTREAM=1` 记录）**：

```json
{
  "model": "deepseek-v4.1-flash-official",
  "messages": [
    { "role": "system", "content": "该助手为DeepSeek，由深度求索公司创造。" },
    { "role": "user", "content": "用一句话介绍杭州。开头必须是：杭州是" },
    { "role": "assistant", "content": "杭州是" }
  ],
  "stream": true,
  "stream_options": { "include_usage": true },
  "max_tokens": 8192,
  "thinking": { "type": "disabled" },
  "reasoning_effort": "none",
  "continue_final_message": true,
  "add_generation_prompt": false
}
```

（`system` 与官方常量逐字节相同，日志核验 7/7 次全等。）

---

## 5. 翻译规则（Anthropic → OpenAI）

| 客户端 | 上游 |
|---|---|
| `system` | 首条 `{"role":"system"}`，**逐字节原样** |
| `latest_reminder` | 折叠进下一条 user（`reminder: ...\n\n<原问题>`），不进 system |
| user 字符串 | 原样 |
| user 块数组 | `text` 块拼接；`image.source.file_id` → 读盘 → `data:<mime>;base64,...` 的 `image_url`；`image.source.base64/url` 也支持 |
| `document` | 能按 UTF-8 文本解码的（text/*、json、csv、md、代码等）→ `<document name="文件名">…</document>`；二进制/PDF 忽略（不报错） |
| assistant history | 只取 `text` 块拼接；`thinking`、`server_tool_use`、`web_fetch_tool_result`、`tool_use` 全部丢弃 |
| 末尾 assistant（Prefill） | 走 vLLM 的 `continue_final_message:true` + `add_generation_prompt:false`（**实测支持**）；若上游返回 400，自动降级为在 user 末尾追加 `继续：` 重试一次 |
| `thinking` | 见 §3 |
| `tools`（`deepseek_web_search`/`deepseek_read_url`） | 见 §6；DGX 本身无联网能力 |
| `title:{enabled:true}` | 并行发一个短请求（关闭思考）生成标题，塞进 `message_delta.delta.title`（≤30 字符） |
| 连续同角色消息 | 合并（避免 chat template 报错） |

上游 usage 只在流式请求带 `stream_options.include_usage=true` 时出现——**非流式请求绝不带 `stream_options`**
（网关会 400）。`prompt_tokens_details.cached_tokens` 映射为 `cache_read_input_tokens`，
`created_cache_tokens` 映射为 `cache_creation_input_tokens`，`input_tokens = prompt_tokens - cached`。

### 5.1 附件文档解析

| 附件类型 | 处理 |
|---|---|
| `text/*`、json、csv、md、代码等可解码文本 | 原文包成 `<document name="文件名">…</document>`（与旧版本完全一致） |
| `application/pdf`、`.pdf` | 用 `pypdf` 逐页抽取文本；加密/结构损坏 → **400 + 明确中文错误**，绝不 500；扫描件无文本 → 400 提示需要 OCR |
| `.docx`（wordprocessingml） | 用 `python-docx` 抽取段落与表格（表格行以 ` \| ` 连接） |
| `.xlsx` / `.xls` | **跳过**（`openpyxl` 不是本项目依赖），响应头 `x-dgx-notice` 带 `xlsx_not_supported` |
| 其它二进制（`application/octet-stream` 等） | 跳过，`unsupported_document_skipped` |

- 单份文档读取上限 `DGXCHAT_MAX_DOCUMENT_BYTES`（默认 8 MiB，超出 413）；
  抽取后进入上下文的字符预算 `DGXCHAT_MAX_DOCUMENT_CHARS`（默认 120000，超出截断并标注
  `…[truncated]`，响应头 `document_truncated`）。
- 解析器缺失/损坏都走「清晰错误 + 不 500」；文档缺失（file_id 已删）退化为跳过该文档，
  聊天继续（`document_file_missing`）。
- 依赖：`pypdf==7.1.0`、`python-docx==1.2.0`（已 pin 进 `requirements*.txt` 与镜像）。

### 5.2 SSE 事件（与 `parseEvent.ts` 对齐）

```
message_start            {"message":{"id":"<无前缀 uuid>","model":"<客户端 model>","usage":{0,0,0,0}}}
content_block_start      thinking 块（仅思考开启且上游真的思考时）
content_block_delta      thinking_delta ...
content_block_stop
content_block_start      text 块
content_block_delta      text_delta ...
content_block_delta      citations_delta ...（仅联网检索且模型写了 [n]）
content_block_stop
message_delta            {"delta":{"stop_reason":"end_turn","stop_sequence":null,"title":"…"?},"usage":{...}}
message_stop
```

- 上游错误 → `event: error` + `{"error":{"message":...}}`，随后连接关闭；
- 客户端断连 → 立即关闭上游连接（`httpx` 的 stream context 在取消时释放）；
- 若上游一个字都没吐，仍会补一个空的 `text` 块——否则客户端的 `receivedContent`
  会误判为附件上传失败并触发重传退避（见 `store.ts` 的 `FILE_RETRY_LIMIT` 分支）；
- 思考块/文本块的 index 依次分配，惰性开启（先出现哪个就先开哪个，绝不发空块）。

---

## 6. 联网搜索 / 链接阅读（SEARCH_MODE）

`SEARCH_MODE` 三档，**默认 `dgx`**（用户要求：回答正文必须由本地 DGX 生成）：

| 模式 | 行为 |
|---|---|
| `dgx`（默认） | 后端用官方 key **只做检索**（请求官方 Anthropic 端点，收到 `web_search_tool_result` 立即 abort，省钱），把该结果块**原样**下发给客户端；检索摘要拼进给 DGX 的 user 上下文，要求模型用 `[n]` 标注；正文流里把 `[n]` 换成 `citations_delta`。链接阅读由后端抓取（8s 超时、截断 8k 字）后下发 `server_tool_use(kind=read_link)` + `web_fetch_tool_result`。 |
| `official`（可选，默认关闭） | 整轮请求转发到 `https://api.deepseek.com/anthropic/v1/messages`，**原样中继 SSE**（JSON 重新序列化，字段不丢不改）。搜索结果/深搜/图搜/引用与官方 App 100% 一致，但正文由官方云模型生成。 |
| `off` | 完全不检索；响应头 `x-dgx-search: off`，正常回答。 |

- 检索来源可插拔：`SEARCH_PROVIDER=deepseek|brave|tavily|searxng|google_cse|none`，
  用 `SEARCH_BASE_URL`/`SEARCH_API_KEY` 配置；`deepseek` 之外的 provider 会合成
  `srvtoolu_s_00_<32hex>` 形态的合规块。
- 官方 Key 的解析优先级与"缺 Key 报错"行为见 §2.1：用户 `official.api_key` 优先，其次服务器
  `DEEPSEEK_API_KEY`；两者都没有且声明了搜索工具时返回 `official_key_required` 错误帧，
  **不再静默降级**。
- 检索请求固定携带服务器本地时间（含年月日/时区），避免官方模型把"最近"猜测成几个月前。
- 无 key / 无 provider → 优雅降级：不发工具块、正常回答，响应头 `x-dgx-search: unavailable`。
- 每用户可通过 `PUT /api/me/settings` 覆盖：`{"search":{"mode":"off","provider":"none","api_key":"…"}}`。
- 客户端回传的历史里只有 `read_link` 的 `server_tool_use` + `web_fetch_tool_result`，
  后端解析时全部接受并忽略（不会崩），见 `tests/test_translation.py::test_assistant_history_keeps_only_text`。

> 🔴 **TODO（需要真实 key 才能验证，代码已就绪）**：`SEARCH_MODE=official` 的中继路径
> 已用本地 mock 官方端点验证帧序与字段无损（`tests/test_sse.py`、`tests/test_search.py`），
> 但**还没有对真实官方端点跑过整轮中继**。一条命令验证：
>
> ```bash
> DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY SEARCH_MODE=official \
>   python tests/live_smoke.py --search
> ```
>
> 该命令会真实请求官方端点并断言 `server_tool_use` / `web_search_tool_result` /
> `citations_delta` 全部通过客户端 `parseEvent.ts` 的严格校验。

---

## 7. 语音（TTS / ASR）

| provider | TTS | ASR | 需要 ffmpeg |
|---|---|---|---|
| `deepseek`（默认） | `POST {DEEPSEEK_BASE_URL}/v1/tools/tts`，body `{text,voice_id,format:"pcm",filter_markdown}` → **原生 24kHz 单声道 s16le**，**原样透传不转码** | `POST {…}/v1/tools/asr`，multipart 字段 `file` → `{"text":…}` | 否 |
| `deepseek_tools` | 同上（自建兼容端点） | 同上 | 否 |
| `openai` | `POST {base}/audio/speech`（`response_format:pcm`，voice 由 `TTS_VOICE_MAP` 映射，默认 `stella→alloy`、`tide→echo`） | `POST {base}/audio/transcriptions`（`model`+`file`） | 返回非 PCM 时需要 |
| `disabled` | 503 | 503 | — |

- 输出统一为 `audio/pcm; rate=24000; encoding=signed-integer; bits=16; channels=1`。
- 上游若返回 WAV/MP3/OGG 或非 24k 的 PCM → 用 ffmpeg 转码（`FFMPEG_BIN` 可指定路径）；
  **镜像里装了 ffmpeg**；本地没装时返回 502 并明确提示"需要 ffmpeg"，不会返回坏音频。
- `filter_markdown:true`（客户端默认）：去掉代码块、表格、引用符号、标题/列表标记、链接语法、
  长 LaTeX、HTML 标签、强调符号后再合成（保留引用文字本身，只去 `>` 符号）。
- 每用户每日 TTS 字符配额 `DAILY_TTS_CHARS`（默认 20000，0=不限），超限 429 + `Retry-After`。
- 每用户可通过 `PUT /api/me/settings` 覆盖：`{"tts":{"provider":"openai","base_url":"…","api_key":"…","voice":"alloy"}}`；
  优先级与官方 Key 回落见 §2.1。密钥只写不回显。

---

## 8. 安全默认值（P0/P1 清单）

| 项目 | 状态 | 默认值 |
|---|---|---|
| 邀请码注册 `REGISTRATION_MODE` | ✅ invite/closed/open，官方邀请码表 + 原子消费 + 过期/吊销/次数 | `invite`（空库首个注册者免码且为 admin） |
| `/docs` `/redoc` `/openapi.json` | ✅ 默认 404（`DGXCHAT_DOCS=1` 才开） | 关闭 |
| 登录限流 | ✅ 每 (IP+用户名) 和每 IP 滑动窗口，429 + `Retry-After` | 10 次 / 300s |
| 注册限流 | ✅ 每 IP | 5 次 / 小时 |
| 通用鉴权错误 | ✅ 错用户/错密码同一条 `用户名或密码错误`；邀请码失败统一 `邀请码无效或不可用` | — |
| 不泄露栈信息 | ✅ 500 只返回 `{"error":{"message":"internal error"}}`，traceback 只进服务端日志；`Server` 头固定为 `dgxchat`；`/health` 匿名只返回 `{"ok","status"}` | — |
| 请求体上限 | ✅ JSON 8 MiB / 上传 100 MiB，先看 `Content-Length` 再流式计数，超限 413 | 见 §9 |
| CORS 默认非通配符 | ✅ 默认 `https://localhost`、`http://localhost`、`capacitor://localhost`、`ionic://localhost` + 127.0.0.1/私网 LAN 正则；只有显式 `CORS_ALLOW_ORIGINS=*` 才放开 | 见 §9 |
| 每用户消息限流 | ✅ `/anthropic/v1/messages` | 20 次 / 分钟 |
| 每用户每日 token 配额 | ✅ 超限 429（0=不限） | 0 |
| 每用户每日 TTS 字符配额 | ✅ 超限 429 | 20000 |
| `usage_log` 记录 | ✅ messages / tts / asr / official 中继；`/admin/stats` 汇总 | — |
| 吊销 token | ✅ `POST /admin/users/{id}/revoke-tokens`；重置密码也会吊销；用户可自助管理自己的设备（`/api/me/tokens*`） | — |
| Token 有效期 | ✅ `TOKEN_TTL_DAYS`，0=永不过期（可吊销） | 0 |
| 凭证列表 | ✅ 只返回 token 哈希前 16 位的公开 id，**永不回显 token**；`last_used_at` 最多每分钟刷新一次 | — |
| 审计日志 | ✅ `audit_log` 记录 12 类管理/安全操作，`/admin/audit` 可分页查看；**不记录密码/token 明文** | 保留全部 |
| 存储配额 | ✅ `USER_STORAGE_MB`（0=不限，默认 0）；Files API 与 `/api/files/{id}` 均执行 | 不限 |
| 注销账号 | ✅ 需当前密码；级联删除会话/文件/设置/token；最后管理员不可自删 | — |
| 多端删除传播 | ✅ `deleted_sessions` 墓碑 + `/api/sessions/sync`；90 天自动清理 | 90 天 |
| 孤儿清理 | ✅ `/admin/maintenance/cleanup`（默认 dry-run） | dry-run |
| 密码哈希 | ✅ stdlib `hashlib.scrypt`（N=2^14,r=8,p=1，16B 随机盐），`hmac.compare_digest` 比较 | — |
| Token 存储 | ✅ 32 字节随机 hex，库里只存 sha256，camel 比较 | — |
| 多用户隔离 | ✅ `sessions`/`client_files` 复合主键 `(user_id, id)`；所有查询带 `user_id`；文件按用户目录隔离；路径穿越防护 | — |
| 公开页面信息最小化 | ✅ 首页只有 `Service is running.`，无产品名/模型名/版本/栈信息/`/admin` 链接 | — |

---

## 9. 配置项（节选，完整见 `.env.example`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DGX_BASE_URL` | `http://dgx.local:8000/v1` | 主端点（局域网 DGX 网关） |
| `DGX_API_KEY` | 空 | 主端点鉴权（局域网不需要） |
| `DGX_BASE_URL_FALLBACK` / `DGX_API_KEY_FALLBACK` | 空 | 备用端点（支持 `_2`、`_3`）；也可用 `DGX_FALLBACKS` JSON 列表逐端点配方言 |
| `DGX_MODEL_MAP` | 两个客户端模型 → `deepseek-v4.1-flash-official` | 模型映射 |
| `DGX_THINKING_DISABLE_MODE` | `reasoning_effort` | 见 §3；`thinking`/`both` 只适合局域网网关 |
| `DGX_LOG_UPSTREAM` | `0` | 1=打印完整上游请求体（含 `UPSTREAM_SYSTEM_JSON`） |
| `DGXCHAT_DATA_DIR` | `./data`（镜像内 `/data`） | SQLite + 文件 |
| `WEB_DIR` | `./web`（镜像内 `/app/web`） | 前端构建产物目录，存在则在 `/` 提供并做 SPA 深链回退 |
| `REGISTRATION_MODE` | `invite` | invite/closed/open |
| `TOKEN_TTL_DAYS` | `0` | 0=永不过期 |
| `DGXCHAT_DOCS` | `0` | 1=开启 `/docs` |
| `LOGIN_MAX_FAILURES` / `LOGIN_WINDOW_SECONDS` | `10` / `300` | 登录限流 |
| `REGISTER_MAX_PER_HOUR` | `5` | 注册限流 |
| `MESSAGE_RATE_PER_MINUTE` | `20` | 每用户消息限流 |
| `DAILY_TOKEN_QUOTA` | `0` | 每用户每日 token 配额（0=不限） |
| `DAILY_TTS_CHARS` | `20000` | 每用户每日 TTS 字符（0=不限） |
| `DGXCHAT_MAX_JSON_BYTES` | `8388608` | JSON 体上限 |
| `DGXCHAT_MAX_UPLOAD_BYTES` | `104857600` | 上传上限 |
| `DGXCHAT_MAX_DOCUMENT_CHARS` | `120000` | 单份文档进入上下文的最大字符数 |
| `USER_STORAGE_MB` | `0` | 每用户存储配额（MiB，0=不限；超出 413） |
| `CORS_ALLOW_ORIGINS` | 空（=具体白名单） | 只有显式 `*` 才通配 |
| `DGXCHAT_TRUST_PROXY` | `0` | 反向代理后置 1 才信任 `X-Forwarded-For` |
| `SEARCH_MODE` | `dgx` | dgx/official/off |
| `SEARCH_PROVIDER` | `deepseek` | 检索来源 |
| `TTS_PROVIDER` / `ASR_PROVIDER` | 有 `DEEPSEEK_API_KEY` 时 `deepseek`，否则 `disabled` | 语音供应商 |
| `FFMPEG_BIN` | 自动查找 | 语音转码 |

---

## 10. 部署到 NAS（Docker）

目标：UGREEN NAS，Debian 12 x86_64，docker 29.6.2 + compose v5.1.3。

```bash
# 目录：/volume1/docker/dgxchat/{app,data,web}
# app  = 本目录（server/ 的内容）
# data = 数据卷（SQLite + 上传文件）
# web  = 前端构建产物（可选）

cd /volume1/docker/dgxchat/app
cp .env.example .env && vi .env          # 至少填 DEEPSEEK_API_KEY、REGISTRATION_MODE
sudo docker compose up -d --build
sudo docker compose logs -f --tail=50
curl -s http://127.0.0.1:8439/health      # {"ok":true,"status":"up"}
```

- 镜像基于 `python:3.12-slim`，**不残留 build-essential**（全部是 wheel），装了 `ffmpeg`。
- 容器**以非 root 启动**：entrypoint 先把 `/data` chown 给 uid 10001，再用 `setpriv` 降权
  （`DGXCHAT_SKIP_CHOWN=1` 可跳过每次启动的 chown；想固定 UID 用 `DGXCHAT_UID/GID`）。
- 数据卷 `./data:/data`、前端 `./web:/app/web:ro`、端口 `8439:8439`，
  `restart: unless-stopped`，`HEALTHCHECK` 打 `/health`，日志 json-file 轮转 10m×3。
- 容器是 `--no-server-header`，`Server` 头由应用固定为 `dgxchat`。

回滚：`docker compose down && docker image ls | grep dgxchat`，用上一版 tag 重新 `up -d`；
数据都在 `./data`，升级不丢。

---

## 11. 已知限制 / 未实现

1. **`SEARCH_MODE=official` 未对真实官方端点整轮验证**（缺 key 时的安全默认是 `dgx`），
   验证命令见 §6 的 TODO。中继逻辑本身已有 mock 测试。
2. **ASR/TTS 的 `openai` provider 未对真实 OpenAI 跑过**（无 key）；代码路径用假客户端做了
   请求形状与转码分支的单测，`deepseek` provider 已真实跑通 TTS→WAV→ASR 闭环。
3. **文档解析只支持纯文本类**：PDF/Word/PPT 需要版面解析（官方 `rich_layout_recognition`），
   这里直接忽略并继续（不报错、不崩）。SPEC §3.3 允许"其它忽略"。
4. **`usage` 的 token 数是上游给的**：网关不开 `include_usage` 时为 0（客户端会显示 0）。
5. **`daily_token_quota` 统计的是上游返回的 input+output 之和**，检索/title 的消耗不计入
   （检索走官方 key，需要单独在官方后台看余额）。
6. **单进程内存限流**：多 worker 或多副本部署时每个进程独立计数（当前 compose 单进程）。
7. **`/v1/tools/*` 不解码/重采样除 24k/mono 之外的输入**：ASR 原样转发上游，由上游负责解码。
8. **`official` 中继会把 JSON 重新序列化**（字段不增不减、语义等价），不是字节级中继。
9. **公网 litellm 网关不暴露 reasoning 通道**：思考开启时客户端不会收到 thinking 块（上游限制）；
   关闭思考时用 `reasoning_effort:"none"` 同样不影响正文。**正文质量不受影响**。
10. **公网网关会对 `thinking:{...}` 返回 400**：后端会自动去掉该参数重试，但每次切换方言会多一次
    上游请求（只在端点方言配置错误时发生；默认 `reasoning_effort` 不会触发）。
11. **XLSX 不支持**：`openpyxl` 不是本项目依赖（按需求"已是依赖才支持"跳过）；上传 xlsx 会跳过并
    在 `x-dgx-notice` 里标注 `xlsx_not_supported`，不会报错。需要表格内容时请转成 CSV/文本。
12. **注销账号保留 `usage_log` 与 `audit_log`**：前者用于管理员统计用量（不含聊天内容），
    后者是管理审计记录（actor_name 保留），如需彻底清除请管理员手动处理数据库。
13. **`/api/sessions/sync` 只返回摘要与墓碑**，不返回会话正文；正文仍按需 `GET /api/sessions/{id}`。
14. **限流器计数是单进程内存值**，`/admin/diagnostics` 里显示的是当前进程的状态。
15. **数据库静态加密不是目标**：用户官方 Key / 语音端点 Key 以**明文**存放在 SQLite
    `user_settings.data`；请依赖 NAS 卷加密、`data/` 目录权限与备份加密（见 §2.1）。
16. **不校验用户 Key 是否有效**：`PUT /api/me/settings` 只做形状/长度校验；
    真实可用性由 `/api/me/capabilities`（是否配置）与实际请求结果反映。

---

## 12. 代码结构

```
server/
  app/
    main.py            应用装配、CORS、静态/SPA、/health、/models、管理页
    config.py          全部 env 配置（含模型映射、供应商表、搜索配置）
    db.py              SQLite（WAL、幂等建表、列迁移）
    models.py          pydantic 模型 + 统一错误体
    auth.py            scrypt 密码、opaque token、邀请码注册、登录限流
    security.py        限流器、配额、请求体上限、usage_log
    upstream.py        DGX（OpenAI 兼容）与官方 Anthropic 端点客户端
    anthropic_compat.py 翻译 + SSE 状态机 + 标题生成 + official 中继  ← 核心
    search.py          检索 provider、read_url、块合成、上下文注入
    files.py           Files API（磁盘 + DB、文件名清洗、大小限制）
    speech.py          TTS/ASR provider、markdown 过滤、ffmpeg 转码、配额
    sync.py            云端会话/附件/设置同步（含删除墓碑与增量同步；设置接口做密钥掩码）
    credentials.py     每用户凭证：掩码、写入语义、解析优先级、capabilities 辅助
    me.py              账号自助：个人凭证、注销账号、我的用量、能力探测
    audit.py           管理/安全操作审计（永不抛出）
    admin.py           管理 API + 内置管理页面（含诊断/审计/清理）
  tests/
    conftest.py        离线 fixtures（桩上游、TestClient）
    test_translation.py / test_sse.py / test_auth.py / test_isolation.py
    test_files.py / test_speech.py / test_search.py / test_static.py
    test_me.py                                 个人凭证/注销/用量统计
    test_sync_tombstones.py                    删除墓碑与增量同步
    test_admin_extras.py                       禁用启用/审计/诊断/清理/指定密码
    test_documents.py                          PDF/DOCX 解析与降级
    test_storage_quota.py                      每用户存储配额
    test_upstream_dialect.py                  每端点方言 + 回退（httpx.MockTransport）
    live_smoke.py      真实网关端到端验证（独立脚本，非 pytest）
    live_dialect_smoke.py  真实网关的「方言拒绝→剥离重试」「主端点不可用→回退」验证
    _probe_upstream.py / _probe_thinking2.py / _probe_enable_thinking.py
                                               思考开关实测脚本（README §3 数据来源）
    _check_system_log.py                       系统提示词日志核对
    _manual_turn.py / _manual_features.py / _manual_speech.py   手工联调脚本
  Dockerfile / docker-compose.yml / docker-entrypoint.sh
  requirements.txt / requirements-prod.txt / .env.example
```
