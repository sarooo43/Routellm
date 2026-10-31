import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RoutingExplorer } from './RoutingExplorer'
import type { TaskLog } from '../types/api'

const state = {
  logs: [] as TaskLog[],
  logsTotal: 0,
  fetchLogs: vi.fn().mockResolvedValue(undefined),
}

vi.mock('../store/useAppStore', () => ({
  useAppStore: (selector: (s: typeof state) => unknown) => selector(state),
}))

vi.mock('../api/client', () => ({
  postFeedback: vi.fn().mockResolvedValue({ status: 'recorded', request_id: 'req-1' }),
}))

import * as apiClient from '../api/client'

const postFeedback = vi.mocked(apiClient.postFeedback)

const successLog: TaskLog = {
  request_id: 'req-1',
  task_type: 'summarization',
  selected_model: 'groq',
  confidence_score: 0.6,
  status: 'success',
  latency_ms: 420,
  tokens_used: 120,
  error_message: null,
  fallback_chain: [
    { provider_name: 'groq', success: true, error: null, latency_ms: 410 },
  ],
  created_at: new Date().toISOString(),
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  state.logs = []
  state.logsTotal = 0
})

function renderWithLog(log: TaskLog) {
  state.logs = [log]
  state.logsTotal = 1
  render(<RoutingExplorer />)
  // Expand the first body row (the whole row toggles the panel).
  fireEvent.click(screen.getAllByRole('row')[1])
}

describe('RoutingExplorer feedback widget', () => {
  it('clicking 👍 POSTs the feedback endpoint with correct request_id and rating, then shows the confirmation', async () => {
    renderWithLog(successLog)

    expect(await screen.findByTestId('feedback-up')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('feedback-up'))

    await waitFor(() => expect(postFeedback).toHaveBeenCalledTimes(1))
    expect(postFeedback).toHaveBeenCalledWith({
      request_id: 'req-1',
      rating: 'positive',
    })

    // Buttons are replaced by the fade-in confirmation.
    await waitFor(() =>
      expect(screen.getByTestId('feedback-thanks')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('feedback-up')).not.toBeInTheDocument()
    expect(screen.queryByTestId('feedback-down')).not.toBeInTheDocument()
  })

  it('does not offer a rating widget on failed requests', () => {
    renderWithLog({ ...successLog, status: 'failed', selected_model: null })
    expect(screen.queryByTestId('feedback-up')).not.toBeInTheDocument()
    expect(screen.queryByTestId('feedback-down')).not.toBeInTheDocument()
  })

  it('renders the already-rated state immediately when feedback_given is true', async () => {
    renderWithLog({ ...successLog, feedback_given: true })

    // Confirmation shown on load, without any click or API call.
    expect(await screen.findByTestId('feedback-thanks')).toBeInTheDocument()
    expect(screen.queryByTestId('feedback-up')).not.toBeInTheDocument()
    expect(postFeedback).not.toHaveBeenCalled()
  })
})
