import { createRouter, createWebHistory } from 'vue-router'
import { ElMessage } from 'element-plus'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'dashboard',
      component: () => import('../views/DashboardView.vue'),
      meta: { title: '数据总览' },
    },
    {
      path: '/posts',
      name: 'posts',
      component: () => import('../views/PostsView.vue'),
      meta: { title: '帖子浏览' },
    },
    {
      path: '/runs',
      name: 'runs',
      component: () => import('../views/RunsView.vue'),
      meta: { title: '运行记录' },
    },
    {
      path: '/resources',
      name: 'resources',
      component: () => import('../views/ResourcesView.vue'),
      meta: { title: '资源管理' },
    },
    {
      path: '/downloads',
      name: 'downloads',
      component: () => import('../views/DownloadsView.vue'),
      meta: { title: '下载中心' },
    },
    {
      path: '/trash',
      name: 'trash',
      component: () => import('../views/TrashView.vue'),
      meta: { title: '回收管理' },
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { title: '参数设置' },
    },
  ],
})

router.afterEach((to) => {
  document.title = `${String(to.meta.title ?? '')} - txxy 数据展示`
})

/**
 * 判定是否为「路由异步 chunk 加载失败」：
 * 典型场景是前端重新构建（vite build 生成新的 hash 文件名）后浏览器仍停留在旧页面，
 * 此时未访问过的页面其 chunk 已不存在，动态 import 必然失败。
 */
function isChunkLoadError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err)
  return /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(
    msg,
  )
}

// 自动重载只允许触发一次：多个 chunk 同时失败时不必重复提示、也不必重复 reload
let reloading = false

/**
 * 兜底：chunk 加载失败时 vue-router 会 abort 导航且界面零反馈（URL、高亮、内容都不变），
 * 表现为「点了菜单没反应」。此处统一提示并自动重载到最新版本。
 */
function reloadToLatest(): void {
  if (reloading) return
  reloading = true
  ElMessage.warning('页面已更新，正在重新加载…')
  window.setTimeout(() => window.location.reload(), 1000)
}

router.onError((err) => {
  if (isChunkLoadError(err)) reloadToLatest()
})

// Vite 构建产物的预加载（__vitePreload）失败不经过 router，需单独兜底，否则同样静默
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault()
  reloadToLatest()
})

export default router
