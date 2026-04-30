import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import AccountManagement from '../views/AccountManagement.vue'
import MaterialManagement from '../views/MaterialManagement.vue'
import PublishCenter from '../views/PublishCenter.vue'
import JobsView from '../views/JobsView.vue'
import LoginView from '../views/LoginView.vue'
import About from '../views/About.vue'

import { getToken } from '@/utils/auth'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: LoginView,
    meta: { public: true }
  },
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard
  },
  {
    path: '/account-management',
    name: 'AccountManagement',
    component: AccountManagement
  },
  {
    path: '/material-management',
    name: 'MaterialManagement',
    component: MaterialManagement
  },
  {
    path: '/publish-center',
    name: 'PublishCenter',
    component: PublishCenter
  },
  {
    path: '/jobs',
    name: 'Jobs',
    component: JobsView
  },
  {
    path: '/about',
    name: 'About',
    component: About
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

// Route guard. We do not block the login screen itself, and we let any
// route through if a token is already present — the per-request 401
// interceptor will bounce the user back if the token is rejected by the
// backend. This keeps the UX simple in open-mode deployments.
router.beforeEach((to) => {
  if (to.meta && to.meta.public) {
    return true
  }
  if (!getToken()) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  return true
})

export default router
