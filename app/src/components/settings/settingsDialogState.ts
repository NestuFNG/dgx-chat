import { create } from 'zustand'
import { hasCredential } from '../../services/auth.js'

/** 设置弹窗的分组：服务器 / 账号、通用设置、数据管理 */
export type SettingsSection = 'server' | 'general' | 'data'

/** 设置弹窗：侧边栏底部、没有凭证时的操作与 401 提示都可以打开；section 为打开时选中的分组 */
export const useSettingsDialog = create(() => ({
  open: false,
  section: 'general' as SettingsSection,
}))

export const openSettings = (section: SettingsSection) =>
  useSettingsDialog.setState({ open: true, section })

export const closeSettings = () => useSettingsDialog.setState({ open: false })

/** 没有聊天凭证（云端 token 或直连 API Key）时打开设置并选中 API Key 所在的「数据管理」 */
export const ensureApiKey = () => {
  if (hasCredential()) {
    return true
  }
  openSettings('data')
  return false
}
