import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import pinia from './stores'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import './styles/modern.css'
import { useAuthStore } from '@/stores/auth'

const app = createApp(App)

/* Register Element Plus icons globally */
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

// Pinia must be active before the router guard (which reads the auth store)
// and before we resolve the Google session below.
app.use(pinia)
app.use(ElementPlus)

// Resolve the current session (Google cookie -> user/workspace) before the
// first navigation so the guard doesn't bounce an authenticated user to
// /login. A failed/absent session simply leaves the app in token-only mode.
const authStore = useAuthStore()
authStore.bootstrap().finally(() => {
  app.use(router)
  app.mount('#app')
})
