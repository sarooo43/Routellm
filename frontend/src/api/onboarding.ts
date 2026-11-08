/**
 * Persistence for the first-run checklist.
 *
 * Rules that make an onboarding panel tolerable:
 *  - it is dismissible, and the dismissal is remembered;
 *  - a completed step is remembered, so the list shrinks instead of nagging;
 *  - storage failures (private mode, quota) never break the app.
 *
 * Keys are versioned so a future redesign can start clean instead of
 * resurrecting stale flags.
 */
const DISMISSED_KEY = 'routellm.onboarding.dismissed.v1'
const DONE_KEY = 'routellm.onboarding.doneSteps.v1'
export const ONBOARDING_CHANGED_EVENT = 'routellm-onboarding-changed'

function notify(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(ONBOARDING_CHANGED_EVENT))
  }
}

export function isOnboardingDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISSED_KEY) === 'true'
  } catch {
    return false
  }
}

export function dismissOnboarding(): void {
  try {
    localStorage.setItem(DISMISSED_KEY, 'true')
  } catch {
    /* storage unavailable: dismiss for this session only */
  }
  notify()
}

export function resetOnboarding(): void {
  try {
    localStorage.removeItem(DISMISSED_KEY)
    localStorage.removeItem(DONE_KEY)
  } catch {
    /* ignore */
  }
  notify()
}

export function completedSteps(): string[] {
  try {
    const raw = localStorage.getItem(DONE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function markStepDone(stepId: string): void {
  const steps = completedSteps()
  if (steps.includes(stepId)) return
  try {
    localStorage.setItem(DONE_KEY, JSON.stringify([...steps, stepId]))
  } catch {
    /* ignore */
  }
  notify()
}

/** Test/reset helper: forget everything this module persists. */
export function clearOnboardingState(): void {
  try {
    localStorage.removeItem(DISMISSED_KEY)
    localStorage.removeItem(DONE_KEY)
  } catch {
    /* ignore */
  }
  notify()
}
