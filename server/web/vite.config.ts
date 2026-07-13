import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built assets are committed to dist/ and served by FastAPI, so `git pull` self-update ships
// the UI with no Node on the server. `npm run dev` proxies API calls to a local uvicorn.
const api = ['/session', '/setup', '/config', '/check', '/preview', '/print', '/alert',
  '/ingest', '/watchtower', '/admin', '/status', '/fonts', '/scout.py', '/install-scout']

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { outDir: 'dist', assetsDir: 'assets', emptyOutDir: true },
  server: {
    proxy: Object.fromEntries(api.map(p => [p, { target: 'http://localhost:8000', ws: p === '/messages' }])),
  },
})
