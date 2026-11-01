import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  deleteProviderKey,
  ensureActiveApiKey,
  getProviderKeys,
  postRoute,
  setProviderKey,
} from './client'
import { getActiveKey, getStoredKeys } from './keyStorage'

vi.mock('axios', async () => {
  const post = vi.fn()
  const get = vi.fn()
  const put = vi.fn()
  const del = vi.fn()
  return {
    default: {
      create: () => ({ post, get, put, delete: del }),
    },
  }
})

import { api } from './client'

const generated = {
  api_key: 'rk_abcdefghijklmnopqrstuvwxyz123456',
  key_prefix: 'rk_abcde',
  label: 'Dashboard demo key',
  warning: 'Save this key now — it will not be shown again.',
}

beforeEach(() => {
  localStorage.clear()
  vi.clearAllMocks()
})

describe('dashboard API key integration', () => {
  it('auto-generates a local key on first Try it Live request and sends X-API-Key', async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({ data: generated })
      .mockResolvedValueOnce({ data: { success: true, request_id: 'req-1' } })

    await postRoute({ prompt: 'hello' })

    expect(api.post).toHaveBeenNthCalledWith(1, '/api/v1/keys/generate', {
      label: 'Dashboard demo key',
    })
    expect(api.post).toHaveBeenNthCalledWith(
      2,
      '/api/v1/route',
      { prompt: 'hello' },
      { headers: { 'X-API-Key': generated.api_key } },
    )
    expect(getActiveKey()?.api_key).toBe(generated.api_key)
    expect(getStoredKeys()).toHaveLength(1)
  })

  it('sends BYOK provider-key requests with the caller X-API-Key header', async () => {
    vi.mocked(api.get).mockResolvedValueOnce({ data: [] })
    vi.mocked(api.put).mockResolvedValueOnce({ data: { provider_name: 'groq', configured: true } })
    vi.mocked(api.delete).mockResolvedValueOnce({ data: { status: 'deleted', provider_name: 'groq' } })

    await getProviderKeys('rk_test')
    await setProviderKey('rk_test', 'groq', 'gsk_provider_secret')
    await deleteProviderKey('rk_test', 'groq')

    expect(api.get).toHaveBeenCalledWith('/api/v1/provider-keys', {
      headers: { 'X-API-Key': 'rk_test' },
    })
    expect(api.put).toHaveBeenCalledWith(
      '/api/v1/provider-keys/groq',
      { api_key: 'gsk_provider_secret' },
      { headers: { 'X-API-Key': 'rk_test' } },
    )
    expect(api.delete).toHaveBeenCalledWith('/api/v1/provider-keys/groq', {
      headers: { 'X-API-Key': 'rk_test' },
    })
  })

  it('reuses the selected local key without generating another', async () => {
    vi.mocked(api.post)
      .mockResolvedValueOnce({ data: generated })
      .mockResolvedValueOnce({ data: { success: true } })
    await ensureActiveApiKey()
    vi.mocked(api.post).mockClear()
    vi.mocked(api.post).mockResolvedValueOnce({ data: { success: true } })

    await postRoute({ prompt: 'again' })

    expect(api.post).toHaveBeenCalledTimes(1)
    expect(api.post).toHaveBeenCalledWith(
      '/api/v1/route',
      { prompt: 'again' },
      { headers: { 'X-API-Key': generated.api_key } },
    )
  })
})
