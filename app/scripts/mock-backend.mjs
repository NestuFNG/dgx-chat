/**
 * 本地联调用的最小后端（仅用于前端同步链路自测，不参与构建、不随 dist 发布）。
 *
 * 实现 SPEC §2.1 的 /auth/* 与 §2.3 的 /api/*，数据放内存：
 *   node scripts/mock-backend.mjs [port]
 * 然后在前端登录页把服务器地址改成 http://127.0.0.1:8787 并注册账号。
 * 所有请求都会打印到控制台，便于观察前端实际发出的同步请求。
 */
import { createServer } from 'node:http'

const port = Number(process.argv[2] ?? 8787)

/** @type {Map<string, {id:string, username:string, password:string, role:string, createdAt:number}>} */
const users = new Map()
/** @type {Map<string, string>} token -> userId */
const tokens = new Map()
/** @type {Map<string, {userId: string, session: any}>} key = `${userId}:${sessionId}`（按用户隔离） */
const sessions = new Map()
/** @type {Map<string, Buffer>} key = `${userId}:${fileId}` */
const files = new Map()
/** 邀请码：REGISTRATION_MODE=invite 时，非首个账号必须匹配 */
const INVITE_CODE = process.env.MOCK_INVITE_CODE ?? 'DGX-INVITE-2026'
/** 登录失败计数：同一用户名 1 分钟内失败 3 次后返回 429 */
const loginFailures = new Map()

const cors = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization,x-api-key,content-type,anthropic-beta',
  'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS',
}

const json = (res, status, body) => {
  const payload = JSON.stringify(body)
  res.writeHead(status, { ...cors, 'Content-Type': 'application/json' })
  res.end(payload)
}

const unauthorized = (res) =>
  json(res, 401, { error: { message: 'invalid token', type: 'authentication_error' } })

const userOf = (req) => {
  const header = req.headers.authorization ?? ''
  const token = header.startsWith('Bearer ') ? header.slice(7) : (req.headers['x-api-key'] ?? '')
  const userId = token ? tokens.get(token) : undefined
  return userId ? users.get(userId) : undefined
}

const readBody = async (req) => {
  const chunks = []
  for await (const chunk of req) {
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

const summarize = (session) => ({
  id: session.id,
  title: session.title,
  createdAt: session.createdAt,
  updatedAt: session.updatedAt,
})

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${port}`)
  const path = url.pathname
  const method = req.method ?? 'GET'

  if (method === 'OPTIONS') {
    res.writeHead(204, cors)
    res.end()
    return
  }

  try {
    if (method === 'POST' && (path === '/auth/register' || path === '/auth/login')) {
      const body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      const username = String(body.username ?? '').trim()
      const password = String(body.password ?? '')
      if (!username || !password) {
        json(res, 400, { error: { message: 'username and password are required' } })
        return
      }
      const failures = loginFailures.get(username) ?? { count: 0, since: Date.now() }
      if (Date.now() - failures.since > 60_000) {
        failures.count = 0
        failures.since = Date.now()
      }
      if (failures.count >= 3) {
        console.log(`[mock] ${method} ${path} -> 429 (${username} throttled)`)
        json(res, 429, { error: { message: 'too many attempts' } })
        return
      }
      let user = [...users.values()].find((item) => item.username === username)
      if (path === '/auth/register') {
        if (user) {
          json(res, 400, { error: { message: 'username already exists' } })
          return
        }
        // bootstrap：空库第一个账号无需邀请码；之后必须是有效邀请码
        const invite = String(body.invite_code ?? '').trim()
        if (users.size > 0 && invite !== INVITE_CODE) {
          failures.count += 1
          loginFailures.set(username, failures)
          console.log(`[mock] ${method} ${path} -> 403 (${username}, bad invite "${invite}")`)
          json(res, 403, { error: { message: 'registration is closed or invite code is invalid' } })
          return
        }
        user = {
          id: `u${users.size + 1}`,
          username,
          password,
          role: users.size === 0 ? 'admin' : 'user',
          createdAt: Date.now(),
        }
        users.set(user.id, user)
        loginFailures.delete(username)
      } else if (!user || user.password !== password) {
        failures.count += 1
        loginFailures.set(username, failures)
        json(res, 401, { error: { message: 'invalid username or password', type: 'authentication_error' } })
        return
      }
      const token = `token-${user.id}-${Math.random().toString(16).slice(2)}`
      tokens.set(token, user.id)
      console.log(`[mock] ${method} ${path} -> 200 (${username})`)
      json(res, 200, {
        token,
        user: { id: user.id, username: user.username, role: user.role, createdAt: user.createdAt },
      })
      return
    }

    // 兼容层接口不校验账号（对应 SPEC：/models、/anthropic/* 接受 token 或 API Key）
    const isCompat = path === '/models' || path.startsWith('/anthropic/')
    const user = isCompat ? { id: 'compat', username: 'compat' } : userOf(req)
    if (!user) {
      console.log(`[mock] ${method} ${path} -> 401`)
      unauthorized(res)
      return
    }

    // 兼容层：/models 与 Anthropic Messages SSE（只用于验证前端把请求发到了运行期地址）
    if (method === 'GET' && path === '/models') {
      console.log(`[mock] GET ${path} -> 200`)
      json(res, 200, {
        object: 'list',
        data: [{ id: 'deepseek-flash', object: 'model', created: Date.now(), owned_by: 'dgx' }],
      })
      return
    }

    if (path === '/anthropic/v1/files' && method === 'POST') {
      const body = await readBody(req)
      const id = `file-api-mock-${Date.now()}`
      files.set(id, body)
      console.log(`[mock] POST ${path} -> 200 (${body.length} bytes)`)
      json(res, 200, {
        id,
        type: 'file',
        size_bytes: body.length,
        created_at: Math.floor(Date.now() / 1000),
        filename: 'upload.bin',
        mime_type: 'application/octet-stream',
      })
      return
    }

    if (path === '/anthropic/v1/messages' && method === 'POST') {
      const body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      const last = [...(body.messages ?? [])].reverse().find((m) => m.role === 'user')
      const text =
        typeof last?.content === 'string'
          ? last.content
          : (last?.content ?? [])
              .filter((block) => block.type === 'text')
              .map((block) => block.text)
              .join('')
      console.log(`[mock] POST ${path} -> 200 SSE ("${String(text).slice(0, 40)}")`)
      res.writeHead(200, {
        ...cors,
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        Connection: 'keep-alive',
      })
      const send = (event, data) => {
        res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
      }
      send('message_start', {
        type: 'message_start',
        message: {
          id: `mock-${Date.now()}`,
          model: body.model ?? 'deepseek-flash',
          usage: { input_tokens: 1, output_tokens: 0 },
        },
      })
      send('content_block_start', {
        type: 'content_block_start',
        index: 0,
        content_block: { type: 'text', text: '' },
      })
      send('content_block_delta', {
        type: 'content_block_delta',
        index: 0,
        delta: { type: 'text_delta', text: `收到：${text || '（空消息）'}` },
      })
      send('content_block_stop', { type: 'content_block_stop', index: 0 })
      send('message_delta', {
        type: 'message_delta',
        delta: { stop_reason: 'end_turn', stop_sequence: null, title: '模拟标题' },
        usage: { input_tokens: 1, output_tokens: 8 },
      })
      send('message_stop', { type: 'message_stop' })
      res.end()
      return
    }

    if (method === 'GET' && path === '/auth/me') {
      console.log(`[mock] ${method} ${path} -> 200 (${user.username})`)
      json(res, 200, {
        id: user.id,
        username: user.username,
        role: user.role,
        createdAt: user.createdAt,
      })
      return
    }

    if (method === 'POST' && path === '/auth/logout') {
      console.log(`[mock] ${method} ${path} -> 200`)
      json(res, 200, { ok: true })
      return
    }

    if (path === '/api/sessions') {
      if (method === 'GET') {
        const own = [...sessions.values()].filter((item) => item.userId === user.id)
        console.log(`[mock] GET ${path} -> 200 (${own.length} sessions for ${user.username})`)
        json(res, 200, own.map((item) => summarize(item.session)))
        return
      }
    }

    const sessionMatch = /^\/api\/sessions\/([^/]+)$/.exec(path)
    if (sessionMatch) {
      const id = decodeURIComponent(sessionMatch[1])
      const key = `${user.id}:${id}`
      if (method === 'GET') {
        const entry = sessions.get(key)
        console.log(`[mock] GET ${path} -> ${entry ? 200 : 404}`)
        if (!entry) {
          json(res, 404, { error: { message: 'session not found' } })
          return
        }
        json(res, 200, entry.session)
        return
      }
      if (method === 'PUT') {
        const session = JSON.parse((await readBody(req)).toString('utf8') || '{}')
        if (!session.id || !Array.isArray(session.messages)) {
          json(res, 400, { error: { message: 'invalid session body' } })
          return
        }
        session.id = id
        const previous = sessions.get(key)?.session
        if (previous && previous.updatedAt > session.updatedAt) {
          console.log(`[mock] PUT ${path} -> 200 (ignored, server newer)`)
          json(res, 200, { ok: true, ignored: true })
          return
        }
        sessions.set(key, { userId: user.id, session })
        console.log(`[mock] PUT ${path} -> 200 (${user.username}, title=${session.title || '(empty)'})`)
        json(res, 200, { ok: true })
        return
      }
      if (method === 'DELETE') {
        sessions.delete(key)
        console.log(`[mock] DELETE ${path} -> 200 (${user.username})`)
        json(res, 200, { ok: true })
        return
      }
    }

    const fileMatch = /^\/api\/files\/([^/]+)$/.exec(path)
    if (fileMatch) {
      const id = decodeURIComponent(fileMatch[1])
      const key = `${user.id}:${id}`
      if (method === 'PUT') {
        const body = await readBody(req)
        files.set(key, body)
        console.log(`[mock] PUT ${path} -> 200 (${body.length} bytes)`)
        json(res, 200, { ok: true })
        return
      }
      if (method === 'GET') {
        const body = files.get(key)
        console.log(`[mock] GET ${path} -> ${body ? 200 : 404}`)
        if (!body) {
          json(res, 404, { error: { message: 'file not found' } })
          return
        }
        res.writeHead(200, { ...cors, 'Content-Type': 'application/octet-stream' })
        res.end(body)
        return
      }
    }

    console.log(`[mock] ${method} ${path} -> 404`)
    json(res, 404, { error: { message: 'not found' } })
  } catch (error) {
    console.error('[mock] error', error)
    json(res, 500, { error: { message: String(error) } })
  }
})

server.listen(port, '127.0.0.1', () => {
  console.log(`[mock] DGX Chat mock backend listening on http://127.0.0.1:${port}`)
})
