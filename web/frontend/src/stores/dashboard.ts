import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { formatFullTime } from '../utils/time'

/**
 * Dashboard 全局共享状态：
 * - Header 右侧展示【更新时间】与【自动刷新】控件
 * - DashboardView 复用同一开关并保持轮询逻辑
 * - 自动刷新由后端 /api/config 下发的 enableAutoRefresh 控制（默认关闭，
 *   未启用时隐藏开关且不启动轮询，避免页面进入 / 停留时被定时刷新拖慢）
 */
export const useDashboardStore = defineStore('dashboard', () => {
  // 自动刷新总开关：由后端 /api/config 下发（默认关闭）
  const enableAutoRefresh = ref(false)
  // 自动刷新开关（默认关闭；配置启用时初始为开启，每 30 秒静默刷新一次，逻辑在 DashboardView 中）
  const autoRefresh = ref(false)
  // 最后更新时间（Dashboard 加载成功后更新）
  const updatedAtRaw = ref<string | null>(null)

  const updatedAtText = computed(() => {
    if (!updatedAtRaw.value) return '更新于 --'
    // 兼容旧 ISO 字符串与新 Unix 秒时间戳两种形态
    return `更新于 ${formatFullTime(updatedAtRaw.value)}`
  })

  function setUpdatedAt(raw: string | null) {
    updatedAtRaw.value = raw
  }

  // Header 右侧实时时钟：每秒刷新，展示当前时间
  const nowText = ref<string>(formatNow())
  let nowTimer: ReturnType<typeof setInterval> | null = null

  function formatNow(): string {
    const d = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    return `${date} ${time}`
  }

  function startClock() {
    if (nowTimer !== null) return
    nowText.value = formatNow()
    nowTimer = setInterval(() => {
      nowText.value = formatNow()
    }, 1000)
  }

  function stopClock() {
    if (nowTimer !== null) {
      clearInterval(nowTimer)
      nowTimer = null
    }
  }

  // Dashboard 页面注册的处理器（挂载时注册、卸载时注销）
  let autoChangeHandler: (() => void) | null = null

  function registerAutoChange(fn: (() => void) | null) {
    autoChangeHandler = fn
  }

  // 开关变化：更新状态并通知 Dashboard 启停轮询
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
    updatedAtText,
    setUpdatedAt,
    nowText,
    startClock,
    stopClock,
    registerAutoChange,
    setAutoRefresh,
    setEnableAutoRefresh,
  }
})
