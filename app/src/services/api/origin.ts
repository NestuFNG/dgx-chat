/**
 * API 根地址（运行期可配置）。
 *
 * 解析顺序：localStorage['dgxchat:origin'] → 构建期默认值 __API_ORIGIN__（见 rspack.config.ts）。
 * 所有发请求的模块都必须在「每次请求时」调用 getApiOrigin()，不要在模块顶层把它拼成常量，
 * 否则设置面板改地址后旧模块仍会指向老服务器。
 */

/** 设置面板 / 登录页保存服务器地址用的 localStorage key */
export const ORIGIN_STORAGE_KEY = 'dgxchat:origin'

/**
 * 规范化用户输入的地址：只接受 http(s) URL，去掉末尾斜杠，忽略 query 与 hash。
 * 无效（含空串）时返回 null，调用方自行决定回退或报错。
 */
export const normalizeOrigin = (value: string): string | null => {
  const trimmed = value.trim()
  if (!trimmed) {
    return null
  }
  let url: URL
  try {
    url = new URL(trimmed)
  } catch {
    return null
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    return null
  }
  if (!url.hostname) {
    return null
  }
  const path = url.pathname.replace(/\/+$/, '')
  return `${url.origin}${path}`
}

/** 构建期默认地址；构建配置里默认注入 http://localhost:8439 */
const BUILD_ORIGIN = normalizeOrigin(__API_ORIGIN__) ?? 'http://localhost:8439'

const readStoredOrigin = (): string | null => {
  try {
    return localStorage.getItem(ORIGIN_STORAGE_KEY)
  } catch {
    return null
  }
}

/** 当前生效的 API 根地址；每次调用都重新读取，设置面板改完立即对后续请求生效 */
export const getApiOrigin = (): string => {
  const stored = readStoredOrigin()
  return (stored ? normalizeOrigin(stored) : null) ?? BUILD_ORIGIN
}

/** 保存服务器地址；传 null 恢复构建期默认值，返回保存后的生效地址 */
export const setApiOrigin = (value: string | null): string => {
  const normalized = value === null ? null : normalizeOrigin(value)
  try {
    if (normalized === null) {
      localStorage.removeItem(ORIGIN_STORAGE_KEY)
    } else {
      localStorage.setItem(ORIGIN_STORAGE_KEY, normalized)
    }
  } catch {
    // 存储不可用时本次会话仍返回规范化结果
  }
  return normalized ?? BUILD_ORIGIN
}

/** 地址是否与构建期默认值不同（设置面板用来提示「已自定义」） */
export const isCustomOrigin = (): boolean => getApiOrigin() !== BUILD_ORIGIN
