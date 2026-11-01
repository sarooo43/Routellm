import { act, cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { LogsResponse } from '../types/api'
import { LiveFeedTicker } from './LiveFeedTicker'
import type { TaskLog } from '../types/api'

vi.mock('../api/client', () => ({
  getLogs: vi.fn(),
  api: { create: vi.fn() },
}))

import { getLogs } from '../api/client'

const mockGetLogs = vi.mocked(getLogs)

function makeLog(overrides: Partial<TaskLog> = {}): TaskLog {
  return {
    request_id: 'req-1',
    task_type: 'code',
    selected_model: 'groq',
    confidence_score: 0.42,
    status: 'success',
    latency_ms: 120.5,
    tokens_used: 37,
    error_message: null,
    fallback_chain: [],
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

function page(logs: TaskLog[]): LogsResponse {
  return { total: logs.length, logs }
}

const FIRST_PAGE = page([
  makeLog({ request_id: 'req-a', task_type: 'code', selected_model: 'groq' }),
  makeLog({ request_id: 'req-b', task_type: 'qa', selected_model: 'deepseek',
            status: 'failed', error_message: 'all_providers_failed' }),
])

const SECOND_PAGE = page([
  makeLog({ request_id: 'req-c', task_type: 'summarization', selected_model: 'gemini' }),
  ...FIRST_PAGE.logs,
])

describe('LiveFeedTicker', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('renders the most recent log entries from the API', async () => {
    mockGetLogs.mockResolvedValue(FIRST_PAGE)

    render(<MemoryRouter><LiveFeedTicker /></MemoryRouter>)

    // Initial poll fires immediately on mount.
    await act(async () => {})

    expect(screen.getByText('groq')).toBeInTheDocument()
    expect(screen.getByText('deepseek')).toBeInTheDocument()
    expect(screen.getByText('code')).toBeInTheDocument()
    expect(screen.getByText('qa')).toBeInTheDocument()
  })

  it('renders a genuinely new entry appearing on the next poll without crashing', async () => {
    mockGetLogs.mockResolvedValueOnce(FIRST_PAGE).mockResolvedValue(SECOND_PAGE)

    render(<MemoryRouter><LiveFeedTicker /></MemoryRouter>)
    await act(async () => {})

    expect(screen.queryByText('summarization')).not.toBeInTheDocument()

    // Advance past the 3s polling interval — the new req-c entry arrives.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(screen.getByText('summarization')).toBeInTheDocument()
    expect(screen.getByText('gemini')).toBeInTheDocument()
    // Existing entries survive the update too.
    expect(screen.getByText('groq')).toBeInTheDocument()
  })

  it('keeps the last known items if a poll fails', async () => {
    mockGetLogs.mockResolvedValueOnce(FIRST_PAGE).mockRejectedValue(new Error('down'))

    render(<MemoryRouter><LiveFeedTicker /></MemoryRouter>)
    await act(async () => {})

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(screen.getByText('groq')).toBeInTheDocument()
  })
})
