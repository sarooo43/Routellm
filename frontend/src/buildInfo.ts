/**
 * T1 stale-build detection: the commit this frontend bundle was built from.
 *
 * vite.config injects `__ROUTELLM_BUILD_COMMIT__` via `define`, so the value
 * is frozen into the bundle at build time. `typeof` keeps this safe when the
 * bundle was produced by tooling without the define (the identifier stays
 * undeclared and typeof never throws on undeclared identifiers).
 */
declare const __ROUTELLM_BUILD_COMMIT__: string | undefined

export function frontendBuildCommit(): string {
  return typeof __ROUTELLM_BUILD_COMMIT__ === 'string' && __ROUTELLM_BUILD_COMMIT__
    ? __ROUTELLM_BUILD_COMMIT__
    : 'unknown'
}
