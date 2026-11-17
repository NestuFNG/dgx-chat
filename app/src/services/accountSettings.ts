import { create } from 'zustand'
import { getStoredToken, handleUnauthorized } from './auth.js'
import { bearer, fetchApiJson } from './api/http.js'
import { isUnauthorized } from './sync/cloud.js'

/**
 * 每个账号存在服务器上的非凭证设置 + 语音 / 搜索凭证块（SPEC 后端契约）。
 * 服务器返回的 api_key 永远是掩码；真实 Key 只在本组件编辑态里短暂存在，不写 localStorage。
 */
export type SecretBlock = {
  base_url: string | null
  /** 掩码（如 sk-de…mnop）或 null；响应里绝不出现真实值 */
  api_key: string | null
  has_key: boolean
}

export type SpeechBlock = SecretBlock & {
  provider: string | null
  model: string | null
  voice: string | null
}

export type SearchBlock = SecretBlock & {
  mode: string | null
  provider: string | null
  max_results: number | null
}

export type AccountSettings = {
  model?: { id?: string } | null
  official: SecretBlock
  tts: SpeechBlock
  asr: SpeechBlock
  search: SearchBlock
  /** 后端保存的其它非凭证设置原样保留 */
  [key: string]: unknown
}

/** PUT 载荷：只想改哪个块就传哪个块，其余由服务器保留 */
export type SecretBlockPatch = {
  base_url?: string | null
  api_key?: string | null
}

export type SpeechBlockPatch = SecretBlockPatch & {
  provider?: string | null
  model?: string | null
  voice?: string | null
}

export type AccountSettingsPatch = {
  official?: SecretBlockPatch
  tts?: SpeechBlockPatch
  asr?: SpeechBlockPatch
}

type State = {
  data: AccountSettings | null
  loading: boolean
  error: string | null
}

export const useAccountSettings = create<State>(() => ({
  data: null,
  loading: false,
  error: null,
}))

let inflight: Promise<AccountSettings | null> | null = null

const tokenOrNull = (): string | null => getStoredToken()

/** 读取当前账号设置；未登录返回 null。成功后的掩码文档直接用于渲染。 */
export const loadAccountSettings = (): Promise<AccountSettings | null> => {
  const token = tokenOrNull()
  if (!token) {
    useAccountSettings.setState({ data: null, loading: false, error: null })
    return Promise.resolve(null)
  }
  if (inflight) {
    return inflight
  }
  useAccountSettings.setState({ loading: true, error: null })
  const request = fetchApiJson<AccountSettings>('/api/me/settings', { headers: bearer(token) })
    .then((data) => {
      useAccountSettings.setState({ data, loading: false, error: null })
      return data
    })
    .catch((error: unknown) => {
      if (isUnauthorized(error)) {
        handleUnauthorized()
      }
      useAccountSettings.setState({
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      })
      throw error
    })
    .finally(() => {
      inflight = null
    })
  inflight = request
  return request
}

/**
 * 保存部分设置并直接用服务器返回的掩码文档替换本地缓存（不要相信提交值）。
 * 失败时抛出 HttpError，UI 用 param 定位到具体字段。
 */
export const saveAccountSettings = async (
  patch: AccountSettingsPatch
): Promise<AccountSettings> => {
  const token = tokenOrNull()
  if (!token) {
    throw new Error('Not signed in')
  }
  const data = await fetchApiJson<AccountSettings>('/api/me/settings', {
    method: 'PUT',
    headers: { ...bearer(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  })
  useAccountSettings.setState({ data, loading: false, error: null })
  return data
}

/** 账号切换 / 登出时清空内存缓存 */
export const resetAccountSettings = () => {
  inflight = null
  useAccountSettings.setState({ data: null, loading: false, error: null })
}
