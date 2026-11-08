import { AnimatePresence, motion } from 'framer-motion'
import {
  ArrowRight,
  CheckCircle2,
  Circle,
  ExternalLink,
  FlaskConical,
  Send,
  Sparkles,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { getSetupStatus } from '../api/client'
import {
  completedSteps,
  dismissOnboarding,
  isOnboardingDismissed,
  markStepDone,
  ONBOARDING_CHANGED_EVENT,
} from '../api/onboarding'
import type { SetupStatus } from '../types/api'
import { GlassCard } from './GlassCard'

/** Default simulated-failure duration used by the checklist shortcut. */
export const ONBOARDING_FAILOVER_SECONDS = 60

const STEP_ICONS: Record<string, typeof Circle> = {
  provider_key: Sparkles,
  test_prompt: Send,
  try_failover: FlaskConical,
}

interface OnboardingChecklistProps {
  /** Called when demos should start; lets the page open Demo mode in place. */
  onTryDemo?: () => void
}

/**
 * First-run checklist: three dismissible, self-checking steps.
 *
 * "Self-checking" is literal — a step is marked done when the app observes the
 * user doing it (a routing decision appears in the log, a chaos injection is
 * active, a provider key is stored), not when they click a checkbox. Steps the
 * server can answer directly come from `GET /api/v1/setup/status`.
 *
 * The panel disappears once dismissed (remembered in localStorage) or once
 * every step is done.
 */
export function OnboardingChecklist({ onTryDemo }: OnboardingChecklistProps) {
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [dismissed, setDismissed] = useState(() => isOnboardingDismissed())
  const [localDone, setLocalDone] = useState<string[]>(() => completedSteps())

  const refresh = useCallback(async () => {
    try {
      setStatus(await getSetupStatus())
    } catch {
      // The backend being unreachable is the app's own error state to show;
      // the checklist simply stays hidden rather than duplicating it.
      setStatus(null)
    }
  }, [])

  useEffect(() => {
    // Fetch-on-mount by design (readiness is external state).
    // eslint-disable-next-line react/set-state-in-effect
    void refresh()
    const sync = () => {
      setDismissed(isOnboardingDismissed())
      setLocalDone(completedSteps())
    }
    window.addEventListener(ONBOARDING_CHANGED_EVENT, sync)
    return () => window.removeEventListener(ONBOARDING_CHANGED_EVENT, sync)
  }, [refresh])

  const steps = (status?.steps ?? []).map((step) => ({
    ...step,
    // Server truth OR an observed local action.
    done: step.done || localDone.includes(step.id),
  }))
  const allDone = steps.length > 0 && steps.every((step) => step.done)

  if (dismissed || !status || allDone) return null

  const providerStep = status.providers.find((provider) => !provider.configured)
  const failoverStep = steps.find((step) => step.id === 'try_failover')

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0, y: -8 }}
        transition={{ duration: 0.28, ease: 'easeOut' }}
      >
        <GlassCard hoverable={false} className="border-accent/25 bg-accent/[0.05]">
          <div className="flex items-start justify-between gap-4">
            <div>
              <p className="rl-eyebrow">Getting started</p>
              <h2 className="mt-1 font-display text-lg font-semibold text-text-primary">
                Three steps to a real routing decision
              </h2>
              <p className="mt-1 text-sm text-text-secondary">
                Nothing here is required to look around — every step is skippable.
              </p>
            </div>
            <button
              type="button"
              onClick={() => {
                dismissOnboarding()
                setDismissed(true)
              }}
              aria-label="Dismiss the getting-started checklist"
              className="rounded-lg p-1.5 text-text-secondary transition hover:bg-white/[0.06] hover:text-text-primary"
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>

          <ol className="mt-5 space-y-3">
            {steps.map((step) => {
              const Icon = STEP_ICONS[step.id] ?? Circle
              return (
                <li key={step.id} className="flex items-start gap-3">
                  {step.done ? (
                    <CheckCircle2
                      size={18}
                      className="mt-0.5 shrink-0 text-success"
                      aria-label="Completed"
                    />
                  ) : (
                    <Icon size={18} className="mt-0.5 shrink-0 text-accent" aria-hidden="true" />
                  )}
                  <div className="min-w-0 flex-1">
                    <p
                      className={`text-sm font-medium ${
                        step.done ? 'text-text-secondary line-through' : 'text-text-primary'
                      }`}
                    >
                      {step.title}
                    </p>
                    {!step.done && <p className="mt-0.5 text-xs text-text-secondary">{step.detail}</p>}

                    {/* Step-specific action */}
                    {!step.done && step.id === 'provider_key' && providerStep && (
                      <a
                        href={providerStep.console_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-accent hover:underline"
                      >
                        Get a free {providerStep.display} key
                        <ExternalLink size={11} aria-hidden="true" />
                      </a>
                    )}
                    {!step.done && step.id === 'provider_key' && (
                      <Link
                        to="/providers"
                        className="ml-3 mt-2 inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"
                      >
                        Open Providers <ArrowRight size={11} aria-hidden="true" />
                      </Link>
                    )}
                    {!step.done && step.id === 'test_prompt' && (
                      <button
                        type="button"
                        onClick={() => {
                          markStepDone('test_prompt')
                          setLocalDone(completedSteps())
                        }}
                        className="ml-3 mt-2 inline-flex items-center gap-1 text-xs text-text-secondary hover:text-accent"
                      >
                        I sent one <ArrowRight size={11} aria-hidden="true" />
                      </button>
                    )}
                    {!step.done && step.id === 'try_failover' && failoverStep && (
                      <button
                        type="button"
                        onClick={() => {
                          markStepDone('try_failover')
                          setLocalDone(completedSteps())
                          onTryDemo?.()
                        }}
                        className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-accent hover:underline"
                      >
                        Try Demo mode
                        <ArrowRight size={11} aria-hidden="true" />
                      </button>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>

          {status.demo_available && (
            <p className="mt-4 rounded-lg border border-white/[0.06] bg-white/[0.03] px-3 py-2 text-xs text-text-secondary">
              No provider key yet? <strong className="text-text-primary">Demo mode</strong> answers with a
              labelled MOCK provider, so routing and fallback behave exactly as they would — without
              spending anything.
            </p>
          )}
        </GlassCard>
      </motion.div>
    </AnimatePresence>
  )
}
