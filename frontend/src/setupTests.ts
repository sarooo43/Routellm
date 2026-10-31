import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/dom'

// rAF-driven count-up animations need headroom during parallel test files;
// the default 1000ms waitFor window flakes under CPU contention.
configure({ asyncUtilTimeout: 5000 })
