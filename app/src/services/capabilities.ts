import { create } from 'zustand'
import { getStoredToken, handleUnauthorized } from './auth.js'
import { bearer, fetchApiJson } from './api/http.js'
import { isUnauthorized } from './sync/cloud.js'

/**
 * 服务器推导出的「当前账号能用什么」：是否有官方 Key、语音当前走账号/服务器、搜索是否可用。
 * 只含布尔与来源标记，不含任何 Key 材料。登录、切换账号、保存设置、打开设置时刷新。
 */
export type CapabilitySource = 'user' | 'server' | null

export type Capabilities = {
  official: { configured: boolean; source: CapabilitySource }
  tts: { configured: boolean; source: CapabilitySource }
  asr: { configured: boolean; source: CapabilitySource }
  search: { available: boolean; requires_official: boolean }
}

type State = {
  data: Capabilities | null
  loading: boolean
  error: string | null
}

export const useCapabilities = create<State>(() => ({
  data: null,
  loading: false,
  error: null,
}))

let inflight: Promise<Capabilities | null> | null = null

/** 刷新能力标记；未登录直接清空。401 交给 auth 层回登录页。 */
export const refreshCapabilities = (): Promise<Capabilities | null> => {
  const token = getStoredToken()
  if (!token) {
    useCapabilities.setState({ data: null, loading: false, error: null })
    return Promise.resolve(null)
  }
  if (inflight) {
    return inflight
  }
  useCapabilities.setState({ loading: true, error: null })
  const request = fetchApiJson<Capabilities>('/api/me/capabilities', { headers: bearer(token) })
    .then((data) => {
      useCapabilities.setState({ data, loading: false, error: null })
      return data
    })
    .catch((error: unknown) => {
      if (isUnauthorized(error)) {
        handleUnauthorized()
      }
      useCapabilities.setState({
        loading: false,
        error: error instanceof Error ? error.message : String(error),
      })
      return null
    })
    .finally(() => {
      inflight = null
    })
  inflight = request
  return request
}

/** 账号切换 / 登出时清空 */
export const resetCapabilities = () => {
  inflight = null
  useCapabilities.setState({ data: null, loading: false, error: null })
}
