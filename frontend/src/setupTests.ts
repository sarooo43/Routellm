import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { configure } from '@testing-library/dom'
import { afterEach } from 'vitest'

// Without this, a render from one test stays mounted into the next one: queries
// then match multiple elements ("Found multiple elements by: [data-testid=…]")
// and pass or fail depending on file order. Vitest does not auto-cleanup the
// way Jest's jsdom environment does.
afterEach(() => {
  cleanup()
})

// rAF-driven count-up animations need headroom during parallel test files;
// the default 1000ms waitFor window flakes under CPU contention.
configure({ asyncUtilTimeout: 5000 })
