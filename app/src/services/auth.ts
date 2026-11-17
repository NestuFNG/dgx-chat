import { create } from 'zustand'
import { isUnauthorized, cloudLogin, cloudLogout, cloudMe, cloudRegister, type CloudUser } from './sync/cloud.js'
import { HttpError } from './api/http.js'
import { useSettings } from './settings.js'

/**
 * 云端账号状态。token 存在 localStorage['dgxchat:token']（区别于直连模式遗留的
 * chat-demo:api-key），聊天/上传请求优先使用它；没有 token 时才回退到 API Key。
 */
export const TOKEN_STORAGE_KEY = 'dgxchat:token'
/** 显式跳过登录（只用 API Key 直连）；登录成功后自动清除 */
export const SKIP_LOGIN_STORAGE_KEY = 'dgxchat:skip-login'
const USER_STORAGE_KEY = 'dgxchat:user'

/** idle：没有 token；checking：正在调 /auth/me；connected / failed：最近一次校验结果 */
export type ConnectionStatus = 'idle' | 'checking' | 'connected' | 'failed'

export type AuthState = {
  token: string | null
  user: CloudUser | null
  /** 用户选择「只用 API Key」时不显示登录门 */
  skipLogin: boolean
  status: ConnectionStatus
  error: string | null
}

const read = (key: string): string | null => {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

const write = (key: string, value: string | null) => {
  try {
    if (value === null) {
      localStorage.removeItem(key)
    } else {
      localStorage.setItem(key, value)
    }
  } catch {
    // 存储不可用时退化为仅本次会话有效
  }
}

const readUser = (): CloudUser | null => {
  const raw = read(USER_STORAGE_KEY)
  if (!raw) {
    return null
  }
  try {
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed === 'object' && parsed !== null && 'username' in parsed) {
      return parsed as CloudUser
    }
  } catch {
    // 忽略损坏的缓存
  }
  return null
}

export const useAuth = create<AuthState>(() => {
  const token = read(TOKEN_STORAGE_KEY)
  return {
    token,
    user: token ? readUser() : null,
    skipLogin: read(SKIP_LOGIN_STORAGE_KEY) === '1',
    status: 'idle',
    error: null,
  }
})

/** 同步读取 token，供 fetch 前的请求头构造使用（不能依赖 React 渲染时序） */
export const getStoredToken = (): string | null => {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

const persistToken = (token: string | null) => {
  write(TOKEN_STORAGE_KEY, token)
  useAuth.setState({ token })
}

const persistUser = (user: CloudUser | null) => {
  write(USER_STORAGE_KEY, user ? JSON.stringify(user) : null)
  useAuth.setState({ user })
}

/**
 * 聊天/上传接口使用的凭证：已登录用 token，否则用设置里的 API Key（直连 DeepSeek 兼容端点）。
 * 每次调用时读取，登录状态或 Key 变化后立即生效。
 */
export const getCredential = (): string =>
  getStoredToken() ?? useSettings.getState().apiKey ?? ''

/** 是否有可用于发请求的凭证 */
export const hasCredential = (): boolean => getCredential().length > 0

/**
 * 账号隔离用的本地存储命名空间：
 * - 未登录（直连 API Key 模式）：'local'
 * - 已登录：'user-<id>'
 * - 有 token 但还没拿到用户信息（例如手动粘贴 token 且服务器暂时不可达）：'pending'
 * IndexedDB 按它分库（见 chat/db.ts），不同账号的会话与附件互不可见。
 */
export const getAccountScope = (): string => {
  if (!getStoredToken()) {
    return 'local'
  }
  const user = readUser()
  return user?.id ? `user-${user.id}` : 'pending'
}

/** React 侧订阅当前账号命名空间，用于在登录 / 登出时重置内存态与重新拉取 */
export const useAccountScope = (): string =>
  useAuth((state) => {
    if (!state.token) {
      return 'local'
    }
    return state.user?.id ? `user-${state.user.id}` : 'pending'
  })

export const setSkipLogin = (skip: boolean) => {
  write(SKIP_LOGIN_STORAGE_KEY, skip ? '1' : null)
  useAuth.setState({ skipLogin: skip })
}

const applyAuthResponse = (result: { token: string; user: CloudUser }) => {
  persistToken(result.token)
  persistUser(result.user)
  write(SKIP_LOGIN_STORAGE_KEY, null)
  useAuth.setState({ skipLogin: false, status: 'connected', error: null })
}

/** 登录成功后 token 已写入 localStorage，调用方负责触发一次云同步 */
export const login = async (username: string, password: string): Promise<CloudUser> => {
  const result = await cloudLogin(username.trim(), password)
  applyAuthResponse(result)
  return result.user
}

/**
 * 空库时第一个注册者自动成为管理员（后端行为），成功即已登录。
 * 后端默认邀请制：inviteCode 由注册表单传入，bootstrap 阶段可留空。
 */
export const register = async (
  username: string,
  password: string,
  inviteCode?: string
): Promise<CloudUser> => {
  const result = await cloudRegister(username.trim(), password, inviteCode?.trim())
  applyAuthResponse(result)
  return result.user
}

/** 清空本地登录态（不请求后端） */
const clearAuth = () => {
  write(TOKEN_STORAGE_KEY, null)
  write(USER_STORAGE_KEY, null)
  useAuth.setState({ token: null, user: null, status: 'idle', error: null })
}

/**
 * GET /auth/me 校验当前 token 并更新连接状态。
 * - 401：token 失效 → 清空 token（界面回到登录门）
 * - 网络错误：保留 token 与本地数据，只标记「连接失败」，应用照常离线可用
 */
export const checkConnection = async (): Promise<boolean> => {
  const token = getStoredToken()
  if (!token) {
    useAuth.setState({ status: 'idle', error: null })
    return false
  }
  useAuth.setState({ status: 'checking', error: null })
  try {
    const user = await cloudMe(token)
    persistUser(user)
    useAuth.setState({ status: 'connected', error: null })
    return true
  } catch (error) {
    if (isUnauthorized(error)) {
      clearAuth()
      return false
    }
    useAuth.setState({
      status: 'failed',
      error: error instanceof Error ? error.message : String(error),
    })
    return false
  }
}

let checking: Promise<boolean> | null = null

/** 启动时校验一次登录态；并发调用复用同一请求 */
export const initAuth = (): Promise<boolean> => {
  if (!getStoredToken()) {
    return Promise.resolve(false)
  }
  return (checking ??= checkConnection().finally(() => {
    checking = null
  }))
}

/** 退出登录：本地立即清理，后端调用失败不影响结果 */
export const logout = () => {
  const token = getStoredToken()
  clearAuth()
  useAuth.setState({ skipLogin: false })
  write(SKIP_LOGIN_STORAGE_KEY, null)
  if (token) {
    void cloudLogout(token).catch(() => {
      // 后端不可用时忽略：本地凭证已清除
    })
  }
}

/**
 * 后端返回 401 时统一降级：清空 token，界面据此回到登录页。
 * 只有确实持有 token 时才动作，避免直连模式下 API Key 无效时误触发登录门。
 */
export const handleUnauthorized = () => {
  if (!getStoredToken()) {
    return
  }
  // eslint-disable-next-line no-console
  console.warn('[auth] token rejected by server, returning to login')
  clearAuth()
}

/** 校验输入的 token 是否可用（设置面板手动粘贴 token 时用） */
export const verifyToken = async (token: string): Promise<CloudUser> => {
  const trimmed = token.trim()
  if (!trimmed) {
    throw new Error('Token is empty')
  }
  return cloudMe(trimmed)
}

/**
 * 设置面板手动粘贴 token：写入本地并视为已登录。
 * 可选 status 用于「服务器暂时连不上但仍想保存 token」的场景。
 */
export const adoptToken = (
  token: string,
  user: CloudUser | null,
  status: ConnectionStatus = 'connected'
) => {
  persistToken(token.trim())
  if (user) {
    persistUser(user)
  }
  write(SKIP_LOGIN_STORAGE_KEY, null)
  useAuth.setState({ skipLogin: false, status, error: null })
}

export const isNetworkError = (error: unknown): boolean =>
  error instanceof HttpError && error.kind === 'network'
