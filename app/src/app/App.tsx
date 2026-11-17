import { useEffect, useRef } from 'react'
import { useTranslate } from '../i18n/index.js'
import { useSettings } from '../services/settings.js'
import { getStoredToken, initAuth, useAccountScope, useAuth } from '../services/auth.js'
import { resetAccountSettings } from '../services/accountSettings.js'
import { refreshCapabilities, resetCapabilities } from '../services/capabilities.js'
import { flushSyncQueue, resetSyncQueue } from '../services/sync/queue.js'
import { syncAfterLogin } from '../services/sync/pull.js'
import { useIsMobile, useMobileViewport, usePreventPageScroll } from './viewport.js'
import {
  bindChatPersistence,
  initChat,
  reloadSessionSummaries,
  resetChatForAccount,
  retrySaveSessions,
  selectSession,
  useChatStore,
} from '../services/chat/store.js'
import { SearchPanel } from '../components/search/SearchPanel.js'
import { closeSearchPanel } from '../components/search/searchPanelState.js'
import { SessionView } from '../components/session/SessionView.js'
import { MobileHeader } from '../components/session/MobileHeader.js'
import { Welcome } from '../components/session/Welcome.js'
import { ensureApiKey, closeSettings } from '../components/settings/settingsDialogState.js'
import { SettingsDialog } from '../components/settings/SettingsDialog.js'
import { LoginGate } from '../components/auth/LoginGate.js'
import { Toast } from '../components/base/Toast.js'
import { showToast } from '../components/base/toastState.js'
import { onUploadNotice } from '../services/attachments.js'
import { Button } from '../components/base/Button.js'
import { stopReadAloud } from '../services/speech/readAloud.js'
import { CollapsedSider } from '../components/sider/CollapsedSider.js'
import { closeMobileSider } from '../components/sider/mobileSiderState.js'
import { MobileSider, Sider } from '../components/sider/Sider.js'
import styles from './App.module.css'

export const App = () => {
  const t = useTranslate()
  const isMobile = useIsMobile()
  const locale = useSettings((state) => state.locale)
  const token = useAuth((state) => state.token)
  const skipLogin = useAuth((state) => state.skipLogin)
  // 当前账号命名空间：IndexedDB 按它分库（local / user-<id> / pending）
  const accountScope = useAccountScope()
  const previousScope = useRef(accountScope)
  // 没有云端 token 且未选择直连模式时，先显示登录门
  const gated = token === null && !skipLogin
  // 移动端侧边栏以独立抽屉管理开合状态
  const siderCollapsed = useSettings((state) => state.siderCollapsed) && !isMobile
  const ready = useChatStore((state) => state.ready)
  const initError = useChatStore((state) => state.initError)
  const saveError = useChatStore((state) => Object.values(state.sessionSaveErrors).some(Boolean))
  const importingHistory = useChatStore((state) => state.importingHistory)
  const session = useChatStore((state) =>
    state.activeId !== null && Object.hasOwn(state.sessionCache, state.activeId)
      ? state.sessionCache[state.activeId]
      : undefined
  )
  const sessionLoadError = useChatStore((state) =>
    state.activeId === null ? false : state.sessionLoadErrors[state.activeId] === true
  )
  const frameRef = useMobileViewport(isMobile)
  const streaming = useChatStore((state) =>
    state.activeId === null ? false : Object.hasOwn(state.streaming, state.activeId)
  )
  const activeId = useChatStore((state) => state.activeId)

  usePreventPageScroll(isMobile)

  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])

  // 文件数、存储空间达到上限时，附件上只显示「上传失败」，原因用 toast 说明
  useEffect(() => onUploadNotice(showToast), [])
  useEffect(bindChatPersistence, [])

  // 搜索结果面板只属于当前会话
  useEffect(closeSearchPanel, [activeId])
  // 切换会话时停止朗读
  useEffect(stopReadAloud, [activeId])
  // 移动端选中会话、切换布局后收起抽屉
  useEffect(closeMobileSider, [activeId, isMobile])

  /** 校验登录态 → 拉取云端列表 / 回填本地 → 刷新侧边栏；未登录时只看本地 */
  const syncAccount = async () => {
    try {
      if (getStoredToken() !== null && useAuth.getState().status === 'idle') {
        await initAuth()
        if (useAuth.getState().token === null) {
          return
        }
      }
      await syncAfterLogin()
      await refreshCapabilities()
    } catch (error) {
      // eslint-disable-next-line no-console
      console.warn('[sync] cloud sync failed', error)
    }
    await reloadSessionSummaries()
  }

  useEffect(() => {
    void initChat()
    void syncAccount()
    // 挂载时执行一次；账号切换由下面的 scope effect 处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 账号切换（登录 / 登出 / 换号）：丢弃上一账号的同步任务与内存态，
  // db 层按 scope 自动换库，然后重新拉取新账号的列表，避免侧边栏残留上一账号的数据。
  useEffect(() => {
    if (previousScope.current === accountScope) {
      return
    }
    previousScope.current = accountScope
    resetSyncQueue()
    resetAccountSettings()
    resetCapabilities()
    void (async () => {
      await resetChatForAccount()
      await syncAccount()
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountScope])

  // 进入主界面后才提示补凭证；登录门显示期间不要弹出设置
  useEffect(() => {
    if (gated) {
      // 登出 / token 失效回到登录门时收起设置，避免重新登录后被旧弹窗挡住
      closeSettings()
    } else {
      ensureApiKey()
    }
  }, [gated])

  // 页面隐藏 / 关闭前把待推送的同步任务立即发出
  useEffect(() => {
    const flush = () => flushSyncQueue()
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        flush()
      }
    }
    window.addEventListener('pagehide', flush)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => {
      window.removeEventListener('pagehide', flush)
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  }, [])

  if (gated) {
    return (
      <>
        <LoginGate />
        <Toast />
      </>
    )
  }

  return (
    <>
      <div ref={frameRef} className={styles.frame}>
        {isMobile ? <MobileSider /> : siderCollapsed ? null : <Sider />}
        <main className={styles.main}>
          {saveError ? (
            <div className={styles.saveError} role="alert">
              <span>{t('sessionSaveFailed')}</span>
              <Button
                variant="outlined"
                size="s"
                disabled={importingHistory}
                onClick={retrySaveSessions}
              >
                {t('retry')}
              </Button>
            </div>
          ) : null}
          <div className={styles.content}>
            {siderCollapsed ? <CollapsedSider /> : null}
            {!ready || (activeId !== null && !session) ? (
              <div className={styles.loadingView}>
                {isMobile ? <MobileHeader /> : null}
                <div className={styles.loadingState} role="status" aria-live="polite">
                  <span>
                    {(!ready ? initError : sessionLoadError)
                      ? t(!ready ? 'sessionListLoadFailed' : 'sessionLoadFailed')
                      : t('sessionLoading')}
                  </span>
                  {(!ready ? initError : sessionLoadError) ? (
                    <Button
                      variant="outlined"
                      size="m"
                      onClick={() => {
                        if (!ready) {
                          void initChat()
                        } else if (activeId !== null) {
                          selectSession(activeId)
                        }
                      }}
                    >
                      {t('retry')}
                    </Button>
                  ) : null}
                </div>
              </div>
            ) : session ? (
              <SessionView
                session={session}
                streaming={streaming}
                siderCollapsed={siderCollapsed}
              />
            ) : (
              <Welcome />
            )}
          </div>
        </main>
        <SearchPanel />
      </div>
      <SettingsDialog />
      <Toast />
    </>
  )
}
