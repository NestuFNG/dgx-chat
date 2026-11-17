import { getStoredToken, handleUnauthorized } from '../auth.js'
import * as db from '../chat/db.js'
import type { Session, SessionSummary } from '../chat/types.js'
import {
  cloudGetFile,
  cloudGetSession,
  cloudListSessions,
  cloudSyncSessions,
  isNotFound,
  isUnauthorized,
} from './cloud.js'
import { isFilePushed, isSessionPushed, markFilePushed, markSessionPushed } from './markers.js'
import { cancelSessionPush, queueFilePush, queueSessionPush } from './queue.js'
import { createScopeGuard, writeForScope, type ScopeGuard } from './scope.js'

/**
 * 云端 → 本地（离线优先缓存）的拉取：
 * - 增量同步接口消费删除墓碑并把游标存进 localStorage（按账号命名空间隔离）；
 * - 列表只 upsert 摘要，侧边栏由调用方刷新；
 * - 打开会话时按需拉全文（store.ensureSessionLoaded），保存进 IndexedDB 供离线使用；
 * - 会话引用的附件在本地缺失时按 local_id 从 /api/files 补齐。
 * 所有会落盘的异步路径都带账号 scope 守卫，切换账号后到达的响应会被丢弃。
 * 不与 store 互相依赖，避免循环引用。
 */
let remoteSummaries = new Map<string, SessionSummary>()
/** 本次会话里已尝试过、但没拉到的附件，避免反复打接口 */
const filePullAttempts = new Set<string>()

const SYNC_CURSOR_PREFIX = 'dgxchat:sync-cursor:'

export const getRemoteSummary = (id: string): SessionSummary | undefined => remoteSummaries.get(id)

/** 本地是否存在比云端更新的副本；没有远端记录时返回 false（本地新会话等 push 流程） */
export const isRemoteNewer = (id: string, local: Session | undefined): boolean => {
  const remote = remoteSummaries.get(id)
  if (!remote) {
    return false
  }
  return local ? remote.updatedAt > local.updatedAt : true
}

export const clearRemoteCache = () => {
  remoteSummaries = new Map()
  filePullAttempts.clear()
}

// ---------------------------------------------------------------------------
// 增量同步：删除墓碑 + since 游标
// ---------------------------------------------------------------------------

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

const parseSummary = (value: unknown): SessionSummary | null => {
  if (!isRecord(value)) {
    return null
  }
  if (
    typeof value.id !== 'string' ||
    typeof value.createdAt !== 'number' ||
    typeof value.updatedAt !== 'number'
  ) {
    return null
  }
  return {
    id: value.id,
    title: typeof value.title === 'string' ? value.title : '',
    createdAt: value.createdAt,
    updatedAt: value.updatedAt,
  }
}

export type ParsedSessionSync = {
  sessions: SessionSummary[]
  deleted: string[]
  cursor: string | null
}

/** 解析 GET /api/sessions/sync 的响应；结构不对时抛出，由调用方回退旧接口 */
export const parseSessionSyncResponse = (value: unknown): ParsedSessionSync => {
  if (!isRecord(value)) {
    throw new Error('Invalid session sync response')
  }
  const sessions = Array.isArray(value.sessions)
    ? value.sessions.map(parseSummary).filter((item): item is SessionSummary => item !== null)
    : []
  const deleted = Array.isArray(value.deleted)
    ? value.deleted.filter((id): id is string => typeof id === 'string' && id.length > 0)
    : []
  const rawCursor = value.since ?? value.serverTime
  const cursor =
    typeof rawCursor === 'string' || typeof rawCursor === 'number' ? String(rawCursor) : null
  return { sessions, deleted, cursor }
}

/** 游标按账号 scope 分开存，避免切号后沿用别人的增量位置 */
export const getSyncCursor = (scope: string): string | null => {
  try {
    return localStorage.getItem(`${SYNC_CURSOR_PREFIX}${scope}`)
  } catch {
    return null
  }
}

export const setSyncCursor = (since: string, scope: string) => {
  try {
    localStorage.setItem(`${SYNC_CURSOR_PREFIX}${scope}`, since)
  } catch {
    // 存储不可用时退化为每次全量；删除墓碑只在首次运行时忽略，仍安全
  }
}

export type SessionSyncDeps = {
  fetchSync: (since: string | null) => Promise<unknown>
  mergeSummaries: (sessions: SessionSummary[]) => Promise<void>
  deleteSession: (id: string) => Promise<void>
  cancelPush: (id: string) => void
  getCursor: () => string | null
  setCursor: (since: string) => void
}

export type SessionSyncApplied = {
  sessions: SessionSummary[]
  deleted: string[]
  cursor: string | null
  /** 首次运行（没有游标）：拿全量列表，按约定不消费 deleted */
  full: boolean
}

/**
 * 应用一次增量同步：先删墓碑、再合并列表、最后推进游标。
 * 首次运行没有游标时忽略 deleted（避免把「服务器还没见过的本地会话」误删）。
 * 账号切换后返回 null，且不再执行任何写入。
 */
export const applySessionSync = async (
  deps: SessionSyncDeps,
  guard: ScopeGuard
): Promise<SessionSyncApplied | null> => {
  const cursor = deps.getCursor()
  const raw = await deps.fetchSync(cursor)
  if (!guard.isCurrent()) {
    return null
  }
  const parsed = parseSessionSyncResponse(raw)
  const full = cursor === null
  const deleted = full ? [] : parsed.deleted
  // 同一响应里既出现在列表又出现在墓碑中的 id，以墓碑为准，避免合并时又把摘要写回来
  const deletedIds = new Set(deleted)
  const sessions = parsed.sessions.filter((item) => !deletedIds.has(item.id))

  for (const id of deleted) {
    const applied = await writeForScope(guard, async () => {
      deps.cancelPush(id)
      await deps.deleteSession(id)
      return true
    })
    if (!applied) {
      return null
    }
  }

  const merged = await writeForScope(guard, async () => {
    await deps.mergeSummaries(sessions)
    return true
  })
  if (!merged) {
    return null
  }

  if (parsed.cursor !== null) {
    const saved = await writeForScope(guard, () => {
      deps.setCursor(parsed.cursor as string)
      return true
    })
    if (!saved) {
      return null
    }
  }

  return { sessions, deleted, cursor: parsed.cursor, full }
}

// ---------------------------------------------------------------------------
// 全量列表 / 详情 / 附件
// ---------------------------------------------------------------------------

/** 拉取会话列表并合并到 IndexedDB；未登录返回 null，网络/鉴权错误向上抛出 */
export const refreshRemoteSessions = async (): Promise<SessionSummary[] | null> => {
  const token = getStoredToken()
  if (!token) {
    return null
  }
  const guard = createScopeGuard()
  try {
    const list = await cloudListSessions(token)
    const merged = await writeForScope(guard, async () => {
      remoteSummaries = new Map(list.map((summary) => [summary.id, summary]))
      await db.mergeRemoteSummaries(list)
      return true
    })
    return merged ? list : null
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
    }
    throw error
  }
}

/** 附件本地缺失时从云端补齐；失败只记日志，界面仍可显示文件名 */
const pullSessionFiles = async (session: Session, guard: ScopeGuard) => {
  const token = getStoredToken()
  if (!token) {
    return
  }
  const ids = new Set<string>()
  for (const message of session.messages) {
    if (message.role === 'user') {
      for (const file of message.files) {
        ids.add(file.id)
      }
    }
  }
  for (const id of ids) {
    if (!guard.isCurrent()) {
      return
    }
    if (filePullAttempts.has(id) || (await db.loadFile(id))) {
      continue
    }
    filePullAttempts.add(id)
    try {
      const blob = await cloudGetFile(token, id)
      const saved = await writeForScope(guard, async () => {
        await db.saveFiles([{ id, blob }])
        markFilePushed(id)
        return true
      })
      if (!saved) {
        return
      }
    } catch (error) {
      if (isUnauthorized(error)) {
        handleUnauthorized()
        return
      }
      // eslint-disable-next-line no-console
      console.warn(`[sync] pull file ${id} failed`, error)
    }
  }
}

/**
 * 按需拉取单个会话全文并写入本地（不触发写回）。
 * 401 → 清 token 回登录页；404 → 返回 null（调用方回退本地副本）；账号已切换 → 丢弃。
 */
export const fetchRemoteSession = async (id: string): Promise<Session | null> => {
  const token = getStoredToken()
  if (!token) {
    return null
  }
  const guard = createScopeGuard()
  try {
    const session = await cloudGetSession(token, id)
    const saved = await writeForScope(guard, async () => {
      await db.saveRemoteSession(session)
      markSessionPushed(id)
      return true
    })
    if (!saved) {
      return null
    }
    void pullSessionFiles(session, guard)
    return session
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
      return null
    }
    if (isNotFound(error)) {
      return null
    }
    throw error
  }
}

const FILE_PUSH_CONCURRENCY = 2

/** 本地有原件、但从未成功上传的附件：按 local_id 幂等补传 */
const pushUnpushedFiles = async (guard: ScopeGuard) => {
  const ids = (await db.loadFileIds()).filter((id) => !isFilePushed(id))
  let cursor = 0
  const worker = async () => {
    while (cursor < ids.length) {
      if (!guard.isCurrent()) {
        return
      }
      const id = ids[cursor]
      cursor += 1
      if (!id) {
        continue
      }
      try {
        const blob = await db.loadFile(id)
        if (blob && guard.isCurrent()) {
          queueFilePush(id, blob)
        }
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn(`[sync] read local file ${id} failed`, error)
      }
    }
  }
  await Promise.all(Array.from({ length: FILE_PUSH_CONCURRENCY }, worker))
}

/**
 * 登录成功 / 启动时已登录：
 * 1. 先走增量同步接口消费删除墓碑并推进游标（旧后端不支持时回退全量接口，并保留
 *    「不传播删除」的限制）；
 * 2. 再拉全量列表用于回填：只补传从未推送过的本地会话，以及本地比云端新的会话。
 *    已经推送过、但云端没有且没有墓碑的会话不再重新上传，避免把别处的删除复活。
 * 任何阶段发现账号已切换都会停止，不再写库或入队。
 */
export const syncAfterLogin = async (): Promise<void> => {
  const token = getStoredToken()
  if (!token) {
    return
  }
  const guard = createScopeGuard()
  try {
    const applied = await applySessionSync(
      {
        fetchSync: (since) => cloudSyncSessions(token, since),
        mergeSummaries: (list) => db.mergeRemoteSummaries(list),
        deleteSession: (id) => db.deleteSession(id),
        cancelPush: (id) => cancelSessionPush(id),
        getCursor: () => getSyncCursor(guard.scope),
        setCursor: (since) => setSyncCursor(since, guard.scope),
      },
      guard
    )
    if (!applied) {
      return
    }
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
      return
    }
    if (!guard.isCurrent()) {
      return
    }
    // eslint-disable-next-line no-console
    console.warn('[sync] incremental session sync unavailable, falling back to full list', error)
  }

  if (!guard.isCurrent()) {
    return
  }
  const list = await refreshRemoteSessions()
  if (!list || !guard.isCurrent()) {
    return
  }
  const remote = new Map(list.map((summary) => [summary.id, summary]))
  for (const summary of await db.loadSessionSummaries()) {
    if (!guard.isCurrent()) {
      return
    }
    const known = remote.get(summary.id)
    if (known && known.updatedAt >= summary.updatedAt) {
      continue
    }
    if (!known && isSessionPushed(summary.id)) {
      // 推送过但云端已不存在：等墓碑处理，不要盲目回传复活
      continue
    }
    const session = await db.loadSession(summary.id)
    if (session && guard.isCurrent()) {
      queueSessionPush(session)
    }
  }
  await pushUnpushedFiles(guard)
}
