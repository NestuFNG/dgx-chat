/**
 * 云同步的本地游标：记录哪些会话 / 附件已经成功上传过。
 * 存 localStorage（数据量小、同步 API），避免为一次性标记升级 IndexedDB 版本。
 */
const SESSIONS_KEY = 'dgxchat:synced-sessions'
const FILES_KEY = 'dgxchat:synced-files'

const readSet = (key: string): Set<string> => {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) {
      return new Set()
    }
    const parsed: unknown = JSON.parse(raw)
    return new Set(
      Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
    )
  } catch {
    return new Set()
  }
}

const writeSet = (key: string, value: Set<string>) => {
  try {
    localStorage.setItem(key, JSON.stringify([...value]))
  } catch {
    // 存储不可用时下次会重复上传；PUT 幂等，可接受
  }
}

const updateSet = (key: string, id: string, action: 'add' | 'remove') => {
  const set = readSet(key)
  const has = set.has(id)
  if ((action === 'add') === has) {
    return
  }
  if (action === 'add') {
    set.add(id)
  } else {
    set.delete(id)
  }
  writeSet(key, set)
}

export const isSessionPushed = (id: string): boolean => readSet(SESSIONS_KEY).has(id)
export const markSessionPushed = (id: string) => updateSet(SESSIONS_KEY, id, 'add')
export const unmarkSessionPushed = (id: string) => updateSet(SESSIONS_KEY, id, 'remove')

export const isFilePushed = (id: string): boolean => readSet(FILES_KEY).has(id)
export const markFilePushed = (id: string) => updateSet(FILES_KEY, id, 'add')
export const unmarkFilePushed = (id: string) => updateSet(FILES_KEY, id, 'remove')
