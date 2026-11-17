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

/**
 * 构建期默认地址。分发包默认不内置任何服务器地址，此时为空字符串；
 * 自托管构建可以用 DGX_API_ORIGIN 把地址烧进来。这里不再有任何硬编码回退主机。
 */
const BUILD_ORIGIN = normalizeOrigin(__API_ORIGIN__) ?? ''

/** 构建期默认地址（可能为空），供设置面板的「恢复默认」使用 */
export const getBuildApiOrigin = (): string => BUILD_ORIGIN

const readStoredOrigin = (): string | null => {
  try {
    return localStorage.getItem(ORIGIN_STORAGE_KEY)
  } catch {
    return null
  }
}

/**
 * 是否运行在 Capacitor 原生壳（APK）里。
 * Capacitor 会注入 window.Capacitor；纯 Web 环境要么没有该对象，要么 isNativePlatform() 为 false。
 * 原生壳内不允许同源回退（WebView 的 location.origin 通常是 capacitor://localhost 之类）。
 */
const isNativePlatform = (): boolean => {
  try {
    const capacitor = (globalThis as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor
    return typeof capacitor?.isNativePlatform === 'function' && capacitor.isNativePlatform() === true
  } catch {
    return false
  }
}

/**
 * 浏览器同源回退：Web 部署时页面本身就由后端提供（http://<host>:8439/），
 * 打开页面即可把当前 origin 当作服务器地址。file:// 等非 http(s) 页面不适用。
 */
const sameOriginFallback = (): string => {
  if (isNativePlatform() || typeof window === 'undefined') {
    return ''
  }
  try {
    const { protocol, origin } = window.location
    if (protocol !== 'http:' && protocol !== 'https:') {
      return ''
    }
    return normalizeOrigin(origin) ?? ''
  } catch {
    return ''
  }
}

/** 没有保存值时使用的默认地址：构建期地址 → 浏览器同源 → 空（原生壳内没有同源回退） */
const defaultOrigin = (): string => BUILD_ORIGIN || sameOriginFallback()

/** 当前默认地址（未保存任何值时应显示/使用的值） */
export const getDefaultApiOrigin = (): string => defaultOrigin()

/**
 * 当前默认地址是否来自「浏览器同源回退」。
 * 已保存过自定义地址时不适用；用于登录页 / 设置里的「已使用当前页面地址」提示。
 */
export const isSameOriginDefault = (): boolean => {
  const stored = readStoredOrigin()
  if (stored && normalizeOrigin(stored)) {
    return false
  }
  return BUILD_ORIGIN === '' && sameOriginFallback() !== ''
}

/**
 * 当前生效的 API 根地址，可能为空字符串。
 * 为空时调用方必须显式报错（见 client.ts / speech.ts / http.ts），
 * 绝不能拼成相对 URL 去请求 WebView 自身的 origin。
 */
export const getApiOrigin = (): string => {
  const stored = readStoredOrigin()
  return (stored ? normalizeOrigin(stored) : null) ?? defaultOrigin()
}

/** 是否已经配置了可用的服务器地址；为空时 UI 必须先要求用户填写 */
export const hasApiOrigin = (): boolean => getApiOrigin() !== ''

/**
 * 保存服务器地址；传 null 恢复默认值：
 * 构建期地址 → 浏览器同源 → 空（原生壳内为空）。
 */
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
  return normalized ?? defaultOrigin()
}

/** 地址是否与默认值不同（设置面板用来提示「已自定义」） */
export const isCustomOrigin = (): boolean => getApiOrigin() !== defaultOrigin()
