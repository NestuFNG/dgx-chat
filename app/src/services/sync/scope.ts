import { getAccountScope, getStoredToken } from '../auth.js'

/**
 * 账号切换竞态守卫。
 *
 * 异步请求开始时记录当前账号命名空间（与 IndexedDB 分库使用的 scope 相同）**以及
 * 当时的登录凭证**；在写入 IndexedDB / 内存 store 之前必须重新确认两者都没变，
 * 否则静默丢弃结果。
 *
 * 为什么还要比 token：同一个账号可能登出再登录（换了一个新 token），此时 scope
 * 完全相同，但旧 token 的请求返回 401 时不应把新 token 一并清掉。把凭证纳入
 * 比较即可把「登录世代」也纳进来。
 */
export type ScopeGuard = {
  readonly scope: string
  isCurrent(): boolean
}

const currentToken = (): string | null => {
  try {
    return getStoredToken()
  } catch {
    return null
  }
}

export const createScopeGuard = (): ScopeGuard => {
  const scope = getAccountScope()
  const token = currentToken()
  return {
    scope,
    isCurrent: () => getAccountScope() === scope && currentToken() === token,
  }
}

/** 账号已切换时直接丢弃；否则执行写入并返回结果 */
export const writeForScope = async <T>(
  guard: ScopeGuard,
  write: () => T | Promise<T>
): Promise<T | null> => {
  if (!guard.isCurrent()) {
    return null
  }
  return write()
}

/** 同步写入（zustand setState 等）前的守卫 */
export const isScopeCurrent = (guard: ScopeGuard): boolean => guard.isCurrent()
