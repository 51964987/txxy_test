<script setup lang="ts">
import { computed, type Component } from 'vue'
import { useRoute } from 'vue-router'
import {
  Clock,
  Delete,
  Document,
  Download,
  FolderOpened,
  Odometer,
} from '@element-plus/icons-vue'

/**
 * 侧栏内容（Logo + 菜单）：桌面 el-aside 与移动端 el-drawer 共用同一份，
 * 避免两处各写一套菜单导致后续改动不同步。
 * 菜单项与 router 中的 5 条路由一一对应，不新增/不改动路由。
 */
defineProps<{ collapsed?: boolean }>()
const emit = defineEmits<{ navigate: [] }>()

interface MenuItem {
  path: string
  title: string
  icon: Component
}

const MENU_ITEMS: MenuItem[] = [
  { path: '/', title: '数据总览', icon: Odometer },
  { path: '/posts', title: '帖子浏览', icon: Document },
  { path: '/runs', title: '运行记录', icon: Clock },
  { path: '/resources', title: '资源管理', icon: FolderOpened },
  { path: '/downloads', title: '下载中心', icon: Download },
  { path: '/trash', title: '回收管理', icon: Delete },
]

const route = useRoute()
const activeMenu = computed(() => route.path)

/** 选中菜单项：移动端需通知外层关闭抽屉 */
function onSelect() {
  emit('navigate')
}
</script>

<template>
  <div class="side-panel">
    <div class="logo" :class="{ 'is-collapsed': collapsed }">
      <svg viewBox="0 0 32 32" class="logo-svg" aria-hidden="true">
        <rect width="32" height="32" rx="6" fill="#2f6fed" />
        <path d="M7 25V15h4v10H7zm7 0V8h4v17h-4zm7 0v-7h4v7h-4z" fill="#fff" />
      </svg>
      <span v-show="!collapsed" class="logo-text">txxy 数据展示</span>
    </div>
    <el-menu
      :default-active="activeMenu"
      :collapse="collapsed"
      :collapse-transition="false"
      router
      class="side-menu"
      @select="onSelect"
    >
      <el-menu-item v-for="item in MENU_ITEMS" :key="item.path" :index="item.path">
        <el-icon><component :is="item.icon" /></el-icon>
        <template #title>{{ item.title }}</template>
      </el-menu-item>
    </el-menu>
  </div>
</template>

<style scoped>
.side-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: #fff;
}

.logo {
  height: 56px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 18px;
  border-bottom: 1px solid var(--app-border);
}

/* 折叠态：图标居中，与 el-menu 折叠后的 64px 宽度对齐 */
.logo.is-collapsed {
  padding: 0;
  justify-content: center;
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

.side-menu {
  border-right: none;
  padding-top: 8px;
  flex: 1;
  overflow-y: auto;
}
</style>
