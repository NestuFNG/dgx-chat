import { useState, type FormEvent } from 'react'
import { useTranslate } from '../../i18n/index.js'
import { HttpError } from '../../services/api/http.js'
import { getApiOrigin, normalizeOrigin, setApiOrigin } from '../../services/api/origin.js'
import {
  isNetworkError,
  login,
  register,
  setSkipLogin,
  useAuth,
} from '../../services/auth.js'
import { setLocale, useSettings } from '../../services/settings.js'
import { BrandMark } from './BrandMark.js'
import styles from './LoginGate.module.css'

/**
 * 未登录时的登录门：服务器地址 + 账号登录 / 注册。
 * 服务器地址始终可编辑（主流程的一部分）；直连模式是弱化的次要入口，放在卡片底部。
 */
export const LoginGate = () => {
  const t = useTranslate()
  const locale = useSettings((state) => state.locale)
  const status = useAuth((state) => state.status)
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [origin, setOrigin] = useState(() => getApiOrigin())
  const [originError, setOriginError] = useState<string | null>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** 校验并保存服务器地址；返回是否有效 */
  const saveOrigin = (): boolean => {
    if (normalizeOrigin(origin) === null) {
      setOriginError(t('serverAddressInvalid'))
      return false
    }
    setOriginError(null)
    setOrigin(setApiOrigin(origin))
    return true
  }

  const describe = (cause: unknown, kind: 'login' | 'register'): string => {
    if (isNetworkError(cause)) {
      return t('serverNetworkError', {
        message: cause instanceof Error ? cause.message : String(cause),
      })
    }
    if (cause instanceof HttpError) {
      if (cause.status === 429) {
        return t('authRateLimited')
      }
      if (cause.status === 403) {
        return t('registerInviteInvalid')
      }
      if (cause.status === 401) {
        return t('loginInvalidCredentials')
      }
    }
    return t(kind === 'register' ? 'serverRegisterFailed' : 'serverLoginFailed', {
      message: cause instanceof Error ? cause.message : String(cause),
    })
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy || !saveOrigin()) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      if (mode === 'register') {
        await register(username, password, inviteCode)
      } else {
        await login(username, password)
      }
      // 登录成功后 token 写入 localStorage，App 根据登录态切回主界面
    } catch (cause) {
      setError(describe(cause, mode))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          <BrandMark />
          <div className={styles.brandText}>
            <div className={styles.title}>DGX Chat</div>
            <div className={styles.subtitle}>{t('loginSubtitle')}</div>
          </div>
        </div>

        <form className={styles.form} onSubmit={(event) => void submit(event)}>
          <label className={styles.field}>
            <span className={styles.fieldLabel}>{t('serverAddress')}</span>
            <input
              className={styles.input}
              type="text"
              inputMode="url"
              spellCheck={false}
              autoComplete="url"
              value={origin}
              placeholder={t('serverAddressPlaceholder')}
              onChange={(event) => setOrigin(event.target.value)}
              onBlur={() => {
                if (origin !== getApiOrigin()) {
                  saveOrigin()
                }
              }}
            />
            {originError ? (
              <span className={styles.fieldError} role="alert">
                {originError}
              </span>
            ) : (
              <span className={styles.fieldHint}>{t('serverAddressDescription')}</span>
            )}
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>{t('serverUsername')}</span>
            <input
              className={styles.input}
              type="text"
              autoComplete="username"
              spellCheck={false}
              value={username}
              placeholder={t('serverUsernamePlaceholder')}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>

          <label className={styles.field}>
            <span className={styles.fieldLabel}>{t('serverPassword')}</span>
            <input
              className={styles.input}
              type="password"
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
              value={password}
              placeholder={t('serverPasswordPlaceholder')}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>

          {mode === 'register' ? (
            <label className={styles.field}>
              <span className={styles.fieldLabel}>{t('inviteCode')}</span>
              <input
                className={styles.input}
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={inviteCode}
                placeholder={t('inviteCodePlaceholder')}
                onChange={(event) => setInviteCode(event.target.value)}
              />
            </label>
          ) : null}

          {error ? (
            <div className={styles.error} role="alert">
              {error}
            </div>
          ) : null}

          <button className={styles.primary} type="submit" disabled={busy}>
            {busy
              ? t('loginLoading')
              : mode === 'register'
                ? t('loginRegisterSubmit')
                : t('loginSubmit')}
          </button>

          <div className={styles.switchRow}>
            <button
              className={styles.link}
              type="button"
              disabled={busy}
              onClick={() => {
                setMode(mode === 'login' ? 'register' : 'login')
                setError(null)
              }}
            >
              {mode === 'login' ? t('loginSwitchToRegister') : t('loginSwitchToLogin')}
            </button>
            <button
              className={styles.link}
              type="button"
              onClick={() => setLocale(locale === 'zh-CN' ? 'en-US' : 'zh-CN')}
            >
              {locale === 'zh-CN' ? t('settingsLocaleEnUS') : t('settingsLocaleZhCN')}
            </button>
          </div>

          {mode === 'register' ? (
            <p className={styles.hint}>{t('loginFirstUserHint')}</p>
          ) : null}
          {status === 'failed' ? (
            <p className={styles.hint} aria-live="polite">
              {t('serverConnectionFailed', { message: useAuth.getState().error ?? '' })}
            </p>
          ) : null}
        </form>

        <div className={styles.skip}>
          <button
            className={styles.skipButton}
            type="button"
            disabled={busy}
            onClick={() => {
              if (!saveOrigin()) {
                return
              }
              setSkipLogin(true)
            }}
          >
            {t('loginSkip')}
          </button>
          <p className={styles.skipHint}>{t('loginSkipHint')}</p>
        </div>
      </div>
    </div>
  )
}
