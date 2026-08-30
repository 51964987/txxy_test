<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, type ComponentPublicInstance } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { useAppStore } from '../stores/app'
import { useDashboardStore } from '../stores/dashboard'
import SideMenu from './SideMenu.vue'

const route = useRoute()
const app = useAppStore()
const store = useDashboardStore()

const pageTitle = computed(() => String(route.meta.title ?? ''))
// 大屏入口仅在数据总览提供：其余页面全屏后分页/弹窗层级复杂，不做
const isDashboard = computed(() => route.path === '/')

// 实时时钟：拆分为日期与时间两段，窄屏隐藏日期段（时钟本身保留）
const clockDate = computed(() => store.nowText.split(' ')[0] ?? '')
const clockTime = computed(() => store.nowText.split(' ')[1] ?? store.nowText)

/** 左侧菜单按钮图标（全局注册名）：移动端为汉堡（开抽屉），桌面为折叠/展开 */
const menuIcon = computed<string>(() => {
  if (app.isMobile) return 'Menu'
  return app.collapsed ? 'Expand' : 'Fold'
})

// 全屏目标：右侧内容区（Header + Main），侧栏不在其中会自动隐藏，
// 而 Header 的时钟与自动刷新开关得以保留
const bodyRef = ref<ComponentPublicInstance | null>(null)
function bodyEl(): HTMLElement | null {
  const el = bodyRef.value?.$el
  return el instanceof HTMLElement ? el : null
}

function onMenuToggle() {
  if (app.isMobile) app.openDrawer()
  else app.toggleCollapsed()
}

function onFullscreenToggle() {
  void app.toggleFullscreen(bodyEl())
}

/** 浏览器侧退出（Esc / F11）后同步按钮状态 */
function onFullscreenChange() {
  app.syncFullscreen()
}

function onAutoChange() {
  store.setAutoRefresh(store.autoRefresh)
}

// 读取后端配置：自动刷新总开关（未启用时隐藏开关、不启动轮询）
onMounted(async () => {
  app.initViewport()
  document.addEventListener('fullscreenchange', onFullscreenChange)
  try {
    const cfg = await api.config()
    store.setEnableAutoRefresh(!!cfg.enable_auto_refresh)
  } catch {
    store.setEnableAutoRefresh(false)
  }
  // 启动 Header 右侧实时时钟
  store.startClock()
})

// 组件卸载时停止时钟，避免定时器泄漏
onUnmounted(() => {
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  app.disposeViewport()
  store.stopClock()
})
</script>

<template>
  <el-container class="layout">
    <!-- 桌面端：可折叠侧栏（212px ↔ 64px） -->
    <el-aside v-if="!app.isMobile" :width="app.collapsed ? '64px' : '212px'" class="aside">
      <SideMenu :collapsed="app.collapsed" />
    </el-aside>

    <!-- 移动端：抽屉菜单，选中后自动关闭 -->
    <el-drawer
      v-model="app.drawerVisible"
      direction="ltr"
      size="212px"
      :with-header="false"
      class="mobile-drawer"
    >
      <SideMenu @navigate="app.closeDrawer()" />
    </el-drawer>

    <!-- 右侧内容区：全屏目标容器，Header（时钟 + 自动刷新）在全屏时保留 -->
    <el-container
      ref="bodyRef"
      class="body"
      :class="{ 'is-fullscreen': app.fullscreen, 'is-pseudo-fullscreen': app.pseudoFullscreen }"
    >
      <el-header class="header" height="56px">
        <div class="header-left">
          <el-button
            class="menu-btn"
            text
            :icon="menuIcon"
            :aria-label="app.isMobile ? '打开菜单' : app.collapsed ? '展开侧栏' : '折叠侧栏'"
            @click="onMenuToggle"
          />
          <span class="page-title">{{ pageTitle }}</span>
        </div>
        <div class="header-right">
          <span class="updated-info">
            <el-icon class="text-muted"><Timer /></el-icon>
            <span class="text-muted clock-date">{{ clockDate }}</span>
            <span class="text-muted">{{ clockTime }}</span>
          </span>
          <div v-if="store.enableAutoRefresh" class="auto-refresh-card">
            <span class="text-muted auto-label">自动刷新</span>
            <el-switch v-model="store.autoRefresh" size="small" @change="onAutoChange" />
          </div>
          <!-- 全屏（真）时挂 body 的弹层不可见：tooltip 改为就地挂载 -->
          <el-tooltip
            v-if="isDashboard"
            :content="app.fullscreen ? '退出大屏（Esc）' : '进入大屏'"
            placement="bottom"
            :teleported="!app.fullscreen"
          >
            <el-button
              class="fs-btn"
              text
              :icon="app.fullscreen ? 'Aim' : 'FullScreen'"
              aria-label="切换大屏"
              @click="onFullscreenToggle"
            />
          </el-tooltip>
        </div>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.aside {
  background: #fff;
  border-right: 1px solid var(--app-border);
  overflow: hidden;
}

.body {
  min-width: 0;
}

.header {
  background: #fff;
  border-bottom: 1px solid var(--app-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px 0 8px;
  gap: 12px;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
  min-width: 0;
  position: relative; /* 全屏时就地挂载的 popper 以本容器为定位基准 */
}

.menu-btn,
.fs-btn {
  font-size: 18px;
  padding: 6px;
  height: auto;
}

.updated-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  white-space: nowrap;
}

/* 自动刷新操作卡片：独立卡片样式，位于 header 右侧 */
.auto-refresh-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #fff;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 5px 12px;
}

.page-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.main {
  padding: 18px;
  overflow-y: auto;
  overflow-x: hidden; /* 杜绝数据总览等页面渲染时的横向滚动条闪现 */
  min-width: 0;
}

/* 大屏全屏：全屏元素需自带背景（否则为纯黑底）；内容区收紧留白 */
.body.is-fullscreen {
  background: var(--app-bg);
}

.body.is-fullscreen .main {
  padding: 12px 16px;
}

/* 降级伪全屏：不支持元素级 Fullscreen API（如 iOS Safari）时以 fixed 覆盖模拟。
   z-index 低于 Element Plus popper（2000+），原挂 body 的弹层仍可正常显示 */
.body.is-pseudo-fullscreen {
  position: fixed;
  inset: 0;
  z-index: 1000;
}

/* 移动端抽屉：去掉默认内边距，由 SideMenu 自身控制 */
.mobile-drawer :deep(.el-drawer__body) {
  padding: 0;
  overflow-x: hidden;
}

@media (max-width: 767px) {
  .header {
    padding: 0 10px;
    gap: 8px;
  }

  .header-right {
    gap: 10px;
  }

  /* 窄屏优先保留控件本身，仅隐藏可省略的文字 */
  .clock-date,
  .auto-label {
    display: none;
  }

  .page-title {
    font-size: 15px;
  }

  .auto-refresh-card {
    padding: 5px 8px;
  }
}
</style>
