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
const Calendar           = () => import('../views/CalendarView.vue')
const Queue              = () => import('../views/QueueView.vue')
const Settings           = () => import('../views/SettingsView.vue')
const Help               = () => import('../views/HelpView.vue')
const PrivacyPolicy      = () => import('../views/PrivacyPolicy.vue')
const TermsOfUse         = () => import('../views/TermsOfUse.vue')
const DataDeletion       = () => import('../views/DataDeletion.vue')

/* Section layout wrappers */
const PublishLayout   = () => import('../components/PublishLayout.vue')
const LibraryLayout   = () => import('../components/LibraryLayout.vue')
const AnalyticsLayout = () => import('../components/AnalyticsLayout.vue')

/* ------------------------------------------------------------------ */
/*  Route table — section + tab model                                  */
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
  {
    path: '/data-deletion',
    name: 'DataDeletion',
    component: DataDeletion,
    meta: { public: true, publicLayout: true, title: 'Data Deletion — Socialupload' }
  },

  /* Dashboard — top-level, no tabs */
  {
    path: '/dashboard',
    name: 'Dashboard',
    component: Dashboard,
    meta: { title: 'Dashboard — Socialupload' }
  },

  /* Publish section — has tabs: compose / calendar / queue */
  {
    path: '/publish',
    component: PublishLayout,
    children: [
      { path: '',            redirect: '/publish/compose' },
      { path: 'compose',    component: PublishCenter, meta: { title: 'Publish Center — Socialupload' } },
      { path: 'calendar',   component: Calendar,       meta: { title: 'Calendar — Socialupload' } },
      { path: 'queue',      component: Queue,           meta: { title: 'Queue — Socialupload' } },
    ]
  },

  /* Library section — tabs: media / templates / brands */
  {
    path: '/library',
    component: LibraryLayout,
    children: [
      { path: '',            redirect: '/library/media' },
      { path: 'media',      component: MaterialManagement, meta: { title: 'Media Library — Socialupload' } },
      { path: 'templates',  component: TemplateManagement,  meta: { title: 'Templates — Socialupload' } },
      { path: 'brands',     component: ProfileManagement,   meta: { title: 'Brands — Socialupload' } },
    ]
  },

  /* Accounts — top-level, no tabs */
  {
    path: '/accounts',
    name: 'Accounts',
    component: AccountManagement,
    meta: { title: 'Accounts — Socialupload' }
  },

  /* Analytics section — tabs: overview / campaigns */
  {
    path: '/analytics',
    component: AnalyticsLayout,
    children: [
      { path: '',             redirect: '/analytics/overview' },
      { path: 'overview',     component: VideoAnalytics,  meta: { title: 'Analytics — Socialupload' } },
      { path: 'campaigns',    component: CampaignBuilder, meta: { title: 'Campaigns — Socialupload' } },
    ]
  },

  /* Footer nav items */
  {
    path: '/settings',
    name: 'Settings',
    component: Settings,
    meta: { title: 'Settings — Socialupload' }
  },
  {
    path: '/help',
    name: 'Help',
    component: Help,
    meta: { title: 'Help & Support — Socialupload' }
  },

  /* Internal pages (accessible via Settings) — no sidebar chrome */
  {
    path: '/jobs',
    component: JobsView,
    meta: { title: 'Jobs — Socialupload' }
  },
  {
    path: '/batch-upload',
    component: BatchUpload,
    meta: { title: 'Batch Upload — Socialupload' }
  },
  {
    path: '/sheet-exports',
    component: SheetExports,
    meta: { title: 'Sheet Exports — Socialupload' }
  },
  {
    path: '/api-docs',
    component: ApiDocs,
    meta: { title: 'API Docs — Socialupload' }
  },
  {
    path: '/tiktok-review',
    component: TikTokReviewStatus,
    meta: { title: 'TikTok Review — Socialupload' }
  },
  {
    path: '/oauth-review/:platform?',
    component: OAuthReviewStatus,
    meta: { title: 'OAuth Review — Socialupload' }
  },
  {
    path: '/about',
    component: About,
    meta: { title: 'About — Socialupload' }
  },

  /* ---- Backward-compat redirects ---- */
  { path: '/publish-center', redirect: '/publish/compose' },
  { path: '/calendar',       redirect: '/publish/calendar' },
  { path: '/queue',          redirect: '/publish/queue' },
  { path: '/account-management', redirect: '/accounts' },
  { path: '/profile-management',  redirect: '/library/brands' },
  { path: '/material-management', redirect: '/library/media' },
  { path: '/template-management', redirect: '/library/templates' },
  { path: '/video-analytics',    redirect: '/analytics/overview' },
  { path: '/campaign-builder',  redirect: '/analytics/campaigns' },

  /* Catch-all → dashboard */
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