import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Dashboard 全局共享状态：
 * - Header 右侧展示【更新时间】与【自动刷新】控件
 * - DashboardView 复用同一开关并保持原有轮询 / 刷新逻辑
 */
export const useDashboardStore = defineStore('dashboard', () => {
  // 自动刷新总开关：由后端 /api/config 下发（默认关闭）
  const enableAutoRefresh = ref(false)
  // 自动刷新开关（默认关闭；配置启用时初始为开启，每 30 秒静默刷新一次，逻辑在 DashboardView 中）
  const autoRefresh = ref(false)
  // 手动刷新中状态（Header 刷新按钮 loading）
  const refreshing = ref(false)
  // 最后更新时间（Dashboard 加载成功后更新）
  const updatedAtRaw = ref<string | null>(null)

  const updatedAtText = computed(() => {
    if (!updatedAtRaw.value) return '更新于 --'
    const s = String(updatedAtRaw.value).replace('T', ' ')
    return `更新于 ${s.slice(0, 19)}`
  })

  function setUpdatedAt(raw: string | null) {
    updatedAtRaw.value = raw
  }

  // Dashboard 页面注册的处理器（挂载时注册、卸载时注销）
  let refreshHandler: (() => void) | null = null
  let autoChangeHandler: (() => void) | null = null

  function registerRefresh(fn: (() => void) | null) {
    refreshHandler = fn
  }

  function registerAutoChange(fn: (() => void) | null) {
    autoChangeHandler = fn
  }

  // Header 刷新按钮：触发 Dashboard 重新加载数据
  function triggerRefresh() {
    refreshHandler?.()
  }

  // Header 开关变化：更新状态并通知 Dashboard 启停轮询
  function setAutoRefresh(v: boolean) {
    autoRefresh.value = v
    autoChangeHandler?.()
  }

  // 后端配置下发：enable=false 时强制关闭自动刷新；enable=true 时开启
  function setEnableAutoRefresh(v: boolean) {
    enableAutoRefresh.value = v
    if (!v) {
      autoRefresh.value = false
    } else {
      autoRefresh.value = true
    }
    autoChangeHandler?.()
  }

  return {
    enableAutoRefresh,
    autoRefresh,
    refreshing,
    updatedAtText,
    setUpdatedAt,
    registerRefresh,
    registerAutoChange,
    triggerRefresh,
    setAutoRefresh,
    setEnableAutoRefresh,
  }
})
