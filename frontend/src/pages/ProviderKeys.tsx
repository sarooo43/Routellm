import { AlertTriangle, ExternalLink, KeyRound, PlugZap, Save, ShieldCheck, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  deleteProviderKey,
  generateApiKey,
  getProviderKeys,
  setProviderKey,
} from '../api/client'
import {
  getActiveKey,
  getStoredKeys,
  KEYS_CHANGED_EVENT,
  markKeyRevoked,
  saveGeneratedKey,
  setActiveKey,
  type StoredApiKey,
} from '../api/keyStorage'
import { GlassCard } from '../components/GlassCard'
import { PageHeader } from '../components/PageHeader'
import { toApiError } from '../api/errors'
import { relativeTime } from '../lib/format'
import type { ProviderKeyStatus } from '../types/api'

interface ProviderMeta {
  name: string
  display: string
  description: string
  consoleUrl: string
  placeholder: string
}

const PROVIDERS: ProviderMeta[] = [
  {
    name: 'groq',
    display: 'Groq',
    description: 'Fast Llama inference via the Groq OpenAI-compatible API.',
    consoleUrl: 'https://console.groq.com/keys',
    placeholder: 'gsk_…',
  },
  {
    name: 'gemini',
    display: 'Google Gemini',
    description: 'Gemini flash models through Google AI Studio.',
    consoleUrl: 'https://aistudio.google.com/app/apikey',
    placeholder: 'AIza…',
  },
  {
    name: 'deepseek',
    display: 'DeepSeek',
    description: 'DeepSeek chat/coder models for low-cost reasoning.',
    consoleUrl: 'https://platform.deepseek.com/api_keys',
    placeholder: 'sk-…',
  },
]

function errorDetail(err: unknown, fallback: string): string {
  // Taxonomy-aware: the wording now reflects the ACTUAL failure kind instead
  // of blaming the backend for every error (401 → key issue, 429 → rate
  // limit, 503 → db, network → unreachable). Server "detail" wins when set.
  const apiError = toApiError(err)
  return apiError.serverDetail ?? apiError.message ?? fallback
}

export function ProviderKeys() {
  const [keys, setKeys] = useState<StoredApiKey[]>(() => getStoredKeys())
  const [activePrefix, setActivePrefix] = useState<string | null>(() => getActiveKey()?.key_prefix ?? null)
  const [statuses, setStatuses] = useState<Record<string, ProviderKeyStatus>>({})
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  const [saving, setSaving] = useState<string | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const activeKey = keys.find((key) => key.key_prefix === activePrefix && !key.revoked) ?? null
  const usableKeys = keys.filter((key) => !key.revoked)

  const syncLocal = useCallback(() => {
    setKeys(getStoredKeys())
    setActivePrefix(getActiveKey()?.key_prefix ?? null)
  }, [])

  const loadStatuses = useCallback(async (key: StoredApiKey | null) => {
    if (!key) {
      setStatuses({})
      return
    }
    setLoading(true)
    try {
      const rows = await getProviderKeys(key.api_key)
      setStatuses(Object.fromEntries(rows.map((row) => [row.provider_name, row])))
      setError(null)
    } catch (err) {
      const status = (err as { response?: { status?: number } }).response?.status
      if (status === 401) {
        markKeyRevoked(key.key_prefix)
        setStatuses({})
        setError('That RouteLLM key is no longer active. Select or generate another key.')
      } else {
        setError(errorDetail(err, 'Could not load provider-key status.'))
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // Provider-key status is fetched from the backend for the selected local
    // RouteLLM key; raw provider secrets are never persisted in the browser.
    // eslint-disable-next-line react/set-state-in-effect
    void loadStatuses(getActiveKey())
    const onChanged = () => {
      syncLocal()
      void loadStatuses(getActiveKey())
    }
    window.addEventListener(KEYS_CHANGED_EVENT, onChanged)
    window.addEventListener('storage', onChanged)
    return () => {
      window.removeEventListener(KEYS_CHANGED_EVENT, onChanged)
      window.removeEventListener('storage', onChanged)
    }
  }, [loadStatuses, syncLocal])

  const onGenerateKey = async () => {
    setGenerating(true)
    setError(null)
    try {
      const generated = await generateApiKey('Provider keys demo key')
      saveGeneratedKey(generated)
      syncLocal()
      await loadStatuses(getActiveKey())
    } catch {
      setError('Could not generate a RouteLLM key. Is the backend running?')
    } finally {
      setGenerating(false)
    }
  }

  const onSelectKey = (prefix: string) => {
    setActiveKey(prefix)
    syncLocal()
    setDrafts({})
    setNotice(null)
    const next = getStoredKeys().find((key) => key.key_prefix === prefix && !key.revoked) ?? null
    void loadStatuses(next)
  }

  const onSave = async (provider: ProviderMeta) => {
    const current = getActiveKey()
    const draft = (drafts[provider.name] ?? '').trim()
    if (!current) {
      setError('Select or generate a RouteLLM key first.')
      return
    }
    if (draft.length < 8) {
      setError(`${provider.display} key must be at least 8 characters.`)
      return
    }
    setSaving(provider.name)
    setError(null)
    setNotice(null)
    try {
      const status = await setProviderKey(current.api_key, provider.name, draft)
      setStatuses((prev) => ({ ...prev, [provider.name]: status }))
      setDrafts((prev) => ({ ...prev, [provider.name]: '' }))
      setNotice(`${provider.display} key encrypted and saved for ${current.key_prefix}…`)
    } catch (err) {
      setError(errorDetail(err, `Could not save the ${provider.display} key.`))
    } finally {
      setSaving(null)
    }
  }

  const onRemove = async (provider: ProviderMeta) => {
    const current = getActiveKey()
    if (!current) return
    setRemoving(provider.name)
    setError(null)
    setNotice(null)
    try {
      await deleteProviderKey(current.api_key, provider.name)
      setStatuses((prev) => ({
        ...prev,
        [provider.name]: {
          provider_name: provider.name,
          configured: false,
          key_hint: null,
          created_at: null,
          updated_at: null,
        },
      }))
      setDrafts((prev) => ({ ...prev, [provider.name]: '' }))
      setNotice(`${provider.display} key removed from ${current.key_prefix}…`)
    } catch (err) {
      setError(errorDetail(err, `Could not remove the ${provider.display} key.`))
    } finally {
      setRemoving(null)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Bring your own keys"
        title="Provider Keys"
        description="Store upstream Groq, Gemini, and DeepSeek keys against one RouteLLM key. Routing decrypts them only for that caller’s requests."
      />

      <GlassCard hoverable={false} className="border-success/25 bg-success/[0.06]">
        <div className="flex gap-3">
          <ShieldCheck size={18} className="mt-0.5 shrink-0 text-success" aria-hidden="true" />
          <div className="text-sm text-text-secondary">
            <p className="font-semibold text-text-primary">Encrypted at rest, scoped per RouteLLM key</p>
            <p className="mt-1">
              Provider keys are encrypted with a server-side Fernet key before storage and are never
              returned to the browser after saving. Once you configure at least one provider key,
              automatic routing and fallback stay within the providers you configured for the selected
              RouteLLM key.
            </p>
          </div>
        </div>
      </GlassCard>

      <GlassCard hoverable={false}>
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-[240px] flex-1 text-sm">
            <span className="rl-eyebrow mb-1.5 block">RouteLLM key</span>
            {usableKeys.length > 0 ? (
              <select
                aria-label="RouteLLM key"
                className="rl-input"
                value={activePrefix ?? ''}
                onChange={(event) => onSelectKey(event.target.value)}
              >
                {usableKeys.map((key) => (
                  <option key={key.key_prefix} value={key.key_prefix}>
                    {key.label} ({key.key_prefix}…)
                  </option>
                ))}
              </select>
            ) : (
              <p className="text-sm text-text-secondary">
                No active RouteLLM key in this browser. Generate one here or on the{' '}
                <Link to="/keys" className="text-accent underline-offset-2 hover:underline">
                  API Keys page
                </Link>
                .
              </p>
            )}
          </label>
          <button
            type="button"
            onClick={() => void onGenerateKey()}
            disabled={generating}
            className="rl-btn-secondary"
          >
            <KeyRound size={14} aria-hidden="true" />
            {generating ? 'Generating…' : 'Generate demo key'}
          </button>
        </div>
        {activeKey && (
          <p className="mt-3 text-xs text-text-secondary">
            Selected: <code className="font-mono text-text-primary">{activeKey.key_prefix}…</code>
            {loading && <span className="ml-2 text-accent">Loading provider status…</span>}
          </p>
        )}
      </GlassCard>

      {error && (
        <p role="alert" className="rounded-xl border border-danger/25 bg-danger/10 px-4 py-3 text-sm text-danger">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="rounded-xl border border-success/25 bg-success/10 px-4 py-3 text-sm text-success">
          {notice}
        </p>
      )}

      <section aria-label="Provider key sections" className="grid gap-4 lg:grid-cols-3">
        {PROVIDERS.map((provider) => {
          const status = statuses[provider.name]
          const configured = Boolean(status?.configured)
          const draft = drafts[provider.name] ?? ''
          const busy = saving === provider.name || removing === provider.name
          return (
            <GlassCard key={provider.name} hoverable={false} className="flex flex-col p-5">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h3 className="flex items-center gap-2 font-semibold text-text-primary">
                    <PlugZap size={16} className="text-accent" aria-hidden="true" />
                    {provider.display}
                  </h3>
                  <p className="mt-1 text-xs leading-relaxed text-text-secondary">{provider.description}</p>
                </div>
                <span
                  className={`rl-badge ${
                    configured
                      ? 'border-success/25 bg-success/10 text-success'
                      : 'border-white/[0.08] bg-white/[0.04] text-text-secondary'
                  }`}
                >
                  {configured ? 'Configured' : 'Not configured'}
                </span>
              </div>

              <div className="mt-4 min-h-[2.5rem] text-xs text-text-secondary">
                {configured && status?.key_hint ? (
                  <>
                    <p>
                      Stored hint: <code className="font-mono text-text-primary">{status.key_hint}</code>
                    </p>
                    {status.updated_at && <p className="mt-1">Updated {relativeTime(status.updated_at)}</p>}
                  </>
                ) : (
                  <p>No key stored for the selected RouteLLM key.</p>
                )}
              </div>

              <label className="mt-2 block text-sm">
                <span className="sr-only">{provider.display} API key</span>
                <input
                  type="password"
                  autoComplete="off"
                  spellCheck={false}
                  aria-label={`${provider.display} API key`}
                  className="rl-input font-mono"
                  placeholder={configured ? 'Enter a new key to replace…' : provider.placeholder}
                  value={draft}
                  disabled={!activeKey || busy}
                  onChange={(event) =>
                    setDrafts((prev) => ({ ...prev, [provider.name]: event.target.value }))
                  }
                />
              </label>

              <div className="mt-3 flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => void onSave(provider)}
                  disabled={!activeKey || busy || draft.trim().length < 8}
                  className="rl-btn-primary"
                  aria-label={`Save ${provider.display} key`}
                >
                  <Save size={14} aria-hidden="true" />
                  {saving === provider.name ? 'Saving…' : configured ? 'Replace key' : 'Save key'}
                </button>
                {configured && (
                  <button
                    type="button"
                    onClick={() => void onRemove(provider)}
                    disabled={!activeKey || busy}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-danger/25 px-3 py-1.5 text-xs font-medium text-danger transition hover:bg-danger/10 disabled:opacity-40"
                    aria-label={`Remove ${provider.display} key`}
                  >
                    <Trash2 size={12} aria-hidden="true" />
                    {removing === provider.name ? 'Removing…' : 'Remove'}
                  </button>
                )}
                <a
                  href={provider.consoleUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-auto inline-flex items-center gap-1 text-xs text-text-secondary transition hover:text-accent"
                >
                  Get a key <ExternalLink size={12} aria-hidden="true" />
                </a>
              </div>
            </GlassCard>
          )
        })}
      </section>

      <GlassCard hoverable={false} className="border-warning/25 bg-warning/[0.06]">
        <div className="flex gap-3">
          <AlertTriangle size={18} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
          <div className="text-sm text-text-secondary">
            <p className="font-semibold text-text-primary">Operator note</p>
            <p className="mt-1">
              The server must set <code className="font-mono">PROVIDER_KEY_ENCRYPTION_KEY</code>. If it is
              missing, saving keys and BYOK routing fail closed with HTTP 503. Callers with no stored
              provider keys keep the legacy operator/demo behavior. This is still lightweight demo auth,
              not a full account system.
            </p>
          </div>
        </div>
      </GlassCard>
    </div>
  )
}
