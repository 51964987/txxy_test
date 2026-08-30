import { ref } from 'vue'
import { defineStore } from 'pinia'

/** 移动端断点：<768px 视为移动端（侧栏改为抽屉） */
const MOBILE_QUERY = '(max-width: 767px)'
/** 桌面端侧栏折叠态持久化键 */
const COLLAPSE_KEY = 'txxy.aside.collapsed'

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
}

function writeCollapsed(v: boolean): void {
  try {
    localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0')
  } catch {
    // 隐私模式 / 存储被禁用时忽略：折叠态退化为内存态，不影响功能
  }
}

/** 旧版 Safari 仅实现 webkit 前缀的元素级全屏 */
type FullscreenEl = HTMLElement & {
  webkitRequestFullscreen?: () => Promise<void> | void
}

/**
 * 布局级 UI 状态（全局唯一入口，页面不得自建同类状态）：
 * - isMobile：移动端断点判定，驱动「侧栏 / 抽屉」两种形态
 * - collapsed：桌面侧栏折叠态，持久化到 localStorage
 * - drawerVisible：移动端抽屉开合（瞬时态，不持久化）
 * - fullscreen：大屏模式（真全屏与降级伪全屏均为 true）
 * - pseudoFullscreen：降级伪全屏标记（浏览器不支持元素级 Fullscreen API 时）
 */
export const useAppStore = defineStore('app', () => {
  const isMobile = ref(false)
  const collapsed = ref(readCollapsed())
  const drawerVisible = ref(false)
  const fullscreen = ref(false)
  const pseudoFullscreen = ref(false)

  let media: MediaQueryList | null = null

  /** 断点切换：同步移动端标记并复位抽屉，避免两种形态同时生效 */
  function onMediaChange(e: MediaQueryListEvent): void {
    isMobile.value = e.matches
    drawerVisible.value = false
  }

  /** 初始化视口监听（matchMedia，避免与图表的 resize 监听互相干扰） */
  function initViewport(): void {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return
    media = window.matchMedia(MOBILE_QUERY)
    isMobile.value = media.matches
    media.addEventListener('change', onMediaChange)
  }

  function disposeViewport(): void {
    media?.removeEventListener('change', onMediaChange)
    media = null
  }

  function toggleCollapsed(): void {
    collapsed.value = !collapsed.value
    writeCollapsed(collapsed.value)
  }

  function openDrawer(): void {
    drawerVisible.value = true
  }

  function closeDrawer(): void {
    drawerVisible.value = false
  }

  /** 进入大屏：优先元素级真全屏；不支持或失败时降级为 CSS 伪全屏 */
  async function enterFullscreen(target: HTMLElement | null): Promise<void> {
    if (!target) return
    const el = target as FullscreenEl
    const request =
      typeof el.requestFullscreen === 'function'
        ? el.requestFullscreen.bind(el)
        : el.webkitRequestFullscreen?.bind(el)
    if (!request) {
      pseudoFullscreen.value = true
      fullscreen.value = true
      return
    }
    try {
      await request()
    } catch {
      // 用户拒绝 / 浏览器策略限制：降级为伪全屏，保证大屏入口始终可用
      pseudoFullscreen.value = true
      fullscreen.value = true
    }
  }

  /** 退出大屏：伪全屏直接复位；真全屏调用 exitFullscreen，实际状态由 fullscreenchange 同步 */
  async function exitFullscreen(): Promise<void> {
    if (pseudoFullscreen.value) {
      pseudoFullscreen.value = false
      fullscreen.value = false
      return
    }
    if (!document.fullscreenElement) {
      fullscreen.value = false
      return
    }
    try {
      await document.exitFullscreen()
    } catch {
      fullscreen.value = false
    }
  }

  async function toggleFullscreen(target: HTMLElement | null): Promise<void> {
    if (fullscreen.value) await exitFullscreen()
    else await enterFullscreen(target)
  }

  /** 浏览器侧退出（Esc / F11）后同步状态，避免按钮与实际状态不一致 */
  function syncFullscreen(): void {
    fullscreen.value = !!document.fullscreenElement || pseudoFullscreen.value
  }

  return {
    isMobile,
    collapsed,
    drawerVisible,
    fullscreen,
    pseudoFullscreen,
    initViewport,
    disposeViewport,
    toggleCollapsed,
    openDrawer,
    closeDrawer,
    enterFullscreen,
    exitFullscreen,
    toggleFullscreen,
    syncFullscreen,
  }
})
