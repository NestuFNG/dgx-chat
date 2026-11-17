import { getAccountScope } from '../auth.js'

/**
 * 账号切换竞态守卫。
 *
 * 异步请求开始时记录当前账号命名空间（与 IndexedDB 分库使用的 scope 相同）；
 * 在写入 IndexedDB / 内存 store 之前必须重新确认 scope 没变，否则静默丢弃结果，
 * 避免把账号 A 的数据写进账号 B 的库。
 */
export type ScopeGuard = {
  readonly scope: string
  isCurrent(): boolean
}

export const createScopeGuard = (): ScopeGuard => {
  const scope = getAccountScope()
  return {
    scope,
    isCurrent: () => getAccountScope() === scope,
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
