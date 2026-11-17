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

/** 服务器级官方 Key（MOCK_NO_OFFICIAL=1 时视为未配置，用于测试搜索/语音缺 Key 分支） */
const SERVER_OFFICIAL_KEY =
  process.env.MOCK_NO_OFFICIAL === '1' ? '' : (process.env.MOCK_OFFICIAL_KEY ?? 'sk-server-official')
const TTS_ASR_PROVIDERS = new Set(['deepseek', 'openai', 'deepseek_tools', 'disabled'])
const SEARCH_PROVIDERS = new Set(['deepseek', 'brave', 'tavily', 'searxng', 'google_cse', 'none'])
const SEARCH_MODES = new Set(['dgx', 'official', 'off'])

/** @type {Map<string, any>} userId -> 非凭证设置 */
const userSettings = new Map()
/** @type {Map<string, {official:string,tts:string,asr:string,search:string}>} userId -> 明文 Key（只在 mock 内存里） */
const userSecrets = new Map()

const secretsFor = (userId) =>
  userSecrets.get(userId) ?? { official: '', tts: '', asr: '', search: '' }
const settingsFor = (userId) => userSettings.get(userId) ?? {}

const maskSecret = (value) =>
  value ? (value.length > 9 ? `${value.slice(0, 5)}…${value.slice(-4)}` : `${value[0]}…${value.at(-1)}`) : null

/** GET/PUT 返回的掩码文档：非凭证设置原样 + 四个凭证块 */
const publicSettings = (userId) => {
  const stored = settingsFor(userId)
  const secrets = secretsFor(userId)
  const block = (name, extra) => ({
    ...extra,
    base_url: stored[name]?.base_url ?? null,
    api_key: maskSecret(secrets[name]),
    has_key: Boolean(secrets[name]),
  })
  const doc = {}
  if (stored.model !== undefined) {
    doc.model = stored.model
  }
  doc.official = block('official', {})
  doc.tts = block('tts', {
    provider: stored.tts?.provider ?? null,
    model: stored.tts?.model ?? null,
    voice: stored.tts?.voice ?? null,
  })
  doc.asr = block('asr', {
    provider: stored.asr?.provider ?? null,
    model: stored.asr?.model ?? null,
    voice: stored.asr?.voice ?? null,
  })
  doc.search = block('search', {
    mode: stored.search?.mode ?? null,
    provider: stored.search?.provider ?? null,
    max_results: stored.search?.max_results ?? null,
  })
  return doc
}

const capabilitiesFor = (userId) => {
  const stored = settingsFor(userId)
  const secrets = secretsFor(userId)
  const officialUser = Boolean(secrets.official)
  const officialServer = Boolean(SERVER_OFFICIAL_KEY)
  const officialConfigured = officialUser || officialServer
  const speechConfigured = (kind) => {
    const provider = stored[kind]?.provider
    if (provider && provider !== 'disabled') {
      return true
    }
    return officialConfigured
  }
  const speechSource = (kind) => {
    const provider = stored[kind]?.provider
    if (provider && provider !== 'disabled') {
      return 'user'
    }
    if (officialUser) {
      return 'user'
    }
    return officialServer ? 'server' : null
  }
  return {
    official: {
      configured: officialConfigured,
      source: officialUser ? 'user' : officialServer ? 'server' : null,
    },
    tts: { configured: speechConfigured('tts'), source: speechSource('tts') },
    asr: { configured: speechConfigured('asr'), source: speechSource('asr') },
    search: { available: officialConfigured, requires_official: true },
  }
}

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

const SETTINGS_BLOCKS = ['official', 'tts', 'asr', 'search']

/**
 * 按契约应用 PUT 子集：credential 字段支持字符串 / ""|null 清除 / "__keep__" 不变 / 省略不变；
 * 非凭证顶层键整体替换；块内非凭证字段按字段合并。返回 {message,param} 表示 400。
 */
const applySettingsPatch = (userId, body) => {
  const stored = { ...(userSettings.get(userId) ?? {}) }
  const secrets = { ...secretsFor(userId) }
  const bad = (message, param) => ({ message, param })
  const checkString = (value, param) => {
    if (value === null || value === undefined) {
      return null
    }
    if (typeof value !== 'string' || value.length > 512) {
      return bad(`${param} must be a string of at most 512 characters`, param)
    }
    return null
  }

  for (const [key, value] of Object.entries(body)) {
    if (!SETTINGS_BLOCKS.includes(key)) {
      stored[key] = value
    }
  }

  for (const name of SETTINGS_BLOCKS) {
    const input = body[name]
    if (input === undefined) {
      continue
    }
    if (typeof input !== 'object' || input === null || Array.isArray(input)) {
      return bad(`${name} must be an object`, name)
    }
    if (!stored[name]) {
      stored[name] = {}
    }
    const current = { ...stored[name] }
    for (const [field, value] of Object.entries(input)) {
      const param = `${name}.${field}`
      if (field === 'api_key') {
        if (value === '__keep__') {
          continue
        }
        if (value === null || value === '') {
          secrets[name] = ''
          continue
        }
        if (typeof value !== 'string' || value.length > 512) {
          return bad('api_key must be a string of at most 512 characters', param)
        }
        secrets[name] = value
        continue
      }
      if (name === 'search' && field === 'max_results') {
        if (value === null) {
          delete current[field]
          continue
        }
        if (!Number.isInteger(value) || value < 1 || value > 20) {
          return bad('search.max_results must be an integer between 1 and 20', param)
        }
        current[field] = value
        continue
      }
      if (name === 'search' && field === 'mode') {
        if (value === null) {
          delete current[field]
          continue
        }
        if (typeof value !== 'string' || !SEARCH_MODES.has(value)) {
          return bad('invalid search.mode', param)
        }
        current[field] = value
        continue
      }
      if (name === 'search' && field === 'provider') {
        if (value === null) {
          delete current[field]
          continue
        }
        if (!SEARCH_PROVIDERS.has(value)) {
          return bad('invalid search.provider', param)
        }
        current[field] = value
        continue
      }
      if ((name === 'tts' || name === 'asr') && field === 'provider') {
        if (value === null) {
          delete current[field]
          continue
        }
        if (!TTS_ASR_PROVIDERS.has(value)) {
          return bad('invalid provider', param)
        }
        current[field] = value
        continue
      }
      const problem = checkString(value, param)
      if (problem) {
        return problem
      }
      if (value === null) {
        delete current[field]
      } else {
        current[field] = value
      }
    }
    stored[name] = current
  }

  userSettings.set(userId, stored)
  userSecrets.set(userId, secrets)
  return null
}

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
    const user = isCompat
      ? (userOf(req) ?? { id: 'compat', username: 'compat' })
      : userOf(req)
    if (!user) {
      console.log(`[mock] ${method} ${path} -> 401`)
      unauthorized(res)
      return
    }

    if (path === '/api/me/capabilities' && method === 'GET') {
      console.log(`[mock] GET ${path} -> 200 (${user.username})`)
      json(res, 200, capabilitiesFor(user.id))
      return
    }

    if (path === '/api/me/settings' && method === 'GET') {
      console.log(`[mock] GET ${path} -> 200 (${user.username})`)
      json(res, 200, publicSettings(user.id))
      return
    }

    if (path === '/api/me/settings' && method === 'PUT') {
      let body
      try {
        body = JSON.parse((await readBody(req)).toString('utf8') || '{}')
      } catch {
        json(res, 400, {
          error: { message: 'settings must be valid JSON', type: 'invalid_request_error' },
        })
        return
      }
      if (typeof body !== 'object' || body === null || Array.isArray(body)) {
        json(res, 400, {
          error: { message: 'settings must be a JSON object', type: 'invalid_request_error' },
        })
        return
      }
      const problem = applySettingsPatch(user.id, body)
      if (problem) {
        console.log(`[mock] PUT ${path} -> 400 (${problem.param})`)
        json(res, 400, {
          error: { message: problem.message, type: 'invalid_request_error', param: problem.param },
        })
        return
      }
      console.log(`[mock] PUT ${path} -> 200 (${user.username})`)
      json(res, 200, publicSettings(user.id))
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

      // 契约：官方 Key 缺失时，200 + SSE error 帧 + x-dgx-search 头
      const wantsSearch =
        Array.isArray(body.tools) &&
        body.tools.some((tool) => tool?.type === 'deepseek_web_search')
      if (wantsSearch && !capabilitiesFor(user.id).search.available) {
        console.log(`[mock] POST ${path} -> 200 SSE (official_key_required)`)
        res.writeHead(200, {
          ...cors,
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
          'x-dgx-search': 'official_key_required',
        })
        res.write(
          `event: error\ndata: ${JSON.stringify({
            type: 'error',
            error: {
              message: '联网搜索需要 DeepSeek 官方 API Key。',
              type: 'authentication_error',
              code: 'official_key_required',
            },
          })}\n\n`
        )
        res.end()
        return
      }

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
