import { createRouter, createWebHashHistory } from 'vue-router'
import LandingPage from '../views/LandingPage.vue'
import Dashboard from '../views/Dashboard.vue'
import AccountManagement from '../views/AccountManagement.vue'
import MaterialManagement from '../views/MaterialManagement.vue'
import PublishCenter from '../views/PublishCenter.vue'
import JobsView from '../views/JobsView.vue'
import LoginView from '../views/LoginView.vue'
import About from '../views/About.vue'
import PrivacyPolicy from '../views/PrivacyPolicy.vue'
import TermsOfUse from '../views/TermsOfUse.vue'
import TikTokReviewStatus from '../views/TikTokReviewStatus.vue'
import OAuthReviewStatus from '../views/OAuthReviewStatus.vue'
import ProfileManagement from '../views/ProfileManagement.vue'
import TemplateManagement from '../views/TemplateManagement.vue'
import VideoAnalytics from '../views/VideoAnalytics.vue'

import { getToken } from '@/utils/auth'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: LoginView,
    meta: { public: true, publicLayout: true, title: 'Socialupload Sign In' }
  },
  {
    path: '/',
    name: 'LandingPage',
    component: LandingPage,
    meta: { public: true, publicLayout: true, title: 'Socialupload' }
  },
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: Dashboard,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/account-management',
    name: 'AccountManagement',
    component: AccountManagement,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/profile-management',
    name: 'ProfileManagement',
    component: ProfileManagement,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/material-management',
    name: 'MaterialManagement',
    component: MaterialManagement,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/publish-center',
    name: 'PublishCenter',
    component: PublishCenter,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/template-management',
    name: 'TemplateManagement',
    component: TemplateManagement,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/jobs',
    name: 'Jobs',
    component: JobsView,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/video-analytics',
    name: 'VideoAnalytics',
    component: VideoAnalytics,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/about',
    name: 'About',
    component: About,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/tiktok-review',
    name: 'TikTokReviewStatus',
    component: TikTokReviewStatus,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/oauth-review/:platform?',
    name: 'OAuthReviewStatus',
    component: OAuthReviewStatus,
    meta: { title: 'Socialupload' }
  },
  {
    path: '/privacy',
    name: 'PrivacyPolicy',
    component: PrivacyPolicy,
    meta: { public: true, publicLayout: true, title: 'Socialupload Privacy Policy' }
  },
  {
    path: '/terms',
    name: 'TermsOfUse',
    component: TermsOfUse,
    meta: { public: true, publicLayout: true, title: 'Socialupload Terms of Service' }
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

router.afterEach((to) => {
  document.title = to.meta?.title || 'Socialupload'
})

export default router
