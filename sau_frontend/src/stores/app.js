import { defineStore } from 'pinia'
import { ref, watch } from 'vue'

const STORAGE_KEY = 'sau-theme'

function loadTheme() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return {
      theme: saved.theme || 'dark',
      accent: saved.accent || 'lime',
      density: saved.density || 'comfortable',
    }
  } catch { return { theme: 'dark', accent: 'lime', density: 'comfortable' } }
}

export const useAppStore = defineStore('app', () => {
  // ---- Theme / appearance ----
  const saved = loadTheme()
  const theme = ref(saved.theme)
  const accent = ref(saved.accent)
  const density = ref(saved.density)

  function setTheme(v) { theme.value = v }
  function setAccent(v) { accent.value = v }
  function setDensity(v) { density.value = v }

  // Persist + apply to <html>
  watch([theme, accent, density], ([t, a, d]) => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ theme: t, accent: a, density: d }))
    const el = document.documentElement
    el.setAttribute('data-theme', t)
    el.setAttribute('data-accent', a)
    el.setAttribute('data-density', d)
  }, { immediate: true })

  // ---- Existing state ----
  const isFirstTimeAccountManagement = ref(true)
  const isFirstTimeMaterialManagement = ref(true)
  const isAccountRefreshing = ref(false)

  // 素材列表数据
  const materials = ref([])
  
  // 设置账号管理页面已访问
  const setAccountManagementVisited = () => {
    isFirstTimeAccountManagement.value = false
  }
  
  // 设置素材管理页面已访问
  const setMaterialManagementVisited = () => {
    isFirstTimeMaterialManagement.value = false
  }
  
  // 重置所有访问状态（用于重新登录或刷新应用时）
  const resetVisitStatus = () => {
    isFirstTimeAccountManagement.value = true
    isFirstTimeMaterialManagement.value = true
  }

  // 更新素材列表
  const setMaterials = (materialList) => {
    materials.value = materialList
  }

  // 添加新素材
  const addMaterial = (material) => {
    materials.value.push(material)
  }

  // 删除素材
  const removeMaterial = (materialId) => {
    const index = materials.value.findIndex(m => m.id === materialId)
    if (index > -1) {
      materials.value.splice(index, 1)
    }
  }

  // 批量删除素材
  const removeMaterials = (ids) => {
    const idSet = new Set(ids)
    materials.value = materials.value.filter(m => !idSet.has(m.id))
  }
  
  // 设置账号管理页面刷新状态
  const setAccountRefreshing = (status) => {
    isAccountRefreshing.value = status
  }

  return {
    // Theme
    theme, accent, density,
    setTheme, setAccent, setDensity,
    // Existing
    isFirstTimeAccountManagement,
    isFirstTimeMaterialManagement,
    isAccountRefreshing,
    materials,
    setAccountManagementVisited,
    setMaterialManagementVisited,
    resetVisitStatus,
    setMaterials,
    addMaterial,
    removeMaterial,
    removeMaterials,
    setAccountRefreshing
  }
})