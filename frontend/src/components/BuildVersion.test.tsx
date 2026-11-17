/**
 * T1 stale-build detection: the banner and the footer chips.
 *
 * The failure this pins: after the PR #16 merge the owner kept an OLD server
 * and old bundle running, so the walk and diagnostics looked broken while
 * everything was actually fine after a restart. The banner must say that in
 * plain words — and stay quiet whenever either build identity is unknown, so
 * a missing value can never become a false "mismatch".
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import axios from 'axios'
import { BuildVersionBanner, BuildVersionChips } from './BuildVersion'
import { frontendBuildCommit } from '../buildInfo'

vi.mock('axios', async () => {
  const get = vi.fn()
  const isAxiosError = (e: unknown) =>
    Boolean(e && typeof e === 'object' && (e as { isAxiosError?: boolean }).isAxiosError)
  return { default: { get, isAxiosError }, isAxiosError }
})

vi.mock('../buildInfo', () => ({ frontendBuildCommit: vi.fn() }))

const mockedGet = vi.mocked(axios.get)
const mockedCommit = vi.mocked(frontendBuildCommit)

/** The bundle's frozen commit for this test, flipped per case. */
let bundleCommit: string | null = 'aaa1111'
/** The backend's /health build block, flipped per case. */
let backendBuild: Record<string, unknown> | null = { git_commit: 'aaa1111', git_commit_time: '2026-11-15T00:00:00+00:00' }

function answer() {
  if (backendBuild === null) return Promise.reject(new Error('Network Error'))
  return Promise.resolve({ data: { status: 'ok', build: backendBuild, components: {} } })
}

describe('BuildVersionBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    bundleCommit = 'aaa1111'
    backendBuild = { git_commit: 'aaa1111', git_commit_time: '2026-11-15T00:00:00+00:00' }
    mockedCommit.mockImplementation(() => bundleCommit ?? 'unknown')
    mockedGet.mockImplementation(() => answer())
  })

  it('stays quiet when frontend and backend commits match', async () => {
    render(<BuildVersionBanner />)
    await waitFor(() => expect(mockedGet).toHaveBeenCalled())
    expect(screen.queryByTestId('build-mismatch-banner')).not.toBeInTheDocument()
  })

  it('shows a banner naming BOTH commits when the bundle is stale', async () => {
    backendBuild = { git_commit: 'bbb2222', git_commit_time: '2026-11-16T00:00:00+00:00' }
    render(<BuildVersionBanner />)
    const banner = await screen.findByTestId('build-mismatch-banner')
    expect(banner).toHaveTextContent('aaa1111')
    expect(banner).toHaveTextContent('bbb2222')
    // The fix must be named: restart the launcher, which rebuilds.
    expect(banner).toHaveTextContent('python run.py')
  })

  it('stays quiet when the backend reports no build identity', async () => {
    backendBuild = { git_commit: null, git_commit_time: null }
    render(<BuildVersionBanner />)
    await waitFor(() => expect(mockedGet).toHaveBeenCalled())
    expect(screen.queryByTestId('build-mismatch-banner')).not.toBeInTheDocument()
  })

  it('stays quiet when the bundle has no build identity', () => {
    bundleCommit = null
    backendBuild = { git_commit: 'bbb2222', git_commit_time: '2026-11-16T00:00:00+00:00' }
    render(<BuildVersionBanner />)
    expect(screen.queryByTestId('build-mismatch-banner')).not.toBeInTheDocument()
  })

  it('stays quiet when /health does not answer', async () => {
    backendBuild = null
    render(<BuildVersionBanner />)
    await waitFor(() => expect(mockedGet).toHaveBeenCalled())
    expect(screen.queryByTestId('build-mismatch-banner')).not.toBeInTheDocument()
  })
})

describe('BuildVersionChips', () => {
  it('shows the frontend bundle commit and the running backend commit', async () => {
    backendBuild = { git_commit: 'bbb2222', git_commit_time: '2026-11-16T00:00:00+00:00' }
    render(<BuildVersionChips />)
    const chips = await screen.findByTestId('build-version-chips')
    expect(chips).toHaveTextContent('frontend')
    expect(chips).toHaveTextContent('aaa1111')
    expect(chips).toHaveTextContent('backend')
    expect(chips).toHaveTextContent('bbb2222')
  })

  it('shows a placeholder for the backend commit when /health does not answer', async () => {
    backendBuild = null
    render(<BuildVersionChips />)
    const chips = await screen.findByTestId('build-version-chips')
    await waitFor(() => expect(chips).toHaveTextContent('—'))
    expect(chips).toHaveTextContent('aaa1111')
  })
})
