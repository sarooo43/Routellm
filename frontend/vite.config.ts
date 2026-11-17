import react from '@vitejs/plugin-react'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

/**
 * T1 stale-build detection: stamp every build with the git commit it was
 * produced from. The dashboard compares this against /health's backend
 * commit and shows a banner when they differ (the stale-bundle problem the
 * owner hit: an old server + old bundle kept running after the merge).
 * Fail-soft: any git problem reads as 'unknown' and the banner stays quiet.
 */
function frontendBuildCommit(): string {
  const here = fileURLToPath(new URL('.', import.meta.url))
  try {
    const out = execFileSync('git', ['rev-parse', '--short', 'HEAD'], {
      cwd: here,
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 10_000,
    })
      .toString()
      .trim()
    return out || 'unknown'
  } catch {
    return 'unknown'
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  define: {
    __ROUTELLM_BUILD_COMMIT__: JSON.stringify(frontendBuildCommit()),
  },
  server: {
    host: '0.0.0.0',
    allowedHosts: true,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/v1': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    css: false,
    // The count-up / framer-motion animation tests use requestAnimationFrame,
    // which slows down under parallel-file CPU contention — without generous
    // windows the suite flakes even though every assertion is correct.
    testTimeout: 15_000,
  },
})
