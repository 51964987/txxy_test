<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElButton, ElMessage, ElResult, ElTag } from 'element-plus'
import type { Columns } from 'element-plus'
import { api, formatSize, isAborted, type ResourceFile, type Resources } from '../api'

const data = ref<Resources | null>(null)
const loading = ref(false)
const loadError = ref('') // 加载失败信息（非空时展示重试界面）
const active = ref('') // 当前展开的文件夹

const totalSizeText = computed(() => formatSize(data.value?.total_size ?? 0))

const categoryMeta: Record<string, { label: string; type: string }> = {
  image: { label: '图片', type: 'primary' },
  video: { label: '视频', type: 'success' },
  torrent: { label: '种子', type: 'warning' },
  text: { label: '文本', type: 'info' },
  other: { label: '其他', type: 'info' },
}

// 类型筛选选项（P0-2）
const categoryOptions = [
  { label: '全部', value: 'all' },
  { label: '图片', value: 'image' },
  { label: '视频', value: 'video' },
  { label: '种子', value: 'torrent' },
  { label: '文本', value: 'text' },
  { label: '其他', value: 'other' },
]
const typeFilter = ref<'all' | 'image' | 'video' | 'torrent' | 'text' | 'other'>('all') // P0-2 类型筛选

// P0-3 排序：el-table-v2 原生列排序状态
const sortState = ref<{ key: string; order: 'asc' | 'desc' | null }>({ key: '', order: null })

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await api.resources()
  } catch (e) {
    if (isAborted(e)) return
    loadError.value = (e as Error).message
    ElMessage.error(`加载资源失败: ${loadError.value}`)
  } finally {
    loading.value = false
  }
}

function toggle(name: string) {
  active.value = active.value === name ? '' : name
}

function copyPath(p: string) {
  // 兼容非 HTTPS / 非 localhost 下 clipboard API 不可用，降级用 execCommand
  const fallback = () => {
    try {
      const ta = document.createElement('textarea')
      ta.value = p
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      ElMessage.success('路径已复制')
    } catch {
      ElMessage.error('复制失败，请手动复制')
    }
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard
      .writeText(p)
      .then(() => ElMessage.success('路径已复制'))
      .catch(fallback)
  } else {
    fallback()
  }
}

function fmtTime(ts: number): string {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

// ---- P2-14 大列表虚拟滚动：展开文件夹的文件列表改用 el-table-v2 ----
// 当前展开文件夹及其文件列表
const activeFolder = computed(() => data.value?.items.find((i) => i.name === active.value) ?? null)
const activeFiles = computed<ResourceFile[]>(() => activeFolder.value?.files ?? [])

// P0-1 搜索词：匹配文件名 / 相对路径（大小写不敏感）
const keyword = ref('')

// P0-1/2/3 过滤 + 排序后的文件列表（虚拟滚动数据源）
const filteredFiles = computed<ResourceFile[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = activeFiles.value
  if (typeFilter.value !== 'all') list = list.filter((f) => f.category === typeFilter.value)
  if (kw) list = list.filter((f) => f.name.toLowerCase().includes(kw) || f.rel_path.toLowerCase().includes(kw))

  const { key, order } = sortState.value
  if (key && order) {
    const factor = order === 'asc' ? 1 : -1
    const sorted = [...list].sort((a, b) => {
      if (key === 'size') return (Number(a.size) - Number(b.size)) * factor
      const cmp = String(a[key as keyof ResourceFile]).localeCompare(String(b[key as keyof ResourceFile]))
      return cmp * factor
    })
    return sorted
  }
  return list
})

// 命中高亮：先把文本做 HTML 转义，再把关键词匹配片段包成 <mark>（避免特殊字符破坏 innerHTML）
function highlight(text: string): string {
  const kw = keyword.value.trim()
  const escapedText = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  if (!kw) return escapedText
  const escapedKw = kw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  try {
    return escapedText.replace(new RegExp(`(${escapedKw})`, 'ig'), '<mark class="kw">$1</mark>')
  } catch {
    return escapedText
  }
}

// P0-3 排序变化事件（el-table-v2 原生）
function onColumnSort(params: { key: string; order: 'asc' | 'desc' | null }) {
  sortState.value = params
}

// 表格高度按行数自适应，上限 420px（含表头），文件少时贴合内容、多时固定高度滚动
const tableHeight = computed(() => {
  const rows = filteredFiles.value.length
  if (rows === 0) return 0
  return Math.min(rows * 36 + 40, 420)
})
// 表格宽度跟随容器（仅展开的文件夹绑定，用 :ref 函数避免 v-for 重复 ref）
const tableWidth = ref(900)
let tableWrapRef: HTMLDivElement | null = null
let resizeObserver: ResizeObserver | null = null

function setTableWrapRef(el: unknown, name: string) {
  if (name === active.value) tableWrapRef = (el as HTMLDivElement) ?? null
}

function measureWidth() {
  if (tableWrapRef) tableWidth.value = Math.floor(tableWrapRef.getBoundingClientRect().width)
}

const columns: Columns<ResourceFile> = [
  {
    key: 'name',
    dataKey: 'name',
    title: '文件名',
    width: 340,
    ellipsis: true,
    sortable: true,
    cellRenderer: ({ rowData }) =>
      h('span', { title: rowData.name, innerHTML: highlight(rowData.name) }),
  },
  {
    key: 'category',
    dataKey: 'category',
    title: '类型',
    width: 90,
    sortable: true,
    cellRenderer: ({ cellData }) => {
      const meta = categoryMeta[String(cellData)]
      return h(ElTag, { size: 'small', type: (meta?.type as any) ?? 'info' }, () => meta?.label ?? '其他')
    },
  },
  {
    key: 'size',
    dataKey: 'size',
    title: '大小',
    width: 110,
    sortable: true,
    cellRenderer: ({ cellData }) => h('span', formatSize(Number(cellData))),
  },
  {
    key: 'rel_path',
    dataKey: 'rel_path',
    title: '相对路径',
    width: 280,
    ellipsis: true,
    cellRenderer: ({ rowData }) => h('span', { class: 'text-muted' }, `downloads/${rowData.rel_path}`),
  },
  {
    key: 'actions',
    title: '操作',
    width: 120,
    cellRenderer: ({ rowData }) =>
      h(ElButton, { link: true, onClick: () => copyPath(rowData.rel_path) }, () => '复制路径'),
  },
]

// 展开文件夹后测量容器宽度，并监听窗口尺寸变化
watch(active, async () => {
  await nextTick()
  measureWidth()
  if (typeof ResizeObserver !== 'undefined') {
    if (!resizeObserver) resizeObserver = new ResizeObserver(measureWidth)
    resizeObserver.disconnect()
    if (tableWrapRef) resizeObserver.observe(tableWrapRef)
  }
})

onMounted(load)

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  resizeObserver = null
})
</script>

<template>
  <div v-loading="loading">
    <!-- 统计卡片 -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background: #10b981">
          <el-icon><FolderOpened /></el-icon>
        </div>
        <div>
          <div class="stat-label">资源目录</div>
          <div class="stat-value">{{ data?.count ?? 0 }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #2f6fed">
          <el-icon><Files /></el-icon>
        </div>
        <div>
          <div class="stat-label">文件总数</div>
          <div class="stat-value">{{ data?.total_files ?? 0 }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #f59e0b">
          <el-icon><Coin /></el-icon>
        </div>
        <div>
          <div class="stat-label">总大小</div>
          <div class="stat-value">{{ totalSizeText }}</div>
        </div>
      </div>
      <div class="stat-card stat-card-toolbar">
        <el-button
          :icon="'Refresh'"
          :loading="loading"
          type="primary"
          plain
          @click="load"
        >
          刷新
        </el-button>
      </div>
    </div>

    <!-- 资源列表 -->
    <div class="page-card">
      <!-- P0-4 加载失败重试 -->
      <el-result
        v-if="loadError"
        icon="error"
        title="资源加载失败"
        :sub-title="loadError"
      >
        <template #extra>
          <el-button type="primary" :loading="loading" @click="load">重试</el-button>
        </template>
      </el-result>

      <el-empty v-else-if="data && data.count === 0" description="downloads/ 下暂无下载资源" />

      <template v-else-if="data">
        <!-- P0-1/P0-2 工具栏：搜索 + 类型筛选（作用于当前展开文件夹） -->
        <div class="toolbar">
          <el-input
            v-model="keyword"
            class="toolbar-search"
            placeholder="搜索当前文件夹内的文件名 / 路径"
            clearable
            :prefix-icon="'Search'"
          />
          <el-segmented v-model="typeFilter" :options="categoryOptions" />
        </div>

        <div
          v-for="item in data.items"
          :key="item.name"
          class="folder"
        >
          <div class="folder-head" @click="toggle(item.name)">
            <el-icon class="folder-arrow" :class="{ open: active === item.name }">
              <ArrowRight />
            </el-icon>
            <el-icon class="folder-icon"><Folder /></el-icon>
            <span class="folder-name">{{ item.name }}</span>
            <span class="text-muted folder-meta">
              {{ item.file_count }} 个文件 · {{ formatSize(item.total_size) }} · {{ fmtTime(item.mtime) }}
            </span>
            <el-button link type="primary" class="copy-btn" @click.stop="copyPath(item.name)">
              复制目录名
            </el-button>
          </div>

          <el-collapse-transition>
            <div v-show="active === item.name" class="folder-body">
              <div :ref="(el) => setTableWrapRef(el, item.name)">
                <el-table-v2
                  v-if="active === item.name && filteredFiles.length > 0"
                  :columns="columns"
                  :data="filteredFiles"
                  :width="tableWidth"
                  :height="tableHeight"
                  :row-height="36"
                  :header-height="40"
                  :sort-state="sortState"
                  @column-sort="onColumnSort"
                />
                <el-empty
                  v-else-if="active === item.name && activeFiles.length > 0"
                  :image-size="48"
                  description="无匹配文件"
                />
              </div>
            </div>
          </el-collapse-transition>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* P0-4 刷新按钮所在卡片：作为第 4 格，内容居中放置按钮 */
.stat-card-toolbar {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 72px;
}

/* P0-1/P0-2 工具栏 */
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.toolbar-search {
  width: 320px;
  max-width: 100%;
}

/* P0-1 搜索命中高亮 */
:deep(mark.kw) {
  background: #ffe58f;
  color: #b45309;
  border-radius: 2px;
  padding: 0 1px;
}

.folder {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}

.folder-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
}

.folder-head:hover {
  background: #f6f8fc;
}

.folder-arrow {
  transition: transform 0.2s ease;
  color: #909399;
}

.folder-arrow.open {
  transform: rotate(90deg);
}

.folder-icon {
  color: #2f6fed;
}

.folder-name {
  font-weight: 600;
  color: #1f2d3d;
}

.folder-meta {
  font-size: 12px;
}

.copy-btn {
  margin-left: auto;
}

.folder-body {
  padding: 0 14px 12px;
  border-top: 1px dashed var(--app-border);
  background: #fbfcfe;
}
</style>
