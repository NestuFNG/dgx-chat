import { getStoredToken, handleUnauthorized } from '../auth.js'
import type { Session } from '../chat/types.js'
import {
  cloudDeleteSession,
  cloudPutFile,
  cloudPutSession,
  isNotFound,
  isUnauthorized,
} from './cloud.js'
import { markFilePushed, markSessionPushed, unmarkSessionPushed } from './markers.js'

/**
 * 云同步写穿队列：saveSession / renameSession / deleteSession 与附件上传都走这里。
 *
 * 规则：去抖合并同一资源的连续写入、失败退避重试、永不向调用方抛错（同步失败只写日志）。
 * 页面关闭时丢失的待写内容不会丢数据：下次登录/启动会拉一次远端列表，再按 updatedAt 回填本地较新的会话。
 */
const DEBOUNCE_MS = 1200
const RETRY_DELAYS = [1500, 5000, 15000]
const MAX_ATTEMPTS = 4

type Job<T> = {
  value: T
  attempts: number
  timer: ReturnType<typeof setTimeout> | null
  running: boolean
}

const sessionJobs = new Map<string, Job<Session>>()
const fileJobs = new Map<string, Job<Blob>>()
const deleteJobs = new Map<
  string,
  { attempts: number; timer: ReturnType<typeof setTimeout> | null; running: boolean }
>()

/**
 * 已请求删除的会话：不再接受新的 PUT；DELETE 会等同一 id 的在途 PUT 结束后再发，
 * 避免「先 DELETE、旧 PUT 后到」把会话在服务端复活。
 */
const deletingSessions = new Set<string>()
/** 同一会话的在途 PUT，供 DELETE 顺序等待 */
const inFlightPuts = new Map<string, Promise<void>>()

const deliveryDelay = (attempt: number): number =>
  attempt === 0 ? DEBOUNCE_MS : (RETRY_DELAYS[Math.min(attempt - 1, RETRY_DELAYS.length - 1)] ?? 15000)

const scheduleJob = <T>(
  jobs: Map<string, Job<T>>,
  id: string,
  flush: (id: string) => Promise<void>
) => {
  const job = jobs.get(id)
  if (!job || job.timer !== null || job.running) {
    return
  }
  job.timer = setTimeout(() => {
    job.timer = null
    void flush(id)
  }, deliveryDelay(job.attempts))
}

const enqueueJob = <T>(
  jobs: Map<string, Job<T>>,
  id: string,
  value: T,
  flush: (id: string) => Promise<void>
) => {
  const job = jobs.get(id)
  if (job) {
    job.value = value
  } else {
    jobs.set(id, { value, attempts: 0, timer: null, running: false })
  }
  scheduleJob(jobs, id, flush)
}

const logFailure = (what: string, id: string, attempt: number, error: unknown) => {
  // eslint-disable-next-line no-console
  console.warn(`[sync] ${what} ${id} failed (attempt ${attempt}), will retry`, error)
}

const giveUp = (what: string, id: string, error: unknown) => {
  // eslint-disable-next-line no-console
  console.error(`[sync] ${what} ${id} gave up after ${MAX_ATTEMPTS} attempts`, error)
}

/** 会话内容 / 标题变化：合并后延迟推送 */
export const queueSessionPush = (session: Session) => {
  if (deletingSessions.has(session.id)) {
    return
  }
  enqueueJob(sessionJobs, session.id, session, flushSession)
}

/**
 * 取消某个会话尚未发出的 PUT（tombstone）。
 * 删除会话时必须先调用，之后即使有新的 saveSession 也不会再推送。
 */
export const cancelSessionPush = (id: string) => {
  deletingSessions.add(id)
  sessionJobs.delete(id)
}

const flushSession = async (id: string): Promise<void> => {
  const job = sessionJobs.get(id)
  if (!job || job.running) {
    return
  }
  if (deletingSessions.has(id)) {
    sessionJobs.delete(id)
    return
  }
  const token = getStoredToken()
  if (!token) {
    // 未登录（本地模式）：等登录后由 backfill 统一上传
    sessionJobs.delete(id)
    return
  }
  job.running = true
  const value = job.value
  const request = cloudPutSession(token, value)
  inFlightPuts.set(id, request)
  try {
    await request
    markSessionPushed(id)
    if (sessionJobs.get(id) === job) {
      sessionJobs.delete(id)
    }
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
      sessionJobs.delete(id)
      return
    }
    job.attempts += 1
    if (job.attempts > MAX_ATTEMPTS) {
      sessionJobs.delete(id)
      giveUp('session', id, error)
      return
    }
    logFailure('session', id, job.attempts, error)
  } finally {
    if (inFlightPuts.get(id) === request) {
      inFlightPuts.delete(id)
    }
    job.running = false
    // 删除已排队：不重试、不再排新的 PUT
    if (deletingSessions.has(id)) {
      sessionJobs.delete(id)
      return
    }
    if (sessionJobs.get(id) === job && job.attempts > 0) {
      scheduleJob(sessionJobs, id, flushSession)
    } else if (sessionJobs.get(id) === job && job.value !== value) {
      // 运行期间内容又变了：按最新内容再排一次
      scheduleJob(sessionJobs, id, flushSession)
    }
  }
}

/** 删除会话：取消待推送 / 在途 PUT 的影响后删除远端；404 视为已删除 */
export const queueSessionDelete = (id: string) => {
  cancelSessionPush(id)
  if (deleteJobs.has(id)) {
    return
  }
  deleteJobs.set(id, { attempts: 0, timer: null, running: false })
  scheduleDelete(id)
}

const scheduleDelete = (id: string) => {
  const job = deleteJobs.get(id)
  if (!job || job.timer !== null || job.running) {
    return
  }
  job.timer = setTimeout(() => {
    job.timer = null
    void flushDelete(id)
  }, deliveryDelay(job.attempts))
}

const flushDelete = async (id: string): Promise<void> => {
  const job = deleteJobs.get(id)
  if (!job || job.running) {
    return
  }
  const token = getStoredToken()
  if (!token) {
    deleteJobs.delete(id)
    unmarkSessionPushed(id)
    return
  }
  job.running = true
  // 先等同一会话的在途 PUT 落地（成功或失败），再删除，避免旧 PUT 后到把会话复活
  const pendingPut = inFlightPuts.get(id)
  if (pendingPut) {
    try {
      await pendingPut
    } catch {
      // PUT 失败由 flushSession 记录；这里只关心顺序
    }
  }
  try {
    await cloudDeleteSession(token, id)
    deleteJobs.delete(id)
    unmarkSessionPushed(id)
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
      deleteJobs.delete(id)
      return
    }
    if (isNotFound(error)) {
      deleteJobs.delete(id)
      unmarkSessionPushed(id)
      return
    }
    job.attempts += 1
    if (job.attempts > MAX_ATTEMPTS) {
      deleteJobs.delete(id)
      giveUp('delete session', id, error)
      return
    }
    logFailure('delete session', id, job.attempts, error)
  } finally {
    job.running = false
    if (deleteJobs.get(id) === job && job.attempts > 0 && job.attempts <= MAX_ATTEMPTS) {
      scheduleDelete(id)
    }
  }
}

/** 附件上传：同一 local_id 幂等 PUT；上传成功记入 markers，避免重复上传 */
export const queueFilePush = (id: string, blob: Blob) => {
  enqueueJob(fileJobs, id, blob, flushFile)
}

const flushFile = async (id: string): Promise<void> => {
  const job = fileJobs.get(id)
  if (!job || job.running) {
    return
  }
  const token = getStoredToken()
  if (!token) {
    fileJobs.delete(id)
    return
  }
  job.running = true
  const blob = job.value
  try {
    await cloudPutFile(token, id, blob)
    markFilePushed(id)
    if (fileJobs.get(id) === job) {
      fileJobs.delete(id)
    }
  } catch (error) {
    if (isUnauthorized(error)) {
      handleUnauthorized()
      fileJobs.delete(id)
      return
    }
    job.attempts += 1
    if (job.attempts > MAX_ATTEMPTS) {
      fileJobs.delete(id)
      giveUp('file', id, error)
      return
    }
    logFailure('file', id, job.attempts, error)
  } finally {
    job.running = false
    if (fileJobs.get(id) === job && job.attempts > 0) {
      scheduleJob(fileJobs, id, flushFile)
    } else if (fileJobs.get(id) === job && job.value !== blob) {
      scheduleJob(fileJobs, id, flushFile)
    }
  }
}

/**
 * 页面隐藏 / 关闭前把手上的任务立即发出（不等待结果）。
 * 浏览器可能直接终止页面，因此这只是减少丢任务的补救，正确性依赖下次登录的回填。
 */
export const flushSyncQueue = () => {
  for (const [id, job] of sessionJobs) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
      job.timer = null
    }
    void flushSession(id)
  }
  for (const [id, job] of fileJobs) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
      job.timer = null
    }
    void flushFile(id)
  }
  for (const [id, job] of deleteJobs) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
      job.timer = null
    }
    void flushDelete(id)
  }
}

/**
 * 账号切换（登录 / 登出 / 换号）时调用：丢弃上一账号排队中的同步任务，
 * 避免用新账号的 token 把旧账号的会话 / 附件上传过去。
 * 已在途的请求无法取消，但它们开始时已捕获旧 token，落点仍是旧账号，不会串号；
 * 未发出的本地改动仍留在对应账号自己的 IndexedDB 里，下次登录该账号时会重新回填。
 */
export const resetSyncQueue = () => {
  for (const job of sessionJobs.values()) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
    }
  }
  for (const job of fileJobs.values()) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
    }
  }
  for (const job of deleteJobs.values()) {
    if (job.timer !== null) {
      clearTimeout(job.timer)
    }
  }
  sessionJobs.clear()
  fileJobs.clear()
  deleteJobs.clear()
  deletingSessions.clear()
  inFlightPuts.clear()
}
