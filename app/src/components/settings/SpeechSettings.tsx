import { useEffect, useState } from 'react'
import { useTranslate } from '../../i18n/index.js'
import { useAuth } from '../../services/auth.js'
import {
  loadAccountSettings,
  saveAccountSettings,
  useAccountSettings,
  type AccountSettingsPatch,
  type SecretBlock,
  type SpeechBlock,
  type SpeechBlockPatch,
} from '../../services/accountSettings.js'
import { refreshCapabilities } from '../../services/capabilities.js'
import { Button } from '../base/Button.js'
import { Select } from '../base/Select.js'
import { showToast } from '../base/toastState.js'
import settingsStyles from './Settings.module.css'
import styles from './SpeechSettings.module.css'

type Provider = '' | 'openai' | 'deepseek_tools'

const providerOf = (value: string | null): Provider =>
  value === 'openai' || value === 'deepseek_tools' ? value : ''

const EMPTY_SECRET: SecretBlock = { base_url: null, api_key: null, has_key: false }
const EMPTY_SPEECH: SpeechBlock = {
  provider: null,
  base_url: null,
  model: null,
  voice: null,
  api_key: null,
  has_key: false,
}

const errorText = (cause: unknown): string =>
  cause instanceof Error ? cause.message : String(cause)

const isHttpUrl = (value: string): boolean => {
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:'
  } catch {
    return false
  }
}

/**
 * 「语音与联网搜索」子区块：官方 API Key + 自定义 TTS / ASR 端点。
 * 所有 Key 都只存在服务器；GET/PUT 往返的是掩码，输入框提交后立即被响应里的掩码替换。
 */
export const SpeechSettings = () => {
  const t = useTranslate()
  const token = useAuth((state) => state.token)
  const settings = useAccountSettings((state) => state.data)
  const loading = useAccountSettings((state) => state.loading)
  const loadError = useAccountSettings((state) => state.error)
  const disabled = token === null

  useEffect(() => {
    if (!token) {
      return
    }
    void loadAccountSettings().catch(() => {
      // 错误已写入 store，由下面的提示展示
    })
    void refreshCapabilities()
  }, [token])

  const saveOfficialKey = async (value: string) => {
    await saveAccountSettings({
      official: { api_key: value, base_url: settings?.official.base_url ?? null },
    })
    await refreshCapabilities()
    showToast({ key: 'saved' })
  }

  const clearOfficialKey = async () => {
    await saveAccountSettings({ official: { api_key: '' } })
    await refreshCapabilities()
    showToast({ key: 'saved' })
  }

  return (
    <section className={styles.section}>
      <div className={styles.sectionTitle}>{t('voiceSearchSection')}</div>
      {disabled ? <p className={styles.hint}>{t('loginToSaveAccount')}</p> : null}

      <div className={styles.precedence}>
        <div className={styles.precedenceTitle}>{t('speechPrecedenceTitle')}</div>
        <ul className={styles.precedenceList}>
          <li>{t('speechPrecedenceVoice')}</li>
          <li>{t('speechPrecedenceSearch')}</li>
          <li>{t('speechPrecedenceFallback')}</li>
          <li>{t('speechPrecedenceNoForward')}</li>
        </ul>
      </div>

      {loading && !settings ? <p className={styles.hint}>{t('loadingSettings')}</p> : null}
      {loadError && !settings ? (
        <div className={settingsStyles.error}>
          {t('settingsLoadFailed', { message: loadError })}
        </div>
      ) : null}

      <fieldset className={styles.fieldset} disabled={disabled}>
        <SecretEditor
          label={t('officialApiKey')}
          description={t('officialApiKeyDescription')}
          block={settings?.official ?? EMPTY_SECRET}
          disabled={disabled}
          onSave={saveOfficialKey}
          onClear={clearOfficialKey}
        />
        <EndpointForm kind="tts" block={settings?.tts ?? EMPTY_SPEECH} disabled={disabled} />
        <EndpointForm kind="asr" block={settings?.asr ?? EMPTY_SPEECH} disabled={disabled} />
      </fieldset>
    </section>
  )
}

const SecretEditor = ({
  label,
  description,
  block,
  disabled,
  onSave,
  onClear,
}: {
  label: string
  description?: string
  block: SecretBlock
  disabled: boolean
  onSave: (value: string) => Promise<void>
  onClear: () => Promise<void>
}) => {
  const t = useTranslate()
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 服务器返回的掩码变化（保存 / 清除 / 换账号）后退出编辑态
  useEffect(() => {
    if (!block.has_key) {
      setEditing(false)
      setValue('')
      setError(null)
    }
  }, [block.has_key])

  const submit = async () => {
    const key = value.trim()
    if (!key || busy || disabled) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onSave(key)
      setEditing(false)
      setValue('')
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    if (busy || disabled) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await onClear()
      setEditing(false)
      setValue('')
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={styles.field}>
      <div className={styles.fieldHead}>
        <span className={styles.fieldLabel}>{label}</span>
        <span className={block.has_key ? styles.badgeOn : styles.badgeOff}>
          {block.has_key ? t('configured') : t('notConfigured')}
        </span>
      </div>
      {description ? <div className={styles.description}>{description}</div> : null}
      {block.has_key && !editing ? (
        <div className={styles.secretRow}>
          <span className={styles.mask}>{block.api_key ?? '••••'}</span>
          <Button
            variant="outlined"
            size="s"
            disabled={disabled || busy}
            onClick={() => setEditing(true)}
          >
            {t('edit')}
          </Button>
          <Button
            variant="dangerOutlined"
            size="s"
            disabled={disabled || busy}
            onClick={() => void clear()}
          >
            {t('clear')}
          </Button>
        </div>
      ) : (
        <div className={styles.secretInputRow}>
          <input
            className={styles.input}
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={t('apiKeyPlaceholder')}
            disabled={disabled || busy}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
                void submit()
              }
            }}
          />
          <Button size="s" disabled={disabled || busy || !value.trim()} onClick={() => void submit()}>
            {busy ? t('saving') : t('save')}
          </Button>
          {block.has_key ? (
            <Button
              variant="outlined"
              size="s"
              disabled={disabled || busy}
              onClick={() => {
                setEditing(false)
                setValue('')
                setError(null)
              }}
            >
              {t('cancel')}
            </Button>
          ) : null}
        </div>
      )}
      {error ? <div className={settingsStyles.error}>{error}</div> : null}
    </div>
  )
}

const EndpointForm = ({
  kind,
  block,
  disabled,
}: {
  kind: 'tts' | 'asr'
  block: SpeechBlock
  disabled: boolean
}) => {
  const t = useTranslate()
  const [provider, setProvider] = useState<Provider>(() => providerOf(block.provider))
  const [baseUrl, setBaseUrl] = useState(block.base_url ?? '')
  const [model, setModel] = useState(block.model ?? '')
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 服务器响应更新后（保存成功 / 换账号）同步表单；用户正在编辑时不覆盖
  useEffect(() => {
    if (dirty) {
      return
    }
    setProvider(providerOf(block.provider))
    setBaseUrl(block.base_url ?? '')
    setModel(block.model ?? '')
  }, [block, dirty])

  const title = kind === 'tts' ? t('customTtsEndpoint') : t('customAsrEndpoint')

  const buildPatch = (apiKey: string): SpeechBlockPatch => {
    if (!provider) {
      return { provider: null, base_url: null, model: null, voice: null, api_key: apiKey }
    }
    return {
      provider,
      base_url: baseUrl.trim(),
      model: model.trim() || null,
      // 表单未暴露 voice；带上服务器当前值，避免整块保存时丢字段
      voice: block.voice ?? null,
      api_key: apiKey,
    }
  }

  const patchFor = (apiKey: string): AccountSettingsPatch =>
    kind === 'tts' ? { tts: buildPatch(apiKey) } : { asr: buildPatch(apiKey) }

  const save = async () => {
    if (busy || disabled) {
      return
    }
    const url = baseUrl.trim()
    if (provider && !url) {
      setError(t('speechBaseUrlRequired'))
      return
    }
    if (url && !isHttpUrl(url)) {
      setError(t('speechBaseUrlInvalid'))
      return
    }
    if (url.length > 512 || model.trim().length > 512) {
      setError(t('speechFieldTooLong'))
      return
    }
    setBusy(true)
    setError(null)
    try {
      // 保存端点字段时保持服务器上的 Key 不变；Key 由下面的 SecretEditor 单独提交
      await saveAccountSettings(patchFor('__keep__'))
      setDirty(false)
      await refreshCapabilities()
      showToast({ key: 'saved' })
    } catch (cause) {
      setError(errorText(cause))
    } finally {
      setBusy(false)
    }
  }

  const saveKey = async (value: string) => {
    await saveAccountSettings(patchFor(value))
    setDirty(false)
    await refreshCapabilities()
    showToast({ key: 'saved' })
  }

  const clearKey = async () => {
    await saveAccountSettings(patchFor(''))
    await refreshCapabilities()
    showToast({ key: 'saved' })
  }

  const providerOptions: { value: Provider; label: string }[] = [
    { value: '', label: t('speechProviderFollow') },
    { value: 'openai', label: t('speechProviderOpenai') },
    { value: 'deepseek_tools', label: t('speechProviderDeepseekTools') },
  ]

  return (
    <div className={styles.endpoint}>
      <div className={styles.endpointTitle}>{title}</div>
      <div className={styles.field}>
        <span className={styles.fieldLabel}>{t('speechProviderLabel')}</span>
        <Select
          ariaLabel={title}
          value={provider}
          disabled={disabled || busy}
          options={providerOptions}
          onChange={(value) => {
            setProvider(value)
            setDirty(true)
            setError(null)
          }}
        />
      </div>
      {provider ? (
        <>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>{t('speechBaseUrlLabel')}</span>
            <input
              className={styles.input}
              type="text"
              inputMode="url"
              spellCheck={false}
              autoComplete="url"
              placeholder="https://api.example.com"
              disabled={disabled || busy}
              value={baseUrl}
              onChange={(event) => {
                setBaseUrl(event.target.value)
                setDirty(true)
              }}
            />
          </div>
          <div className={styles.field}>
            <span className={styles.fieldLabel}>{t('speechModelLabel')}</span>
            <input
              className={styles.input}
              type="text"
              spellCheck={false}
              autoComplete="off"
              placeholder={t('speechModelPlaceholder')}
              disabled={disabled || busy}
              value={model}
              onChange={(event) => {
                setModel(event.target.value)
                setDirty(true)
              }}
            />
          </div>
          <SecretEditor
            label={t('speechEndpointKeyLabel')}
            description={t('speechEndpointKeyHint')}
            block={block}
            disabled={disabled}
            onSave={saveKey}
            onClear={clearKey}
          />
        </>
      ) : (
        <p className={styles.hint}>{t('speechFollowServerHint')}</p>
      )}
      <div className={styles.actions}>
        <Button
          size="s"
          variant="outlined"
          disabled={disabled || busy || (!dirty && !provider)}
          onClick={() => {
            setDirty(false)
            setError(null)
          }}
        >
          {t('cancel')}
        </Button>
        <Button size="s" disabled={disabled || busy} onClick={() => void save()}>
          {busy ? t('saving') : t('save')}
        </Button>
      </div>
      {error ? <div className={settingsStyles.error}>{error}</div> : null}
    </div>
  )
}
