import type { Session, SessionSummary } from '../chat/types.js'
import { bearer, fetchApi, fetchApiJson, HttpError, jsonBody } from '../api/http.js'

/**
 * 自建后端（SPEC §2.1 / §2.3）的最小客户端。
 * 这里只负责「发请求 + 解析错误」，不关心本地缓存与重试 —— 那是 queue.ts / pull.ts 的事。
 * token 一律由调用方显式传入，避免和 auth.ts 形成循环依赖。
 */
export type CloudUser = {
  id: string
  username: string
  role?: string
  createdAt?: string | number
}

export type AuthResponse = { token: string; user: CloudUser }

/** 401：token 失效或未登录 */
export const isUnauthorized = (error: unknown): boolean =>
  error instanceof HttpError && error.status === 401

/** 404：资源不存在（删除 / 拉取时按幂等处理） */
export const isNotFound = (error: unknown): boolean =>
  error instanceof HttpError && error.status === 404

export const cloudLogin = (username: string, password: string): Promise<AuthResponse> =>
  fetchApiJson<AuthResponse>('/auth/login', jsonBody({ username, password }))

/**
 * 注册：后端默认 invite 模式，需要邀请码；空库首次注册（bootstrap）允许不带邀请码。
 * invite_code 始终以字符串提交（空串表示未填），由后端决定是否校验。
 */
export const cloudRegister = (
  username: string,
  password: string,
  inviteCode = ''
): Promise<AuthResponse> =>
  fetchApiJson<AuthResponse>(
    '/auth/register',
    jsonBody({ username, password, invite_code: inviteCode.trim() })
  )

export const cloudMe = (token: string): Promise<CloudUser> =>
  fetchApiJson<CloudUser>('/auth/me', { headers: bearer(token) })

/** 退出登录：后端失败不影响前端清理本地 token */
export const cloudLogout = (token: string): Promise<void> =>
  fetchApi('/auth/logout', { method: 'POST', headers: bearer(token) }).then(() => undefined)

export const cloudListSessions = (token: string): Promise<SessionSummary[]> =>
  fetchApiJson<SessionSummary[]>('/api/sessions', { headers: bearer(token) })

export const cloudGetSession = (token: string, id: string): Promise<Session> =>
  fetchApiJson<Session>(`/api/sessions/${encodeURIComponent(id)}`, { headers: bearer(token) })

/** PUT 原样提交 Session JSON（SPEC：后端不解释 messages 内部结构） */
export const cloudPutSession = async (token: string, session: Session): Promise<void> => {
  await fetchApi(`/api/sessions/${encodeURIComponent(session.id)}`, {
    method: 'PUT',
    headers: { ...bearer(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(session),
  })
}

export const cloudDeleteSession = async (token: string, id: string): Promise<void> => {
  await fetchApi(`/api/sessions/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: bearer(token),
  })
}

/** GET /api/files/{local_id}：附件的本地 id 即云端索引 */
export const cloudGetFile = async (token: string, id: string): Promise<Blob> =>
  (await fetchApi(`/api/files/${encodeURIComponent(id)}`, { headers: bearer(token) })).blob()

export const cloudPutFile = async (token: string, id: string, blob: Blob): Promise<void> => {
  await fetchApi(`/api/files/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { ...bearer(token), 'Content-Type': blob.type || 'application/octet-stream' },
    body: blob,
  })
}
