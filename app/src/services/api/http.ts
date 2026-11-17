import { translate } from '../../i18n/index.js'
import { getApiOrigin } from './origin.js'

/**
 * 云端接口（/auth/*、/api/*）的 HTTP 辅助：自动拼上运行期 origin，统一解析错误。
 * 与 chat 用的 client.ts 分开：这里面向自建后端，错误只需要状态码 + 可读文案。
 */
export class HttpError extends Error {
  constructor(
    readonly status: number | undefined,
    message: string,
    readonly kind: 'http' | 'network' = 'http',
    /** 后端 400 校验错误里的字段路径，例如 official.api_key */
    readonly param?: string | null
  ) {
    super(message)
    this.name = 'HttpError'
  }
}

type ErrorDetails = { message: string; param?: string | null }

/** 云端请求超时（毫秒）：服务器不可达或卡住时尽快给出可读错误 */
const REQUEST_TIMEOUT_MS = 15_000

/** 从响应里取可读错误与字段路径：优先后端约定的 {"error":{"message","param"}}，其次纯文本 */
const readErrorDetails = async (response: Response): Promise<ErrorDetails> => {
  const text = await response.text().catch(() => '')
  if (!text) {
    return { message: response.statusText || `HTTP ${response.status}` }
  }
  try {
    const body: unknown = JSON.parse(text)
    if (typeof body === 'object' && body !== null && 'error' in body) {
      const error = (body as { error?: { message?: unknown; param?: unknown } }).error
      if (typeof error?.message === 'string') {
        return {
          message: error.message,
          param: typeof error.param === 'string' ? error.param : null,
        }
      }
    }
  } catch {
    // 非 JSON 响应体直接用原文
  }
  return { message: text }
}

/** 兼容旧调用：只要可读文案 */
export const readErrorMessage = async (response: Response): Promise<string> =>
  (await readErrorDetails(response)).message

/** 拼接完整 URL；origin 未配置时抛出可读错误，绝不返回相对路径 */
export const apiUrl = (path: string): string => {
  const origin = getApiOrigin()
  if (!origin) {
    throw new HttpError(undefined, translate('serverAddressRequired'), 'network')
  }
  return `${origin}${path}`
}

export const bearer = (token: string): Record<string, string> => ({
  Authorization: `Bearer ${token}`,
})

export const fetchApi = async (path: string, init: RequestInit = {}): Promise<Response> => {
  // 分发包可能没有默认服务器地址：为空时显式报错，避免打到 WebView 自身的相对地址
  const origin = getApiOrigin()
  if (!origin) {
    throw new HttpError(undefined, translate('serverAddressRequired'), 'network')
  }
  // 自建 NAS 不可达时浏览器可能挂很久，统一 15 秒超时；调用方自己的 signal 仍然有效
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  const forwardAbort = () => controller.abort()
  init.signal?.addEventListener('abort', forwardAbort)
  let response: Response
  try {
    response = await fetch(`${origin}${path}`, { ...init, signal: controller.signal })
  } catch (error) {
    if (init.signal?.aborted) {
      throw error
    }
    if (controller.signal.aborted) {
      throw new HttpError(undefined, 'Request timed out', 'network')
    }
    throw new HttpError(undefined, error instanceof Error ? error.message : String(error), 'network')
  } finally {
    clearTimeout(timer)
    init.signal?.removeEventListener('abort', forwardAbort)
  }
  if (!response.ok) {
    const details = await readErrorDetails(response)
    throw new HttpError(response.status, details.message, 'http', details.param)
  }
  return response
}

export const fetchApiJson = async <T>(path: string, init: RequestInit = {}): Promise<T> =>
  (await fetchApi(path, init)).json() as Promise<T>

export const jsonBody = (value: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(value),
})
