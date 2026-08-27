<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api'
import { useDashboardStore } from '../stores/dashboard'

const route = useRoute()
const activeMenu = computed(() => route.path)
const pageTitle = computed(() => String(route.meta.title ?? ''))
const store = useDashboardStore()

function onAutoChange() {
  store.setAutoRefresh(store.autoRefresh)
}

// 读取后端配置：自动刷新总开关（未启用时隐藏开关、不启动轮询）
onMounted(async () => {
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
  store.stopClock()
})
</script>

<template>
  <el-container class="layout">
    <el-aside width="212px" class="aside">
      <div class="logo">
        <svg viewBox="0 0 32 32" class="logo-svg" aria-hidden="true">
          <rect width="32" height="32" rx="6" fill="#2f6fed" />
          <path d="M7 25V15h4v10H7zm7 0V8h4v17h-4zm7 0v-7h4v7h-4z" fill="#fff" />
        </svg>
        <span class="logo-text">txxy 数据展示</span>
      </div>
      <el-menu :default-active="activeMenu" router class="menu">
        <el-menu-item index="/">
          <el-icon><Odometer /></el-icon>
          <span>数据总览</span>
        </el-menu-item>
        <el-menu-item index="/posts">
          <el-icon><Document /></el-icon>
          <span>帖子浏览</span>
        </el-menu-item>
        <el-menu-item index="/runs">
          <el-icon><Clock /></el-icon>
          <span>运行记录</span>
        </el-menu-item>
        <el-menu-item index="/resources">
          <el-icon><FolderOpened /></el-icon>
          <span>资源管理</span>
        </el-menu-item>
        <el-menu-item index="/downloads">
          <el-icon><Download /></el-icon>
          <span>下载中心</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container class="body">
      <el-header class="header" height="56px">
        <span class="page-title">{{ pageTitle }}</span>
        <div class="header-right">
          <span class="updated-info">
            <el-icon class="text-muted"><Timer /></el-icon>
            <span class="text-muted">{{ store.nowText }}</span>
          </span>
          <div v-if="store.enableAutoRefresh" class="auto-refresh-card">
            <span class="text-muted">自动刷新</span>
            <el-switch v-model="store.autoRefresh" size="small" @change="onAutoChange" />
          </div>
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
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  border-bottom: 1px solid var(--app-border);
}

.logo-svg {
  width: 28px;
  height: 28px;
  flex: none;
}

.logo-text {
  font-size: 15px;
  font-weight: 600;
  color: #1f2d3d;
  white-space: nowrap;
}

.menu {
  border-right: none;
  padding-top: 8px;
  flex: 1;
  overflow-y: auto;
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
  padding: 0 18px;
  gap: 12px;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
  min-width: 0;
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
}

.main {
  padding: 18px;
  overflow-y: auto;
  overflow-x: hidden; /* 杜绝数据总览等页面渲染时的横向滚动条闪现 */
  min-width: 0;
}
</style>
