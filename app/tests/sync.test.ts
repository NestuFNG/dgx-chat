import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Session, SessionSummary } from '../src/services/chat/types.js'
import type { SessionSyncDeps } from '../src/services/sync/pull.js'

/**
 * 同步层回归测试：
 * 1. 账号切换竞态：A 账号的响应不能在切到 B 之后落盘；
 * 2. 队列版本：在途 PUT 期间入队的新内容必须继续发送；
 * 3. 删除墓碑：首次运行忽略 deleted，增量运行消费 deleted 并推进游标。
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
// db.ts 在导入时检查旧版数据库，给一个最小 indexedDB 桩；测试里守卫生效时不会真正打开库
;(globalThis as unknown as { indexedDB: { databases: () => Promise<unknown[]> } }).indexedDB = {
  databases: async () => [],
}
storage.setItem('dgxchat:origin', 'http://sync.test')

const setAccount = (id: string, token: string) => {
  storage.setItem('dgxchat:token', token)
  storage.setItem('dgxchat:user', JSON.stringify({ id, username: id }))
}

const jsonResponse = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
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

const { createScopeGuard } = await import('../src/services/sync/scope.js')
const queue = await import('../src/services/sync/queue.js')
const pull = await import('../src/services/sync/pull.js')

test('账号切换后返回的会话详情不会写入本地', async () => {
  setAccount('u1', 'token-a')
  let resolveFetch: ((response: Response) => void) | undefined
  globalThis.fetch = (() =>
    new Promise<Response>((resolve) => {
      resolveFetch = resolve
    })) as typeof fetch

  const promise = pull.fetchRemoteSession('session-a')
  await tick()
  // 请求尚未返回时切到账号 B
  setAccount('u2', 'token-b')
  resolveFetch?.(jsonResponse(session('session-a', 'A 的会话')))

  const result = await promise
  assert.equal(result, null, '切换账号后应丢弃结果')
})

test('在途 PUT 期间入队的新版本仍会发送', async () => {
  setAccount('u1', 'token-a')
  queue.resetSyncQueue()
  const bodies: Session[] = []
  let releaseFirst: (() => void) | undefined
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)) as Session)
    if (bodies.length === 1) {
      return new Promise<Response>((resolve) => {
        releaseFirst = () => resolve(jsonResponse({ ok: true }))
      })
    }
    return Promise.resolve(jsonResponse({ ok: true }))
  }) as typeof fetch

  queue.queueSessionPush(session('session-1', 'v1'))
  await waitFor(() => bodies.length === 1, 3000)
  assert.equal(bodies[0]?.title, 'v1')

  // 第一次 PUT 还在途时内容更新为 v2
  queue.queueSessionPush(session('session-1', 'v2'))
  releaseFirst?.()

  await waitFor(() => bodies.length === 2, 6000)
  assert.equal(bodies[1]?.title, 'v2', 'v1 完成后必须继续发送 v2')
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

test('首次同步（无游标）忽略删除墓碑并记录游标', async () => {
  setAccount('u1', 'token-a')
  const { state, deps } = makeSyncDeps(
    { sessions: [summary('keep')], deleted: ['gone'], since: '100' },
    null
  )
  const applied = await pull.applySessionSync(deps, createScopeGuard())
  assert.ok(applied)
  assert.equal(applied.full, true)
  assert.deepEqual(applied.deleted, [])
  assert.deepEqual(state.deleted, [], '首次运行不能消费 deleted')
  assert.deepEqual(state.merged, ['keep'])
  assert.equal(state.cursor, '100')
})

test('增量同步消费墓碑、取消推送并推进游标', async () => {
  setAccount('u1', 'token-a')
  const { state, deps } = makeSyncDeps(
    { sessions: [summary('keep'), summary('gone')], deleted: ['gone'], since: '200' },
    '100'
  )
  const applied = await pull.applySessionSync(deps, createScopeGuard())
  assert.ok(applied)
  assert.equal(applied.full, false)
  assert.deepEqual(applied.deleted, ['gone'])
  assert.deepEqual(state.cancelled, ['gone'])
  assert.deepEqual(state.deleted, ['gone'])
  assert.deepEqual(state.merged, ['keep'], '墓碑中的 id 不能再从列表合并回来')
  assert.equal(state.cursor, '200')
})

test('同步请求返回前切换账号：丢弃墓碑与游标写入', async () => {
  setAccount('u1', 'token-a')
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
  setAccount('u2', 'token-b')
  releaseFetch?.({ sessions: [summary('keep')], deleted: ['gone'], since: '300' })

  const applied = await promise
  assert.equal(applied, null)
  assert.deepEqual(state.deleted, [])
  assert.deepEqual(state.cancelled, [])
  assert.deepEqual(state.merged, [])
  assert.equal(state.cursor, '50', '账号切换后不能推进游标')
})
