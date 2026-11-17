import clsx from 'clsx'
import { useEffect, useState, type KeyboardEvent } from 'react'
import { useTranslate } from '../../i18n/index.js'
import { HttpError } from '../../services/api/http.js'
import {
  getApiOrigin,
  isSameOriginDefault,
  normalizeOrigin,
  setApiOrigin,
} from '../../services/api/origin.js'
import {
  adoptToken,
  checkConnection,
  isNetworkError,
  login,
  logout,
  register,
  useAuth,
  verifyToken,
} from '../../services/auth.js'
import { IconEditOutline16 } from '../base/icons.js'
import { Button } from '../base/Button.js'
import { IconButton } from '../base/IconButton.js'
import { showToast } from '../base/toastState.js'
import settingsStyles from './Settings.module.css'
import styles from './ServerSettings.module.css'

/**
 * 设置面板「服务器 / 账号」：服务器地址、Token、登录 / 注册 / 登出与连接状态。
 * 地址与 Token 在直连模式（无 token）下同样可编辑，避免用户找不到回到云端账号的入口。
 */
export const ServerSettings = () => {
  const t = useTranslate()
  const token = useAuth((state) => state.token)
  const user = useAuth((state) => state.user)
  const status = useAuth((state) => state.status)
  const connectionError = useAuth((state) => state.error)

  const [origin, setOrigin] = useState(() => getApiOrigin())
  const [originError, setOriginError] = useState<string | null>(null)
  const [originSaved, setOriginSaved] = useState(false)
  // Web 部署时默认使用当前页面地址；原生壳内没有这个回退
  const [sameOriginDefault, setSameOriginDefault] = useState(() => isSameOriginDefault())

  const [editingToken, setEditingToken] = useState(false)
  const [tokenValue, setTokenValue] = useState('')
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [tokenBusy, setTokenBusy] = useState(false)

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [inviteCode, setInviteCode] = useState('')
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login')
  const [authBusy, setAuthBusy] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)

  // 打开设置时若已登录且还没校验过，主动查一次 /auth/me
  useEffect(() => {
    if (token && useAuth.getState().status === 'idle') {
      void checkConnection()
    }
  }, [token])

  const saveOrigin = () => {
    if (!origin.trim()) {
      setOriginError(t('serverAddressMissing'))
      return
    }
    if (normalizeOrigin(origin) === null) {
      setOriginError(t('serverAddressInvalid'))
      return
    }
    const saved = setApiOrigin(origin)
    setOrigin(saved)
    setOriginError(null)
    setOriginSaved(true)
    setSameOriginDefault(isSameOriginDefault())
    if (useAuth.getState().token) {
      void checkConnection()
    }
  }

  /** 恢复默认值：构建期地址 → 浏览器同源 → 空（原生壳内为空），绝不伪造地址 */
  const resetOrigin = () => {
    const restored = setApiOrigin(null)
    setOrigin(restored)
    setOriginError(null)
    setOriginSaved(false)
    setSameOriginDefault(isSameOriginDefault())
    if (restored && useAuth.getState().token) {
      void checkConnection()
    }
  }

  const saveToken = async () => {
    const value = tokenValue.trim()
    if (!value || tokenBusy) {
      return
    }
    setTokenBusy(true)
    setTokenError(null)
    try {
      const me = await verifyToken(value)
      adoptToken(value, me)
      setEditingToken(false)
      setTokenValue('')
      showToast({ key: 'serverTokenSaved' })
    } catch (cause) {
      if (isNetworkError(cause)) {
        // 服务器暂时连不上：先保存 token，状态显示「连接失败」，恢复后自动可用
        adoptToken(value, null, 'failed')
        setEditingToken(false)
        setTokenValue('')
        void checkConnection()
        showToast({ key: 'serverTokenSaved' })
      } else {
        setTokenError(t('serverTokenInvalid'))
      }
    } finally {
      setTokenBusy(false)
    }
  }

  /** 后端错误分类：限流 / 邀请码（403）/ 账号密码错误单独给出可读文案 */
  const describeAuthError = (cause: unknown, kind: 'login' | 'register'): string => {
    if (isNetworkError(cause)) {
      const message = cause instanceof Error ? cause.message : String(cause)
      if (message === t('serverAddressRequired')) {
        return message
      }
      return t('serverNetworkError', { message })
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

  const submitAuth = async (kind: 'login' | 'register') => {
    if (authBusy || !username.trim() || !password) {
      return
    }
    setAuthBusy(true)
    setAuthError(null)
    try {
      if (kind === 'register') {
        await register(username, password, inviteCode)
      } else {
        await login(username, password)
      }
      setUsername('')
      setPassword('')
      setInviteCode('')
    } catch (cause) {
      setAuthError(describeAuthError(cause, kind))
    } finally {
      setAuthBusy(false)
    }
  }

  const statusText =
    status === 'checking'
      ? t('serverChecking')
      : status === 'connected'
        ? t('serverConnected')
        : status === 'failed'
          ? t('serverConnectionFailed', { message: connectionError ?? '' })
          : token
            ? t('serverChecking')
            : t('serverNotLoggedIn')

  return (
    <>
      <div className={clsx(settingsStyles.cell, styles.block)}>
        <div className={settingsStyles.label}>{t('serverAddress')}</div>
        <div className={settingsStyles.description}>{t('serverAddressDescription')}</div>
        {sameOriginDefault ? (
          <div className={settingsStyles.description}>{t('serverAddressSameOriginHint')}</div>
        ) : null}
        <div className={styles.inline}>
          <input
            className={clsx(styles.input, originError && styles.inputError)}
            type="text"
            inputMode="url"
            spellCheck={false}
            autoComplete="url"
            value={origin}
            placeholder={t('serverAddressPlaceholder')}
            onChange={(event) => {
              setOrigin(event.target.value)
              setOriginSaved(false)
            }}
            onBlur={saveOrigin}
            onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                saveOrigin()
              }
            }}
          />
          <button type="button" className={styles.link} onClick={resetOrigin}>
            {t('serverAddressReset')}
          </button>
          {originError ? (
            <div className={settingsStyles.error} role="alert">
              {originError}
            </div>
          ) : originSaved ? (
            <div className={styles.saved} role="status">
              {t('serverAddressSaved')}
            </div>
          ) : null}
        </div>
      </div>

      <div className={clsx(settingsStyles.cell, styles.block)}>
        <div className={settingsStyles.label}>{t('serverToken')}</div>
        <div className={settingsStyles.description}>{t('serverTokenDescription')}</div>
        {token && !editingToken ? (
          <div className={styles.tokenSaved}>
            <span className={styles.tokenMask}>{'*'.repeat(Math.min(token.length, 48))}</span>
            <IconButton
              tone="primary"
              icon={<IconEditOutline16 />}
              tooltip={t('serverToken')}
              onClick={() => setEditingToken(true)}
            />
          </div>
        ) : (
          <div className={styles.tokenInput}>
            <input
              type="password"
              placeholder={t('serverTokenPlaceholder')}
              autoComplete="off"
              spellCheck={false}
              value={tokenValue}
              onChange={(event) => {
                setTokenValue(event.target.value)
                setTokenError(null)
              }}
              onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                  void saveToken()
                }
              }}
            />
            {tokenValue.trim() ? (
              <>
                <span className={styles.divider} />
                <button
                  type="button"
                  className={styles.confirm}
                  disabled={tokenBusy}
                  onMouseDown={(event) => event.preventDefault()}
                  onClick={() => void saveToken()}
                >
                  {tokenBusy ? t('apiKeyVerifying') : t('confirm')}
                </button>
              </>
            ) : null}
          </div>
        )}
        {tokenError ? <div className={settingsStyles.error}>{tokenError}</div> : null}
      </div>

      <div className={clsx(settingsStyles.cell, settingsStyles.row)}>
        <div className={settingsStyles.text}>
          <div className={settingsStyles.label}>{t('serverAccount')}</div>
          <div className={settingsStyles.description}>
            {token ? (user?.username ?? t('serverNotLoggedIn')) : t('serverLocalMode')}
          </div>
          <div
            className={clsx(
              styles.status,
              status === 'connected' && styles.statusOk,
              status === 'failed' && styles.statusError
            )}
            role="status"
            aria-live="polite"
          >
            {statusText}
          </div>
        </div>
        <div className={styles.actions}>
          {token ? (
            <>
              <Button variant="outlined" size="m" onClick={() => void checkConnection()}>
                {t('serverRecheck')}
              </Button>
              <Button variant="dangerOutlined" size="m" onClick={logout}>
                {t('serverLogout')}
              </Button>
            </>
          ) : null}
        </div>
      </div>

      {token ? null : (
        <div className={clsx(settingsStyles.cell, styles.loginBlock)}>
          <div className={settingsStyles.label}>
            {authMode === 'login' ? t('serverLogin') : t('serverRegister')}
          </div>
          <div className={styles.loginRow}>
            <input
              className={styles.input}
              type="text"
              autoComplete="username"
              spellCheck={false}
              placeholder={t('serverUsernamePlaceholder')}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
            <input
              className={styles.input}
              type="password"
              autoComplete={authMode === 'register' ? 'new-password' : 'current-password'}
              placeholder={t('serverPasswordPlaceholder')}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
                if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                  void submitAuth(authMode)
                }
              }}
            />
            {authMode === 'register' ? (
              <input
                className={styles.input}
                type="text"
                autoComplete="off"
                spellCheck={false}
                placeholder={t('inviteCodePlaceholder')}
                value={inviteCode}
                onChange={(event) => setInviteCode(event.target.value)}
              />
            ) : null}
            <Button
              size="m"
              disabled={authBusy || !username.trim() || !password}
              onClick={() => void submitAuth(authMode)}
            >
              {authMode === 'login' ? t('serverLogin') : t('serverRegister')}
            </Button>
            <button
              type="button"
              className={styles.link}
              onClick={() => {
                setAuthMode(authMode === 'login' ? 'register' : 'login')
                setAuthError(null)
              }}
            >
              {authMode === 'login' ? t('loginSwitchToRegister') : t('loginSwitchToLogin')}
            </button>
          </div>
          {authError ? (
            <div className={settingsStyles.error} role="alert">
              {authError}
            </div>
          ) : null}
          <div className={settingsStyles.description}>
            {authMode === 'register' ? t('serverRegisterHint') : t('serverLoginToSync')}
          </div>
        </div>
      )}
    </>
  )
}
