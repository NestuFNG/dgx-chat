import 'fake-indexeddb/auto'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Session, SessionSummary } from '../src/services/chat/types.js'
import type { SessionSyncDeps } from '../src/services/sync/pull.js'

/**
 * 同步层回归测试：
 * 1. 账号切换竞态：A 账号的响应不能在切到 B 之后落盘；
 * 2. 队列版本：在途 PUT 期间入队的新内容必须继续发送；
 * 3. 队列归属账号：A 保存的任务切到 B 后必须丢弃，且旧账号的 401 不能登出新账号；
 * 4. 删除墓碑：首次运行忽略 deleted，增量运行消费 deleted 并按 serverTime 推进游标，
 *    墓碑删除不能反向通知服务器；用户主动删除仍然只发一次 DELETE。
 *
 * 运行：pnpm test（tsc 编译到 .test-build 后交给 node --test）
 */

class MemoryStorage implements Storage {
  private readonly data = new Map<string, string>()

  get length(): number {
    return this.data.size
  }

  clear(): void {
    this.data.clear()
  }

  getItem(key: string): string | null {
    return this.data.get(key) ?? null
  }

  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null
  }

  removeItem(key: string): void {
    this.data.delete(key)
  }

  setItem(key: string, value: string): void {
    this.data.set(key, String(value))
  }
}

const storage = new MemoryStorage()
Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true })
// origin.ts 在模块初始化时读取构建期注入的地址
;(globalThis as unknown as { __API_ORIGIN__: string }).__API_ORIGIN__ = ''
storage.setItem('dgxchat:origin', 'http://sync.test')

const setAccount = (id: string, token: string) => {
  storage.setItem('dgxchat:token', token)
  storage.setItem('dgxchat:user', JSON.stringify({ id, username: id }))
  return `user-${id}`
}

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const session = (id: string, title: string): Session => ({
  id,
  title,
  createdAt: 1,
  updatedAt: 2,
  messages: [],
  rootMessageId: null,
})

const summary = (id: string): SessionSummary => ({
  id,
  title: id,
  createdAt: 1,
  updatedAt: 2,
})

const tick = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

const waitFor = async (condition: () => boolean, timeoutMs: number) => {
  const start = Date.now()
  while (!condition()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error('Timed out waiting for condition')
    }
    await new Promise<void>((resolve) => setTimeout(resolve, 25))
  }
}

type FetchCall = { url: string; method: string; token: string | undefined; body?: string }

/** 安装可观测的 fetch 桩；handler 返回 Response */
const installFetch = (
  handler: (url: string, init: RequestInit | undefined, index: number) => Promise<Response>
) => {
  const calls: FetchCall[] = []
  globalThis.fetch = ((url: string | URL | Request, init?: RequestInit) => {
    const headers = (init?.headers ?? {}) as Record<string, string>
    calls.push({
      url: String(url),
      method: init?.method ?? 'GET',
      token: headers.Authorization ?? headers['x-api-key'],
      body: typeof init?.body === 'string' ? init.body : undefined,
    })
    return handler(String(url), init, calls.length - 1)
  }) as typeof fetch
  return calls
}

const { createScopeGuard } = await import('../src/services/sync/scope.js')
const queue = await import('../src/services/sync/queue.js')
const pull = await import('../src/services/sync/pull.js')
const db = await import('../src/services/chat/db.js')

test('账号切换后返回的会话详情不会写入本地', async () => {
  setAccount('race1', 'token-a')
  let resolveFetch: ((response: Response) => void) | undefined
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })) as typeof fetch

  const promise = pull.fetchRemoteSession('session-a')
  await tick()
  setAccount('race2', 'token-b')
  resolveFetch?.(jsonResponse(session('session-a', 'A 的会话')))

  const result = await promise
  assert.equal(result, null, '切换账号后应丢弃结果')
})

test('在途 PUT 期间入队的新版本仍会发送', async () => {
  const scope = setAccount('rev1', 'token-a')
  queue.resetSyncQueue()
  const bodies: string[] = []
  let releaseFirst: (() => void) | undefined
  installFetch((_url, init) => {
    bodies.push(String(init?.body))
    if (bodies.length === 1) {
      return new Promise<Response>((resolve) => {
        releaseFirst = () => resolve(jsonResponse({ ok: true }))
      })
    }
    return Promise.resolve(jsonResponse({ ok: true }))
  })

  queue.queueSessionPush(session('rev-session', 'v1'), scope)
  await waitFor(() => bodies.length === 1, 3000)
  assert.equal((JSON.parse(bodies[0] ?? '{}') as Session).title, 'v1')

  queue.queueSessionPush(session('rev-session', 'v2'), scope)
  releaseFirst?.()

  await waitFor(() => bodies.length === 2, 6000)
  assert.equal((JSON.parse(bodies[1] ?? '{}') as Session).title, 'v2')
  queue.resetSyncQueue()
})

test('A 账号保存的任务在切到 B 后不会发送', async () => {
  const scope = setAccount('owner1', 'token-a')
  queue.resetSyncQueue()
  const calls = installFetch(async () => jsonResponse({ ok: true }))

  await db.saveSession(session('owner-session', 'A 的内容'))
  assert.equal(scope, 'user-owner1')

  setAccount('owner2', 'token-b')
  queue.flushSyncQueue()
  await tick()

  assert.equal(calls.length, 0, '不能把 A 的内容用 B 的 token 上传')
  queue.resetSyncQueue()
})

test('旧账号请求的 401 不会登出新账号', async () => {
  const scope = setAccount('stale1', 'token-a')
  queue.resetSyncQueue()
  let releaseFetch: (() => void) | undefined
  installFetch(
    () =>
      new Promise<Response>((resolve) => {
        releaseFetch = () =>
          resolve(jsonResponse({ error: { message: 'token expired' } }, 401))
      })
  )

  queue.queueSessionPush(session('stale-session', 'A'), scope)
  await waitFor(() => releaseFetch !== undefined, 3000)

  setAccount('stale2', 'token-b')
  releaseFetch?.()
  await tick()
  await tick()

  assert.equal(storage.getItem('dgxchat:token'), 'token-b', 'B 的登录态必须保留')
  queue.resetSyncQueue()
})

const makeSyncDeps = (response: unknown, initialCursor: string | null) => {
  const state = {
    deleted: [] as string[],
    cancelled: [] as string[],
    merged: [] as string[],
    cursor: initialCursor as string | null,
  }
  const deps = {
    fetchSync: async () => response,
    mergeSummaries: async (sessions: SessionSummary[]) => {
      state.merged = sessions.map((item) => item.id)
    },
    deleteSession: async (id: string) => {
      state.deleted.push(id)
    },
    cancelPush: (id: string) => {
      state.cancelled.push(id)
    },
    getCursor: () => state.cursor,
    setCursor: (since: string) => {
      state.cursor = since
    },
  } satisfies SessionSyncDeps
  return { state, deps }
}

test('同一账号换 token 后，旧守卫失效（旧 401 不应踢掉新登录）', () => {
  // scope 只认 user id，所以登出再登入同一账号时 scope 不变；把凭证也纳入比较
  // 才能让旧 token 的在途请求作废。
  setAccount('same-user', 'old-token')
  const guard = createScopeGuard()
  assert.equal(guard.isCurrent(), true)

  storage.setItem('dgxchat:token', 'new-token') // 同一账号换了新凭证
  assert.equal(guard.isCurrent(), false, '换 token 之后旧守卫必须失效')
})

test('首次同步（无游标）也应用删除墓碑，并记录 serverTime', async () => {
  // 服务器只为它实际存在过的会话留墓碑，所以首次同步应用它们是安全的；
  // 忽略它们会让升级后的旧设备永远收不到那次删除通知。
  setAccount('cursor1', 'token-a')
  const { state, deps } = makeSyncDeps(
    { sessions: [summary('keep')], deleted: ['gone'], since: '0', serverTime: '100' },
    null
  )
  const applied = await pull.applySessionSync(deps, createScopeGuard())
  assert.ok(applied)
  assert.equal(applied.full, true)
  assert.deepEqual(applied.deleted, ['gone'], '首次同步也要消费 deleted')
  assert.deepEqual(state.cancelled, ['gone'], '墓碑要先取消待推送')
  assert.deepEqual(state.deleted, ['gone'], '本地副本必须删掉')
  assert.deepEqual(state.merged, ['keep'], '墓碑中的 id 不能从列表合并回来')
  assert.equal(state.cursor, '100')
  assert.deepEqual(state.cancelled, ['gone'], '墓碑只清本地，不产生出站 DELETE')
})

test('增量同步的游标取 serverTime，忽略回显的 since', async () => {
  setAccount('cursor2', 'token-a')
  const { state, deps } = makeSyncDeps(
    {
      sessions: [summary('keep'), summary('gone')],
      deleted: ['gone'],
      // 后端会把请求里的 since 原样回显；若客户端用它做游标就会永远停在 "0"
      since: '0',
      serverTime: '1700000000123',
    },
    '0'
  )
  const applied = await pull.applySessionSync(deps, createScopeGuard())
  assert.ok(applied)
  assert.deepEqual(applied.deleted, ['gone'])
  assert.deepEqual(state.cancelled, ['gone'])
  assert.deepEqual(state.deleted, ['gone'])
  assert.deepEqual(state.merged, ['keep'], '墓碑中的 id 不能再从列表合并回来')
  assert.equal(state.cursor, '1700000000123', '游标必须前进到 serverTime')
})

test('同步请求返回前切换账号：丢弃墓碑与游标写入', async () => {
  setAccount('race3', 'token-a')
  let releaseFetch: ((value: unknown) => void) | undefined
  const state = {
    deleted: [] as string[],
    cancelled: [] as string[],
    merged: [] as string[],
    cursor: '50' as string | null,
  }
  const deps: SessionSyncDeps = {
    fetchSync: () =>
      new Promise<unknown>((resolve) => {
        releaseFetch = resolve
      }),
    mergeSummaries: async (sessions) => {
      state.merged = sessions.map((item) => item.id)
    },
    deleteSession: async (id) => {
      state.deleted.push(id)
    },
    cancelPush: (id) => {
      state.cancelled.push(id)
    },
    getCursor: () => state.cursor,
    setCursor: (since) => {
      state.cursor = since
    },
  }

  const promise = pull.applySessionSync(deps, createScopeGuard())
  await tick()
  setAccount('race4', 'token-b')
  releaseFetch?.({
    sessions: [summary('keep')],
    deleted: ['gone'],
    since: '0',
    serverTime: '300',
  })

  const applied = await promise
  assert.equal(applied, null)
  assert.deepEqual(state.deleted, [])
  assert.deepEqual(state.cancelled, [])
  assert.deepEqual(state.merged, [])
  assert.equal(state.cursor, '50', '账号切换后不能推进游标')
})

test('消费墓碑只删本地，不会向服务器发 DELETE', async () => {
  const scope = setAccount('tomb1', 'token-a')
  queue.resetSyncQueue()
  await db.saveSession(session('tomb-session', '将被远端删除'))
  queue.resetSyncQueue()
  storage.setItem(`dgxchat:sync-cursor:${scope}`, '400')

  const calls = installFetch(async (url) => {
    if (url.includes('/api/sessions/sync')) {
      return jsonResponse({
        sessions: [],
        deleted: ['tomb-session'],
        since: '400',
        serverTime: '500',
      })
    }
    if (url.includes('/api/sessions')) {
      return jsonResponse([])
    }
    return jsonResponse({ ok: true })
  })

  await pull.syncAfterLogin()
  // 若墓碑处理错误地走了 db.deleteSession，DELETE 会排在队列里；强制冲刷以证明它不存在
  queue.flushSyncQueue()
  await tick()

  const deletes = calls.filter((call) => call.method === 'DELETE')
  assert.equal(deletes.length, 0, '墓碑是服务端发来的，不能再回发 DELETE')
  assert.equal(await db.loadSession('tomb-session'), undefined, '本地记录应被删除')
  assert.equal(storage.getItem(`dgxchat:sync-cursor:${scope}`), '500')
  queue.resetSyncQueue()
})

test('用户主动删除仍然只向服务器发一次 DELETE', async () => {
  const scope = setAccount('userdel1', 'token-a')
  queue.resetSyncQueue()
  await db.saveSession(session('user-del', '用户删除'))
  queue.resetSyncQueue()
  assert.equal(scope, 'user-userdel1')

  const calls = installFetch(async () => jsonResponse({ ok: true }))
  await db.deleteSession('user-del')
  queue.flushSyncQueue()
  await waitFor(() => calls.some((call) => call.method === 'DELETE'), 3000)
  await tick()

  const deletes = calls.filter((call) => call.method === 'DELETE')
  assert.equal(deletes.length, 1, '用户删除应通知服务器恰好一次')
  assert.equal(await db.loadSession('user-del'), undefined)
  queue.resetSyncQueue()
})
