import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'

// Unit regressions are deliberately separate from Playwright/demo recording.
// Do not merge this into vite.config.js: production build settings stay unchanged.
export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  test: {
    environment: 'node',
    // Client SFC transforms for the in-memory renderer; no browser dependency.
    testTransformMode: { web: ['**/src/components/**/*.test.js', '**/src/views/**/*.test.js'] },
    include: ['src/**/__tests__/**/*.test.{js,ts}'],
    restoreMocks: true,
  },
})
