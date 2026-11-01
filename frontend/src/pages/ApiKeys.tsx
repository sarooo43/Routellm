import { AnimatePresence, motion } from 'framer-motion'
import { Check, Copy, KeyRound, Plus, ShieldAlert, Trash2, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { generateApiKey, getKeyUsage, revokeKey } from '../api/client'
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
import { formatCount, relativeTime } from '../lib/format'
import type { GenerateKeyResponse, KeyUsageResponse } from '../types/api'

export function ApiKeys() {
  const [keys, setKeys] = useState<StoredApiKey[]>(() => getStoredKeys())
  const [activePrefix, setActivePrefix] = useState<string | null>(() => getActiveKey()?.key_prefix ?? null)
  const [usage, setUsage] = useState<Record<string, KeyUsageResponse>>({})
  const [label, setLabel] = useState('')
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [modalKey, setModalKey] = useState<GenerateKeyResponse | null>(null)
  const [copied, setCopied] = useState(false)
  const [revoking, setRevoking] = useState<string | null>(null)

  const syncLocal = useCallback(() => {
    setKeys(getStoredKeys())
    setActivePrefix(getActiveKey()?.key_prefix ?? null)
  }, [])

  const loadUsage = useCallback(async () => {
    const stored = getStoredKeys()
    const results = await Promise.all(
      stored
        .filter((key) => !key.revoked)
        .map(async (key) => {
          try {
            return [key.key_prefix, await getKeyUsage(key.api_key)] as const
          } catch {
            // A key revoked outside this browser should stop being offered.
            markKeyRevoked(key.key_prefix)
            return null
          }
        }),
    )
    setUsage(
      Object.fromEntries(
        results.filter((entry): entry is NonNullable<typeof entry> => entry !== null),
      ),
    )
    syncLocal()
  }, [syncLocal])

  const refresh = useCallback(async () => {
    syncLocal()
    await loadUsage()
  }, [syncLocal, loadUsage])

  useEffect(() => {
    // Usage is fetched asynchronously from the backend; local key rows are
    // already initialized from localStorage above.
    // eslint-disable-next-line react/set-state-in-effect
    void loadUsage()
    const onChanged = () => void refresh()
    window.addEventListener(KEYS_CHANGED_EVENT, onChanged)
    window.addEventListener('storage', onChanged)
    return () => {
      window.removeEventListener(KEYS_CHANGED_EVENT, onChanged)
      window.removeEventListener('storage', onChanged)
    }
  }, [loadUsage, refresh])

  const onGenerate = async () => {
    setGenerating(true)
    setError(null)
    try {
      const generated = await generateApiKey(label.trim() || 'Unnamed Key')
      saveGeneratedKey(generated)
      setModalKey(generated)
      setCopied(false)
      setLabel('')
      await refresh()
    } catch {
      setError('Could not generate a key. Is the backend running?')
    } finally {
      setGenerating(false)
    }
  }

  const onCopy = async () => {
    if (!modalKey) return
    try {
      await navigator.clipboard.writeText(modalKey.api_key)
      setCopied(true)
    } catch {
      setError('Could not copy automatically. Select the key and copy it manually.')
    }
  }

  const onRevoke = async (key: StoredApiKey) => {
    setRevoking(key.key_prefix)
    setError(null)
    try {
      await revokeKey(key.api_key)
      markKeyRevoked(key.key_prefix)
      await refresh()
    } catch {
      setError(`Could not revoke ${key.key_prefix}.`)
    } finally {
      setRevoking(null)
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Access"
        title="API Keys"
        description="Generate a lightweight demo key, use it on protected routing endpoints, and track its usage. No accounts, passwords, or OAuth."
      />

      <GlassCard hoverable={false} className="border-warning/25 bg-warning/[0.06]">
        <div className="flex gap-3">
          <ShieldAlert size={18} className="mt-0.5 shrink-0 text-warning" aria-hidden="true" />
          <div className="text-sm text-text-secondary">
            <p className="font-semibold text-text-primary">Demo/portfolio auth only</p>
            <p className="mt-1">
              The backend stores only SHA-256 hashes. This browser keeps raw demo keys in
              localStorage so you can reuse them here; they are shown in full only once at
              creation. Do not use this flow for production secrets.
            </p>
          </div>
        </div>
      </GlassCard>

      <GlassCard hoverable={false}>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex-1 min-w-[220px] text-sm">
            <span className="rl-eyebrow mb-1.5 block">
              Key label
            </span>
            <input
              type="text"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              placeholder="e.g. Cursor dev key"
              aria-label="Key label"
              className="rl-input"
            />
          </label>
          <button
            type="button"
            onClick={() => void onGenerate()}
            disabled={generating}
            className="rl-btn-primary"
          >
            <Plus size={14} aria-hidden="true" />
            {generating ? 'Generating…' : 'Generate New Key'}
          </button>
        </div>
        {error && (
          <p role="alert" className="mt-3 text-sm text-danger">
            {error}
          </p>
        )}
      </GlassCard>

      <section aria-label="My keys" className="space-y-3">
        <h3 className="rl-eyebrow">
          My Keys ({keys.length})
        </h3>
        {keys.length === 0 ? (
          <GlassCard hoverable={false} className="border-dashed py-12 text-center text-sm text-text-secondary">
            <span className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-xl bg-accent/10 text-accent">
              <KeyRound size={22} aria-hidden="true" />
            </span>
            No keys in this browser yet. Generate one, or send a request from “Try it Live”
            to create a demo key automatically.
          </GlassCard>
        ) : (
          keys.map((key) => {
            const stats = usage[key.key_prefix]
            const isActive = activePrefix === key.key_prefix && !key.revoked
            return (
              <GlassCard key={key.key_prefix} hoverable={false} className="p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h4 className="font-semibold">{key.label}</h4>
                      <code className="rounded-md border border-white/[0.06] bg-black/40 px-2 py-0.5 font-mono text-xs text-text-secondary">
                        {key.key_prefix}…
                      </code>
                      {isActive && (
                        <span className="rl-badge border-success/25 bg-success/10 text-success">
                          Active
                        </span>
                      )}
                      {key.revoked && (
                        <span className="rl-badge border-danger/25 bg-danger/10 text-danger">
                          Revoked
                        </span>
                      )}
                    </div>
                    <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 text-xs text-text-secondary sm:grid-cols-3">
                      <div>
                        <dt>Requests</dt>
                        <dd className="mt-0.5 font-semibold tabular-nums text-text-primary">
                          {stats ? formatCount(stats.total_requests) : '—'}
                        </dd>
                      </div>
                      <div>
                        <dt>Tokens used</dt>
                        <dd className="mt-0.5 font-semibold tabular-nums text-text-primary">
                          {stats ? formatCount(stats.total_tokens_used) : '—'}
                        </dd>
                      </div>
                      <div>
                        <dt>Last used</dt>
                        <dd className="mt-0.5 text-text-primary">
                          {stats?.last_used_at ? relativeTime(stats.last_used_at) : 'Never'}
                        </dd>
                      </div>
                    </dl>
                  </div>

                  <div className="flex items-center gap-2">
                    {!key.revoked && !isActive && (
                      <button
                        type="button"
                        onClick={() => setActiveKey(key.key_prefix)}
                        className="rl-btn-ghost border border-white/[0.07]"
                      >
                        Use key
                      </button>
                    )}
                    {!key.revoked && (
                      <button
                        type="button"
                        onClick={() => void onRevoke(key)}
                        disabled={revoking === key.key_prefix}
                        aria-label={`Revoke ${key.label}`}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-danger/25 px-3 py-1.5 text-xs font-medium text-danger transition hover:bg-danger/10 disabled:opacity-40"
                      >
                        <Trash2 size={12} aria-hidden="true" />
                        {revoking === key.key_prefix ? 'Revoking…' : 'Revoke'}
                      </button>
                    )}
                  </div>
                </div>
              </GlassCard>
            )
          })
        )}
      </section>

      <AnimatePresence>
        {modalKey && (
          <motion.div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            role="dialog"
            aria-modal="true"
            aria-labelledby="new-key-title"
          >
            <motion.div
              initial={{ scale: 0.96, y: 12 }}
              animate={{ scale: 1, y: 0 }}
              exit={{ scale: 0.96, y: 12 }}
              className="w-full max-w-lg rounded-2xl border border-white/[0.08] bg-[#121418] p-6 shadow-2xl shadow-black/60"
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 id="new-key-title" className="text-lg font-semibold">
                    Your new API key
                  </h3>
                  <p className="mt-1 text-sm text-text-secondary">{modalKey.label}</p>
                </div>
                <button
                  type="button"
                  aria-label="Close"
                  onClick={() => setModalKey(null)}
                  className="rounded-lg p-1.5 text-text-secondary transition hover:bg-white/5 hover:text-text-primary"
                >
                  <X size={18} />
                </button>
              </div>

              <div className="mt-4 rounded-xl border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-warning">
                This key won’t be shown again — copy it now.
              </div>

              <div className="mt-4 flex items-center gap-2 rounded-xl border border-white/[0.07] bg-black/40 p-3">
                <code data-testid="generated-raw-key" className="min-w-0 flex-1 break-all font-mono text-sm">
                  {modalKey.api_key}
                </code>
                <button
                  type="button"
                  onClick={() => void onCopy()}
                  className="rl-btn-primary shrink-0 px-3 py-2 text-xs"
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>

              <button
                type="button"
                onClick={() => setModalKey(null)}
                className="rl-btn-secondary mt-5 w-full"
              >
                I’ve saved it
              </button>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
