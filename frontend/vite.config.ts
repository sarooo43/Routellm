import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
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
