import { useState } from 'react'
import { useTranslate } from '../../i18n/index.js'
import { APP_VERSION, checkForUpdate, openExternal, type UpdateInfo } from '../../services/update.js'
import { Button } from '../base/Button.js'
import settingsStyles from './Settings.module.css'
import styles from './AboutUpdate.module.css'

type CheckState =
  | { status: 'idle' | 'checking' | 'error' }
  | { status: 'done'; info: UpdateInfo | null }

/** 「关于与更新」：显示当前版本、手动检查更新，并把下载交给系统浏览器 */
export const AboutUpdate = () => {
  const t = useTranslate()
  const [state, setState] = useState<CheckState>({ status: 'idle' })

  const check = async () => {
    if (state.status === 'checking') {
      return
    }
    setState({ status: 'checking' })
    try {
      const info = await checkForUpdate()
      setState({ status: 'done', info })
    } catch (error) {
      // 检查失败是安静的内联提示，不弹窗；详细原因只写控制台
      // eslint-disable-next-line no-console
      console.warn('[update] check failed', error)
      setState({ status: 'error' })
    }
  }

  const info = state.status === 'done' ? state.info : null

  return (
    <div className={settingsStyles.cell}>
      <div className={styles.header}>
        <span className={settingsStyles.label}>{t('aboutUpdateTitle')}</span>
        <span className={styles.version}>{t('currentVersion', { version: APP_VERSION })}</span>
      </div>
      <div className={styles.actions}>
        <Button
          variant="outlined"
          size="m"
          disabled={state.status === 'checking'}
          onClick={() => void check()}
        >
          {state.status === 'checking' ? t('checkingUpdate') : t('checkUpdate')}
        </Button>
        {state.status === 'done' && state.info === null ? (
          <span className={styles.status}>{t('upToDate')}</span>
        ) : null}
        {state.status === 'error' ? (
          <span className={styles.status}>{t('updateCheckFailed')}</span>
        ) : null}
      </div>
      {info ? (
        <div className={styles.update}>
          <div className={styles.updateTitle}>
            {t('updateAvailable', { version: info.latestVersion })}
          </div>
          {info.notes ? <div className={styles.notes}>{info.notes}</div> : null}
          <div className={styles.actions}>
            <Button size="m" onClick={() => openExternal(info.downloadUrl)}>
              {t('downloadUpdate')}
            </Button>
            <span className={styles.hint}>{t('updateDownloadHint')}</span>
          </div>
          {/* 设备上打开浏览器失败时，可手动复制这个地址 */}
          <div className={styles.url}>{info.downloadUrl}</div>
        </div>
      ) : null}
    </div>
  )
}
