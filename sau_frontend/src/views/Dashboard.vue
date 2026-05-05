<template>
  <div class="dashboard">
    <div class="page-header">
      <h1>自媒體自動化營運系統</h1>
    </div>

    <div class="dashboard-content">
      <el-row :gutter="20">
        <!-- 账号统计卡片 -->
        <el-col :span="8">
          <el-card class="stat-card">
            <div class="stat-card-content">
              <div class="stat-icon">
                <el-icon><User /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-value">{{ accountStats.total }}</div>
                <div class="stat-label">帳號總數</div>
              </div>
            </div>
            <div class="stat-footer">
              <div class="stat-detail">
                <span>正常：{{ accountStats.normal }}</span>
                <span>異常：{{ accountStats.abnormal }}</span>
              </div>
            </div>
          </el-card>
        </el-col>

        <!-- 平台统计卡片 -->
        <el-col :span="8">
          <el-card class="stat-card">
            <div class="stat-card-content">
              <div class="stat-icon platform-icon">
                <el-icon><Platform /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-value">{{ platformStats.total }}</div>
                <div class="stat-label">已接入平台</div>
              </div>
            </div>
            <div class="stat-footer">
              <div class="stat-detail">
                <el-tooltip
                  v-for="platform in dashboardPlatforms"
                  :key="platform.key"
                  :content="`${platform.label}帳號`"
                  placement="top"
                >
                  <el-tag size="small" :type="platform.tagType">
                    {{ platformStats.counts[platform.key] || 0 }}
                  </el-tag>
                </el-tooltip>
              </div>
            </div>
          </el-card>
        </el-col>

        <!-- 素材统计卡片 -->
        <el-col :span="8">
          <el-card class="stat-card">
            <div class="stat-card-content">
              <div class="stat-icon content-icon">
                <el-icon><Document /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-value">{{ contentStats.total }}</div>
                <div class="stat-label">素材總數</div>
              </div>
            </div>
            <div class="stat-footer">
              <div class="stat-detail">
                <span>影片：{{ contentStats.videos }}</span>
                <span>圖片：{{ contentStats.images }}</span>
                <span>其他：{{ contentStats.others }}</span>
              </div>
            </div>
          </el-card>
        </el-col>
        <el-col :span="8">
          <el-card class="stat-card">
            <div class="stat-card-content">
              <div class="stat-icon platform-icon">
                <el-icon><Connection /></el-icon>
              </div>
              <div class="stat-info">
                <div class="stat-value">{{ healthSummary.ready }}</div>
                <div class="stat-label">已就緒連線</div>
              </div>
            </div>
            <div class="stat-footer">
              <div class="stat-detail">
                <span>Configured：{{ healthSummary.configured }}</span>
                <span>Missing：{{ healthSummary.missing }}</span>
                <span>Refresh：{{ healthSummary.refreshable }}</span>
                <span>Check：{{ healthSummary.checkable }}</span>
              </div>
            </div>
          </el-card>
        </el-col>

      </el-row>

      <div class="recent-tasks">
        <div class="section-header">
          <h2>憑證到期風險</h2>
          <el-button text @click="goToAccountQueue({ sort: 'urgency' })">前往處理</el-button>
        </div>

        <div class="expiry-summary">
          <el-tag type="danger">Overdue: {{ healthSummary.expirySummary?.overdue || 0 }}</el-tag>
          <el-tag type="warning">24h: {{ healthSummary.expirySummary?.expiringWithin24h || 0 }}</el-tag>
          <el-tag>7d: {{ healthSummary.expirySummary?.expiringWithin7d || 0 }}</el-tag>
          <el-tag type="danger">Reconnect: {{ healthSummary.expirySummary?.reconnectRequired || 0 }}</el-tag>
        </div>

        <div class="expiry-actions">
          <el-button text @click="goToAccountQueue({ risk: 'expiring_24h', sort: 'expiry' })">查看 24h</el-button>
          <el-button text @click="goToAccountQueue({ risk: 'expiring_7d', sort: 'expiry' })">查看 7d</el-button>
          <el-button text @click="goToAccountQueue({ risk: 'overdue', sort: 'urgency' })">查看逾期</el-button>
          <el-button text @click="goToAccountQueue({ risk: 'reconnect_required', sort: 'urgency' })">查看需重連</el-button>
        </div>

        <el-table :data="healthSummary.expiringAccounts || []" style="width: 100%" v-loading="loading">
          <el-table-column prop="platform" label="平台" width="120" />
          <el-table-column prop="accountName" label="帳號" min-width="180" />
          <el-table-column prop="expiresAt" label="到期時間" width="220" />
          <el-table-column label="剩餘" width="140">
            <template #default="scope">
              {{ formatRemaining(scope.row.secondsRemaining) }}
            </template>
          </el-table-column>
          <el-table-column label="建議" width="120">
            <template #default="scope">
              <el-tag :type="scope.row.requiresReconnect ? 'danger' : 'warning'" effect="plain">{{ scope.row.recommendedAction }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120">
            <template #default="scope">
              <el-button text @click="goToAccountQueue({ risk: scope.row.requiresReconnect ? 'reconnect_required' : (scope.row.secondsRemaining <= 24 * 3600 ? 'expiring_24h' : 'expiring_7d'), sort: scope.row.requiresReconnect ? 'urgency' : 'expiry', platform: scope.row.platform })">前往</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-empty v-if="!loading && (healthSummary.expiringAccounts || []).length === 0" description="目前沒有 7 天內到期的帳號" />
      </div>

      <div class="recent-tasks">
        <div class="section-header">
          <h2>最近帳號操作</h2>
          <el-button text @click="navigateTo('/account-management')">帳號管理</el-button>
        </div>

        <el-table :data="healthSummary.recentEvents || []" style="width: 100%" v-loading="loading">
          <el-table-column prop="created_at" label="時間" width="180" />
          <el-table-column prop="platform" label="平台" width="120" />
          <el-table-column prop="account_name" label="帳號" min-width="180" />
          <el-table-column prop="action" label="操作" width="140" />
          <el-table-column label="結果" width="100">
            <template #default="scope">
              <el-tag :type="scope.row.status === 'ok' ? 'success' : 'danger'" effect="plain">{{ scope.row.status }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="summary" label="摘要" min-width="240" />
        </el-table>
      </div>

      <!-- 快速操作区域 -->
      <div class="quick-actions">
        <h2>快速操作</h2>
        <el-row :gutter="20">
          <el-col :span="6">
            <el-card class="action-card" @click="navigateTo('/account-management')">
              <div class="action-icon">
                <el-icon><UserFilled /></el-icon>
              </div>
              <div class="action-title">帳號管理</div>
              <div class="action-desc">管理所有平台帳號</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card class="action-card" @click="navigateTo('/material-management')">
              <div class="action-icon">
                <el-icon><Upload /></el-icon>
              </div>
              <div class="action-title">素材庫</div>
              <div class="action-desc">上傳與管理影片素材</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card class="action-card" @click="navigateTo('/publish-center')">
              <div class="action-icon">
                <el-icon><Timer /></el-icon>
              </div>
              <div class="action-title">發佈中心</div>
              <div class="action-desc">將內容發佈到各平台</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card class="action-card" @click="navigateTo('/tiktok-review')">
              <div class="action-icon">
                <el-icon><Connection /></el-icon>
              </div>
              <div class="action-title">TikTok callback</div>
              <div class="action-desc">查看 callback / webhook 收件狀態</div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card class="action-card" @click="navigateTo('/about')">
              <div class="action-icon">
                <el-icon><DataAnalysis /></el-icon>
              </div>
              <div class="action-title">關於系統</div>
              <div class="action-desc">查看系統資訊</div>
            </el-card>
          </el-col>
        </el-row>
      </div>

      <!-- 素材列表 -->
      <div class="recent-tasks">
        <div class="section-header">
          <h2>最近上傳素材</h2>
          <el-button text @click="navigateTo('/material-management')">查看全部</el-button>
        </div>

        <el-table :data="recentMaterials" style="width: 100%" v-loading="loading">
          <el-table-column prop="filename" label="檔案名稱" width="300" />
          <el-table-column prop="filesize" label="檔案大小" width="120">
            <template #default="scope">
              {{ scope.row.filesize }} MB
            </template>
          </el-table-column>
          <el-table-column prop="upload_time" label="上傳時間" width="200" />
          <el-table-column label="類型" width="100">
            <template #default="scope">
              <el-tag
                :type="getFileTypeTag(scope.row.filename)"
                effect="plain"
                size="small"
              >
                {{ getFileType(scope.row.filename) }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>

        <el-empty v-if="!loading && recentMaterials.length === 0" description="目前沒有素材資料" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  User, UserFilled, Platform, Document,
  Upload, Timer, DataAnalysis, Connection
} from '@element-plus/icons-vue'
import { accountApi } from '@/api/account'
import { materialApi } from '@/api/material'
import { useAccountStore } from '@/stores/account'
import { useAppStore } from '@/stores/app'
import { ACCOUNT_PLATFORM_OPTIONS, LEGACY_ACCOUNT_PLATFORM_ORDER } from '@/utils/platforms'

const router = useRouter()
const accountStore = useAccountStore()
const appStore = useAppStore()
const loading = ref(false)
const healthSummary = ref({ total: 0, ready: 0, configured: 0, missing: 0, refreshable: 0, checkable: 0, expirySummary: { overdue: 0, expiringWithin24h: 0, expiringWithin7d: 0, reconnectRequired: 0 }, recentEventTotals: { total: 0, ok: 0, error: 0 }, expiringAccounts: [], recentEvents: [] })
const dashboardPlatforms = LEGACY_ACCOUNT_PLATFORM_ORDER
  .map((publishSlug) =>
    ACCOUNT_PLATFORM_OPTIONS.find((platform) => platform.publishSlug === publishSlug)
  )
  .filter(Boolean)
  .map(({ publishSlug, label, tagType }) => ({
    key: publishSlug,
    label,
    tagType
  }))

// 账号统计数据 - 从真实数据计算
const accountStats = computed(() => {
  const accounts = accountStore.accounts
  const normal = accounts.filter(a => a.status === '正常').length
  const abnormal = accounts.filter(a => a.status !== '正常' && a.status !== '驗證中').length
  return {
    total: accounts.length,
    normal,
    abnormal
  }
})

// 平台统计数据 - 从真实数据计算
const platformStats = computed(() => {
  const accounts = accountStore.accounts
  const counts = dashboardPlatforms.reduce((result, platform) => {
    result[platform.key] = accounts.filter(a => a.platform === platform.label).length
    return result
  }, {})

  return {
    total: dashboardPlatforms.filter(platform => counts[platform.key] > 0).length,
    counts
  }
})

// 素材统计数据 - 从真实数据计算
const videoExtensions = ['.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv']
const imageExtensions = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']

const contentStats = computed(() => {
  const materials = appStore.materials
  const videos = materials.filter(m => videoExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  const images = materials.filter(m => imageExtensions.some(ext => m.filename.toLowerCase().endsWith(ext))).length
  return {
    total: materials.length,
    videos,
    images,
    others: materials.length - videos - images
  }
})

// 最近上传的素材（最多显示5条）
const recentMaterials = computed(() => {
  return [...appStore.materials]
    .sort((a, b) => new Date(b.upload_time) - new Date(a.upload_time))
    .slice(0, 5)
})

// 获取文件類型
const getFileType = (filename) => {
  if (videoExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return '影片'
  if (imageExtensions.some(ext => filename.toLowerCase().endsWith(ext))) return '圖片'
  return '其他'
}

// 获取文件類型标签颜色
const getFileTypeTag = (filename) => {
  const type = getFileType(filename)
  return { '影片': 'success', '圖片': 'warning', '其他': 'info' }[type] || 'info'
}

const formatRemaining = (seconds) => {
  if (seconds == null) return '—'
  if (seconds <= 0) return 'expired'
  const hours = Math.floor(seconds / 3600)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

// 导航到指定路由
const navigateTo = (path) => {
  router.push(path)
}

const platformValueByLabel = computed(() => Object.fromEntries(dashboardPlatforms.map((platform) => [platform.label, platform.key])))

const goToAccountQueue = ({ risk = 'all', platform = 'all', profile = 'all', sort = 'urgency' } = {}) => {
  const query = {}
  if (risk && risk !== 'all') query.risk = risk
  const normalizedPlatform = platform && platform !== 'all' ? (platformValueByLabel.value[platform] || platform) : 'all'
  if (normalizedPlatform !== 'all') query.platform = normalizedPlatform
  if (profile && profile !== 'all') query.profile = profile
  if (sort && sort !== 'urgency') query.sort = sort
  router.push({ path: '/account-management', query })
}

// 加载数据
const fetchDashboardData = async () => {
  loading.value = true
  try {
    // 并行获取账号和素材数据
    const [accountRes, materialRes, healthRes] = await Promise.allSettled([
      accountApi.getAccounts(),
      materialApi.getAllMaterials(),
      accountApi.getHealthSummary()
    ])

    if (accountRes.status === 'fulfilled' && accountRes.value.code === 200) {
      accountStore.setAccounts(accountRes.value.data)
    }
    if (materialRes.status === 'fulfilled' && materialRes.value.code === 200) {
      appStore.setMaterials(materialRes.value.data)
    }
    if (healthRes.status === 'fulfilled' && healthRes.value.code === 200) {
      healthSummary.value = healthRes.value.data
    }
  } catch (error) {
    console.error('取得儀表板資料失敗:', error)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchDashboardData()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.dashboard {
  .page-header {
    margin-bottom: 20px;

    h1 {
      font-size: 24px;
      color: $text-primary;
      margin: 0;
    }
  }

  .dashboard-content {
    .stat-card {
      height: 140px;
      margin-bottom: 20px;

      .stat-card-content {
        display: flex;
        align-items: center;
        margin-bottom: 15px;

        .stat-icon {
          width: 60px;
          height: 60px;
          border-radius: 50%;
          background-color: rgba($primary-color, 0.1);
          display: flex;
          justify-content: center;
          align-items: center;
          margin-right: 15px;

          .el-icon {
            font-size: 30px;
            color: $primary-color;
          }

          &.platform-icon {
            background-color: rgba($success-color, 0.1);

            .el-icon {
              color: $success-color;
            }
          }

          &.content-icon {
            background-color: rgba($info-color, 0.1);

            .el-icon {
              color: $info-color;
            }
          }
        }

        .stat-info {
          .stat-value {
            font-size: 24px;
            font-weight: bold;
            color: $text-primary;
            line-height: 1.2;
          }

          .stat-label {
            font-size: 14px;
            color: $text-secondary;
          }
        }
      }

      .stat-footer {
        border-top: 1px solid $border-lighter;
        padding-top: 10px;

        .stat-detail {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          color: $text-secondary;
          font-size: 13px;

          .el-tag {
            margin-right: 5px;
          }
        }
      }
    }

    .quick-actions {
      margin: 20px 0 30px;

      h2 {
        font-size: 18px;
        margin-bottom: 15px;
        color: $text-primary;
      }

      .action-card {
        height: 160px;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: all 0.3s;

        &:hover {
          transform: translateY(-5px);
          box-shadow: 0 10px 20px rgba(0, 0, 0, 0.1);
        }

        .action-icon {
          width: 50px;
          height: 50px;
          border-radius: 50%;
          background-color: rgba($primary-color, 0.1);
          display: flex;
          justify-content: center;
          align-items: center;
          margin-bottom: 15px;

          .el-icon {
            font-size: 24px;
            color: $primary-color;
          }
        }

        .action-title {
          font-size: 16px;
          font-weight: bold;
          color: $text-primary;
          margin-bottom: 5px;
        }

        .action-desc {
          font-size: 13px;
          color: $text-secondary;
          text-align: center;
        }
      }
    }

    .recent-tasks {
      margin-top: 30px;

      .expiry-summary {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 12px;
      }

      .expiry-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 12px;
      }

      .section-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 15px;

        h2 {
          font-size: 18px;
          color: $text-primary;
          margin: 0;
        }
      }
    }
  }
}
</style>
