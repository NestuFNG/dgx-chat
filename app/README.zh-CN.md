# DGX Chat

简体中文 | [English](README.md)

DGX Chat 是面向 DGX 推理网关的自建多端聊天前端，基于开源项目
[deepseek-ai/chat-demo](https://github.com/deepseek-ai/chat-demo)（MIT License）改造。
上游的界面、流式协议处理与渲染管线保持不变；本分支新增了运行期可配置的 API 地址、
针对自建后端的账号登录 / 注册，以及云端会话同步。

## 开始使用

需要 Node.js 22.12+ 与 pnpm。

```sh
pnpm install
pnpm dev        # http://localhost:8090
```

构建静态站点：

```sh
pnpm build      # 或 pnpm build:win，两者都是跨平台的 Node 脚本
pnpm type       # TypeScript 类型检查
```

产物输出到 `dist/`。`scripts/build.mjs` 负责删除 `dist/` 并以 `NODE_ENV=production`
运行 rspack，在 PowerShell、cmd、bash 下行为一致。

## 服务器地址（运行期可配置）

API 根地址在**每次请求时**重新解析，设置后立即生效：

1. `localStorage['dgxchat:origin']` —— 由登录页或「设置 → 服务器 / 账号」写入；
2. 否则使用构建期默认值 `__API_ORIGIN__`（见 `rspack.config.ts`）。**分发包默认不内置地址，
   为空字符串**；自托管者可用 `DGX_API_ORIGIN=http://their-host:8439 pnpm build` 把自己的地址
   烧进包里。既没有保存值、构建时也没指定地址时，登录页会要求用户显式填写，API 调用会以
   「请先在设置里填写服务器地址」失败，而不会回退到任何主机。

## 账号与云同步

- 首次使用在登录页注册（`POST /auth/register`）；空库时第一个注册的账号自动成为管理员。
- 登录 token 存在 `localStorage['dgxchat:token']`。
- 登录后会话与附件通过 `PUT /api/sessions/{id}`、`PUT /api/files/{local_id}` 写穿到云端；
  登录 / 启动时拉取会话列表，打开会话时按需拉取全文。
- IndexedDB 仍是离线优先缓存：界面从不等待网络，同步失败只记日志，后端不可用时应用照常渲染。
- 登录页的「直接用 API Key 直连（不使用云端会话）」保留上游直连模式：在设置里填入
  API Key 即可聊天，会话只保存在本机；该选择记录在 `localStorage['dgxchat:skip-login']`。

## 本地联调后端（仅开发用）

`scripts/mock-backend.mjs` 用内存实现了 `/auth/*` 与 `/api/*`，无需真实后端即可联调
登录、会话同步与附件流程：

```sh
pnpm mock     # http://127.0.0.1:8787
```

它还实现了 `GET /models` 与最小可用的 `POST /anthropic/v1/messages` SSE，便于把聊天请求指向它。
首个账号之后注册需要邀请码，mock 的邀请码为 `DGX-INVITE-2026`。该脚本不参与构建。

## 已知限制（v1）

- **不传播远端删除**：在其他设备删除的会话，本机要等用户自己删除后才会消失；拉取不会主动删本地会话。
- **最后写入获胜（依赖客户端时钟）**：服务端保留 `updatedAt` 更新的一份，设备时钟偏差可能让旧编辑显得更新。
  拉取路径不会把远端较新的时间戳套在本地旧内容上。
- **`/api/me/settings` 未同步**：主题、语言、朗读音色与深度思考 / 智能搜索开关仍按浏览器保存。
- **账号隔离按 IndexedDB 分库**（`dgxchat:local`、`dgxchat:user-<id>`）。分库前的 `chat-demo` 旧库
  故意保留为孤立数据、不迁移到任何账号；如果检测到该库，控制台会打印一条提示。
- **账号切换会丢弃排队中的同步任务**：登出前刚排队的删除可能没发到服务器，该账号再次登录时会话可能重新出现。
- **附件上传**失败重试最多 4 次、不会向界面抛错；已上传过的跳过，永久失败只记日志。

## 许可证

MIT。上游版权声明与许可证原文保留在 [LICENSE](LICENSE)，改造说明与署名见 [NOTICE](NOTICE)。
