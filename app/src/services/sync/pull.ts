import { getStoredToken, handleUnauthorized } from '../auth.js'
import * as db from '../chat/db.js'
import type { Session, SessionSummary } from '../chat/types.js'
import { cloudGetFile, cloudGetSession, cloudListSessions, isNotFound, isUnauthorized } from './cloud.js'
import { isFilePushed, markFilePushed, markSessionPushed } from './markers.js'
import { queueFilePush, queueSessionPush } from './queue.js'

/**
 * 云端 → 本地（离线优先缓存）的拉取：
 * - 列表只 upsert 摘要，侧边栏由调用方刷新；
 * - 打开会话时按需拉全文（store.ensureSessionLoaded），保存进 IndexedDB 供离线使用；
 * - 会话引用的附件在本地缺失时按 local_id 从 /api/files 补齐。
 * 不与 store 互相依赖，避免循环引用。
 */
let remoteSummaries = new Map<string, SessionSummary>()
/** 本次会话里已尝试过、但没拉到的附件，避免反复打接口 */
const filePullAttempts = new Set<string>()

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

/** 拉取会话列表并合并到 IndexedDB；未登录返回 null，网络/鉴权错误向上抛出 */
export const refreshRemoteSessions = async (): Promise<SessionSummary[] | null> => {
  const token = getStoredToken()
  if (!token) {
    return null
  }
  try {
    const list = await cloudListSessions(token)
    remoteSummaries = new Map(list.map((summary) => [summary.id, summary]))
    await db.mergeRemoteSummaries(list)
    return list
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
    }
    throw error
  }
}

/** 附件本地缺失时从云端补齐；失败只记日志，界面仍可显示文件名 */
const pullSessionFiles = async (session: Session) => {
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
    if (filePullAttempts.has(id) || (await db.loadFile(id))) {
      continue
    }
    filePullAttempts.add(id)
    try {
      const blob = await cloudGetFile(token, id)
      await db.saveFiles([{ id, blob }])
      markFilePushed(id)
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
 * 401 → 清 token 回登录页；404 → 返回 null（调用方回退本地副本）。
 */
export const fetchRemoteSession = async (id: string): Promise<Session | null> => {
  const token = getStoredToken()
  if (!token) {
    return null
  }
  try {
    const session = await cloudGetSession(token, id)
    await db.saveRemoteSession(session)
    markSessionPushed(id)
    void pullSessionFiles(session)
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
const pushUnpushedFiles = async () => {
  const ids = (await db.loadFileIds()).filter((id) => !isFilePushed(id))
  let cursor = 0
  const worker = async () => {
    while (cursor < ids.length) {
      const id = ids[cursor]
      cursor += 1
      if (!id) {
        continue
      }
      try {
        const blob = await db.loadFile(id)
        if (blob) {
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
 * 登录成功 / 启动时已登录：先拉远端列表（合并摘要），再把本地比远端新的会话与未上传附件排队推送。
 * 远端列表拉取失败时不做回填，避免把过期副本覆盖到服务器。
 */
export const syncAfterLogin = async (): Promise<void> => {
  const list = await refreshRemoteSessions()
  if (!list) {
    return
  }
  const remote = new Map(list.map((summary) => [summary.id, summary]))
  for (const summary of await db.loadSessionSummaries()) {
    const known = remote.get(summary.id)
    if (known && known.updatedAt >= summary.updatedAt) {
      continue
    }
    const session = await db.loadSession(summary.id)
    if (session) {
      queueSessionPush(session)
    }
  }
  await pushUnpushedFiles()
}
