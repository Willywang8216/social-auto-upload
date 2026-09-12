import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

/* Production serves the legal documents at extension-less paths — Flask routes
 * /privacy, /terms and /data-deletion onto the matching *.html file — and the
 * published documents are the only copies of those texts, so the in-app links
 * point straight at those paths. Mirror the mapping here, otherwise clicking
 * "Privacy Policy" in dev or `npm run preview` lands on a 404 and the pressure
 * is to reintroduce a second, drift-prone copy of the policy inside the SPA. */
const LEGAL_DOCUMENT_ALIASES = {
  '/privacy': '/privacy-policy.html',
  '/terms': '/terms-of-service.html',
  '/data-deletion': '/data-deletion.html',
}

function legalDocumentAliases() {
  const rewrite = (req, _res, next) => {
    const [path, query] = (req.url || '').split('?')
    const target = LEGAL_DOCUMENT_ALIASES[(path || '').replace(/\/$/, '')]
    if (target) req.url = query ? `${target}?${query}` : target
    next()
  }
  return {
    name: 'sau-legal-document-aliases',
    // Block bodies, not expression bodies: Vite treats a *returned* value from
    // these hooks as a post-middleware hook and will call it, and
    // `middlewares.use()` returns the connect app itself.
    configureServer(server) {
      server.middlewares.use(rewrite)
    },
    configurePreviewServer(server) {
      server.middlewares.use(rewrite)
    },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue(), legalDocumentAliases()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  css: {
    preprocessorOptions: {
      scss: {
        // 移除自动导入，改用@use语法
      }
    }
  },
  server: {
    port: 5173,
    open: true,
    proxy: {
      '/api': {
        target: 'http://localhost:5409',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, '')
      }
    }
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1600,
    rollupOptions: {
      output: {
        manualChunks: {
          vue: ['vue', 'vue-router', 'pinia'],
          elementPlus: ['element-plus'],
          utils: ['axios']
        }
      }
    }
  }
})
