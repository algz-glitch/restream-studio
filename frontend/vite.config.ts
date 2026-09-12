import { defineConfig } from 'vite'

export default defineConfig({
  esbuild: { jsx: 'automatic' },
  build: {
    outDir: '../src/restream_studio/static',
    emptyOutDir: true,
  },
  server: {
    host: '127.0.0.1',
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/tests/setup.ts',
    include: ['src/tests/**/*.test.{ts,tsx}'],
    css: true,
  },
})
