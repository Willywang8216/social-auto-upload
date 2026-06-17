import { createRouter, createWebHashHistory } from 'vue-router'
import { getToken } from '@/utils/auth'

/* ------------------------------------------------------------------ */
/*  Lazy-loaded views — each becomes its own chunk                     */
/* ------------------------------------------------------------------ */
const LandingPage        = () => import('../views/LandingPage.vue')
const LoginView          = () => import('../views/LoginView.vue')
const Dashboard          = () => import('../views/Dashboard.vue')
const AccountManagement  = () => import('../views/AccountManagement.vue')
const ProfileManagement  = () => import('../views/ProfileManagement.vue')
const MaterialManagement = () => import('../views/MaterialManagement.vue')
const PublishCenter      = () => import('../views/PublishCenter.vue')
const TemplateManagement = () => import('../views/TemplateManagement.vue')
const JobsView           = () => import('../views/JobsView.vue')
const VideoAnalytics     = () => import('../views/VideoAnalytics.vue')
const About              = () => import('../views/About.vue')
const BatchUpload        = () => import('../views/BatchUpload.vue')
const CampaignBuilder    = () => import('../views/CampaignBuilder.vue')
const SheetExports       = () => import('../views/SheetExports.vue')
const ApiDocs            = () => import('../views/ApiDocs.vue')
const TikTokReviewStatus = () => import('../views/TikTokReviewStatus.vue')
const OAuthReviewStatus  = () => import('../views/OAuthReviewStatus.vue')
const PrivacyPolicy      = () => import('../views/PrivacyPolicy.vue')
const TermsOfUse         = () => import('../views/TermsOfUse.vue')

/* ------------------------------------------------------------------ */
/*  Route table                                                        */
/* ------------------------------------------------------------------ */
const routes = [
  /* Public pages — no sidebar chrome */
  {
    path: '/',
    name: 'LandingPage',
    component: LandingPage,
    meta: { public: true, publicLayout: true, title: 'Socialupload' }
  },
  {
    path: '/login',
    name: 'Login',
    component: LoginView,
    meta: { public: true, publicLayout: true, title: 'Sign In — Socialupload' }
  },
  {
    path: '/privacy',
    name: 'PrivacyPolicy',
    component: PrivacyPolicy,
    meta: { public: true, publicLayout: true, title: 'Privacy Policy — Socialupload' }
  },
  {
    path: '/terms',
    name: 'TermsOfUse',
    component: TermsOfUse,
    meta: { public: true, publicLayout: true, title: 'Terms of Service — Socialupload' }
  },

  /* Authenticated pages — wrapped in sidebar layout */
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: Dashboard,
    meta: { title: 'Dashboard — Socialupload' }
  },
  {
    path: '/account-management',
    name: 'AccountManagement',
    component: AccountManagement,
    meta: { title: 'Accounts — Socialupload' }
  },
  {
    path: '/profile-management',
    name: 'ProfileManagement',
    component: ProfileManagement,
    meta: { title: 'Profiles — Socialupload' }
  },
  {
    path: '/material-management',
    name: 'MaterialManagement',
    component: MaterialManagement,
    meta: { title: 'Materials — Socialupload' }
  },
  {
    path: '/publish-center',
    name: 'PublishCenter',
    component: PublishCenter,
    meta: { title: 'Publish Center — Socialupload' }
  },
  {
    path: '/template-management',
    name: 'TemplateManagement',
    component: TemplateManagement,
    meta: { title: 'Templates — Socialupload' }
  },
  {
    path: '/jobs',
    name: 'Jobs',
    component: JobsView,
    meta: { title: 'Jobs — Socialupload' }
  },
  {
    path: '/video-analytics',
    name: 'VideoAnalytics',
    component: VideoAnalytics,
    meta: { title: 'Analytics — Socialupload' }
  },
  {
    path: '/batch-upload',
    name: 'BatchUpload',
    component: BatchUpload,
    meta: { title: 'Batch Upload — Socialupload' }
  },
  {
    path: '/campaign-builder',
    name: 'CampaignBuilder',
    component: CampaignBuilder,
    meta: { title: 'Campaign Builder — Socialupload' }
  },
  {
    path: '/sheet-exports',
    name: 'SheetExports',
    component: SheetExports,
    meta: { title: 'Sheet Exports — Socialupload' }
  },
  {
    path: '/api-docs',
    name: 'ApiDocs',
    component: ApiDocs,
    meta: { title: 'API Docs — Socialupload' }
  },
  {
    path: '/tiktok-review',
    name: 'TikTokReviewStatus',
    component: TikTokReviewStatus,
    meta: { title: 'TikTok Review — Socialupload' }
  },
  {
    path: '/oauth-review/:platform?',
    name: 'OAuthReviewStatus',
    component: OAuthReviewStatus,
    meta: { title: 'OAuth Review — Socialupload' }
  },
  {
    path: '/about',
    name: 'About',
    component: About,
    meta: { title: 'About — Socialupload' }
  },

  /* Catch-all → dashboard (will be redirected by guard if unauthenticated) */
  {
    path: '/:pathMatch(.*)*',
    redirect: '/dashboard'
  }
]

/* ------------------------------------------------------------------ */
/*  Router instance                                                    */
/* ------------------------------------------------------------------ */
const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition
    return { top: 0 }
  }
})

/* ------------------------------------------------------------------ */
/*  Navigation guard                                                   */
/* ------------------------------------------------------------------ */
router.beforeEach((to) => {
  if (to.meta?.public) return true
  if (!getToken()) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta?.title || 'Socialupload'
})

export default router
