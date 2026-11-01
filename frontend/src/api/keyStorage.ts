/**
 * Browser-local storage for lightweight demo API keys.
 *
 * The backend database stores only SHA-256 hashes. To let a returning visitor
 * use keys generated in THIS browser, the raw demo secret is kept in
 * localStorage and is never rendered again after the one-time creation modal.
 * This is a portfolio/demo convenience, not production secret storage.
 */
import type { GenerateKeyResponse } from '../types/api'

export interface StoredApiKey {
  key_prefix: string
  label: string
  api_key: string
  created_at: string
  revoked?: boolean
}

const KEYS_STORAGE_KEY = 'routellm.apiKeys.v1'
const ACTIVE_STORAGE_KEY = 'routellm.activeKeyPrefix.v1'
export const KEYS_CHANGED_EVENT = 'routellm-keys-changed'

function notifyChanged(): void {
  window.dispatchEvent(new Event(KEYS_CHANGED_EVENT))
}

export function getStoredKeys(): StoredApiKey[] {
  try {
    const raw = localStorage.getItem(KEYS_STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is StoredApiKey =>
        typeof item === 'object' &&
        item !== null &&
        typeof (item as StoredApiKey).key_prefix === 'string' &&
        typeof (item as StoredApiKey).api_key === 'string' &&
        typeof (item as StoredApiKey).label === 'string',
    )
  } catch {
    return []
  }
}

function writeKeys(keys: StoredApiKey[]): void {
  localStorage.setItem(KEYS_STORAGE_KEY, JSON.stringify(keys))
  notifyChanged()
}

export function getActiveKey(): StoredApiKey | null {
  const keys = getStoredKeys()
  const activePrefix = localStorage.getItem(ACTIVE_STORAGE_KEY)
  return (
    keys.find((key) => key.key_prefix === activePrefix && !key.revoked) ??
    keys.find((key) => !key.revoked) ??
    null
  )
}

export function setActiveKey(keyPrefix: string): void {
  localStorage.setItem(ACTIVE_STORAGE_KEY, keyPrefix)
  notifyChanged()
}

export function saveGeneratedKey(response: GenerateKeyResponse): StoredApiKey {
  const stored: StoredApiKey = {
    key_prefix: response.key_prefix,
    label: response.label,
    api_key: response.api_key,
    created_at: new Date().toISOString(),
  }
  const keys = getStoredKeys().filter((key) => key.key_prefix !== stored.key_prefix)
  writeKeys([stored, ...keys])
  if (!localStorage.getItem(ACTIVE_STORAGE_KEY)) {
    setActiveKey(stored.key_prefix)
  }
  return stored
}

export function markKeyRevoked(keyPrefix: string): void {
  const keys = getStoredKeys().map((key) =>
    key.key_prefix === keyPrefix ? { ...key, revoked: true } : key,
  )
  writeKeys(keys)
  if (localStorage.getItem(ACTIVE_STORAGE_KEY) === keyPrefix) {
    const next = keys.find((key) => !key.revoked)
    if (next) {
      setActiveKey(next.key_prefix)
    } else {
      localStorage.removeItem(ACTIVE_STORAGE_KEY)
      notifyChanged()
    }
  }
}
