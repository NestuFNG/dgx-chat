import { getStoredToken } from './auth.js'
import { getApiOrigin } from './api/origin.js'

/**
 * 检查更新：读取服务器上的静态清单 {origin}/release/latest.json。
 * 清单是公开文件；登录时带上账号凭证只是为了保持请求一致。
 * 结果按 origin 缓存在本次页面生命周期内；账号切换时由 App 调用 resetUpdateCache()。
 */
export type UpdateVariant = {
  name: string
  origin: string | null
  /** 相对服务器 origin 的路径 */
  url: string
  sha256?: string
}

export type UpdateManifest = {
  generatedAt?: string
  latest: { version: string; versionCode?: number; notes?: string }
  variants: UpdateVariant[]
}

export type UpdateInfo = {
  currentVersion: string
  latestVersion: string
  notes: string
  versionCode?: number
  /** 已解包的绝对下载地址 = 当前服务器 origin + variant.url */
  downloadUrl: string
  variant: string
}

/** 构建期注入的客户端版本 */
export const APP_VERSION = __APP_VERSION__

const REQUEST_TIMEOUT_MS = 8_000
/** origin -> 检查结果（null 表示已是最新）；按 origin 隔离 */
const cache = new Map<string, UpdateInfo | null>()

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

const parseManifest = (value: unknown): UpdateManifest => {
  if (!isRecord(value)) {
    throw new Error('Invalid update manifest')
  }
  const latest = value.latest
  if (!isRecord(latest) || typeof latest.version !== 'string' || !latest.version.trim()) {
    throw new Error('Invalid update manifest: latest.version')
  }
  if (!Array.isArray(value.variants)) {
    throw new Error('Invalid update manifest: variants')
  }
  const variants: UpdateVariant[] = []
  for (const item of value.variants) {
    if (!isRecord(item) || typeof item.url !== 'string' || !item.url) {
      continue
    }
    variants.push({
      name: typeof item.name === 'string' ? item.name : 'variant',
      origin: typeof item.origin === 'string' ? item.origin : null,
      url: item.url,
      sha256: typeof item.sha256 === 'string' ? item.sha256 : undefined,
    })
  }
  if (variants.length === 0) {
    throw new Error('Invalid update manifest: no variants')
  }
  return {
    generatedAt: typeof value.generatedAt === 'string' ? value.generatedAt : undefined,
    latest: {
      version: latest.version.trim(),
      versionCode: typeof latest.versionCode === 'number' ? latest.versionCode : undefined,
      notes: typeof latest.notes === 'string' ? latest.notes : '',
    },
    variants,
  }
}

/** 优先选择 origin 完全匹配当前服务器配置的变体，其次 generic，最后第一个 */
export const selectVariant = (manifest: UpdateManifest, currentOrigin: string): UpdateVariant => {
  const exact = manifest.variants.find((variant) => variant.origin === currentOrigin)
  if (exact) {
    return exact
  }
  const generic = manifest.variants.find((variant) => variant.name === 'generic')
  return generic ?? manifest.variants[0]
}

const absoluteUrl = (origin: string, path: string): string => {
  try {
    return new URL(path, `${origin}/`).href
  } catch {
    return `${origin}${path}`
  }
}

/**
 * 检查是否有新版本：有更新返回 UpdateInfo，已是最新返回 null，失败抛出。
 * 同一 origin 的成功结果在本次会话内复用（包括「已是最新」）。
 */
export const checkForUpdate = async (): Promise<UpdateInfo | null> => {
  const origin = getApiOrigin()
  if (!origin) {
    throw new Error('Server address is not configured')
  }
  if (cache.has(origin)) {
    return cache.get(origin) ?? null
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  let response: Response
  try {
    const token = getStoredToken()
    response = await fetch(`${origin}/release/latest.json`, {
      method: 'GET',
      signal: controller.signal,
      cache: 'no-store',
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    })
  } catch (error) {
    if (controller.signal.aborted) {
      throw new Error('Update check timed out')
    }
    throw error instanceof Error ? error : new Error(String(error))
  } finally {
    clearTimeout(timer)
  }
  if (!response.ok) {
    throw new Error(`Update manifest returned HTTP ${response.status}`)
  }

  const manifest = parseManifest(await response.json())
  const variant = selectVariant(manifest, origin)
  const info: UpdateInfo | null =
    manifest.latest.version === APP_VERSION
      ? null
      : {
          currentVersion: APP_VERSION,
          latestVersion: manifest.latest.version,
          notes: manifest.latest.notes ?? '',
          versionCode: manifest.latest.versionCode,
          downloadUrl: absoluteUrl(origin, variant.url),
          variant: variant.name,
        }
  cache.set(origin, info)
  return info
}

/** 账号切换 / 登出时清空，避免沿用上一个账号的检查结果 */
export const resetUpdateCache = () => {
  cache.clear()
}

/**
 * 在系统浏览器打开下载地址：
 * - Capacitor 原生壳：window.open(_blank) 由 BridgeWebChromeClient 转成 ACTION_VIEW；
 * - 抛错时退回直接跳转；
 * - 普通浏览器直接 window.open。
 */
export const openExternal = (url: string) => {
  const capacitor = (globalThis as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor
  const native =
    typeof capacitor?.isNativePlatform === 'function' && capacitor.isNativePlatform() === true
  if (native) {
    try {
      window.open(url, '_blank', 'noopener')
      return
    } catch {
      window.location.href = url
      return
    }
  }
  window.open(url, '_blank', 'noopener')
}
