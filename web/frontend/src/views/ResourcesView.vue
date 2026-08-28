<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElMessage, ElResult, ElTag } from 'element-plus'
import type { Columns } from 'element-plus'
import {
  api,
  formatSize,
  isAborted,
  resourceFileUrl,
  type ResourceFile,
  type ResourceItem,
  type ResourceSource,
  type Resources,
} from '../api'

const router = useRouter()

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

// 类型分布色板（B6 容量洞察用，与统计 Tag 语义对应）
const categoryColors: Record<string, string> = {
  image: '#2f6fed',
  video: '#10b981',
  torrent: '#f59e0b',
  text: '#909399',
  other: '#c0c4cc',
}

// 类型筛选选项（P0-2；B2 起作用域升级为全部目录）
const categoryOptions = [
  { label: '全部', value: 'all' },
  { label: '图片', value: 'image' },
  { label: '视频', value: 'video' },
  { label: '种子', value: 'torrent' },
  { label: '文本', value: 'text' },
  { label: '其他', value: 'other' },
]
const typeFilter = ref<'all' | 'image' | 'video' | 'torrent' | 'text' | 'other'>('all') // P0-2 类型筛选

// P0-3 排序：el-table-v2 原生列排序状态（目录模式与全局结果模式共用）
const sortState = ref<{ key: string; order: 'asc' | 'desc' | null }>({ key: '', order: null })

// B3 目录排序：按下载时间（目录 mtime）最新优先 / 按名称
const folderSort = ref<'time' | 'name'>('time')

// B1 来源回溯缓存：目录名 -> 来源帖信息（会话内不重复请求）
const sourceMap = ref<Record<string, ResourceSource>>({})

// B7 下载任务关联缓存：目录名 -> 任务摘要（saved_dir 回填后按目录聚合）
const taskMap = ref<Record<string, { id: string; status: string }>>({})

// B5 图片预览查看器状态
const viewerVisible = ref(false)
const viewerUrls = ref<string[]>([])
const viewerIndex = ref(0)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await api.resources()
    // 加载完成后并行补齐目录级信息：来源帖（B1）与下载任务关联（B7）
    void loadSources((data.value?.items ?? []).map((i) => i.name))
    void loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    loadError.value = (e as Error).message
    ElMessage.error(`加载资源失败: ${loadError.value}`)
  } finally {
    loading.value = false
  }
}

async function loadSources(names: string[]) {
  const pending = names.filter((n) => !(n in sourceMap.value))
  if (!pending.length) return
  // 本地只读接口，目录数量级为几十，直接并发查询；单条失败静默置为未命中
  await Promise.all(
    pending.map(async (n) => {
      try {
        const r = await api.resourceSource(n)
        sourceMap.value = { ...sourceMap.value, [n]: r }
      } catch {
        sourceMap.value = { ...sourceMap.value, [n]: { matched: false } }
      }
    }),
  )
}

async function loadTasks() {
  try {
    const r = await api.downloadTasks()
    const map: Record<string, { id: string; status: string }> = {}
    for (const t of r.tasks) {
      // R1 起任务列表为概要视图，saved_dirs 为该任务已保存目录的去重集合
      for (const dir0 of t.saved_dirs ?? []) {
        // saved_dir 统一取顶层目录名（与资源目录 name 同口径）
        const dir = dir0.replace(/\\/g, '/').replace(/\/+$/, '').split('/')[0]
        if (dir && !map[dir]) map[dir] = { id: t.id, status: t.status }
      }
    }
    taskMap.value = map
  } catch {
    /* 下载中心不可用时不影响资源页主流程 */
  }
}

function toggle(name: string) {
  active.value = active.value === name ? '' : name
}

// B1 来源帖相关操作
function sourceOf(name: string): ResourceSource | undefined {
  return sourceMap.value[name]
}

function openSourceUrl(name: string) {
  const url = sourceOf(name)?.url
  if (url) window.open(url)
}

function goSourcePosts(name: string) {
  const title = sourceOf(name)?.title ?? name
  void router.push({ path: '/posts', query: { q: title } })
}

// B7 跳转下载中心查看产生该目录的任务
function goDownloads() {
  void router.push('/downloads')
}

function taskOf(name: string) {
  return taskMap.value[name]
}

// B8 打开所在目录：调起系统文件管理器（仅本机 Windows 生效）
async function openFolder(item: ResourceItem) {
  try {
    await api.openResourceFolder(item.name)
    ElMessage.success('已在资源管理器中打开')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`打开目录失败: ${(e as Error).message}`)
  }
}

function copyText(text: string, successMsg: string) {
  // 兼容非 HTTPS / 非 localhost 下 clipboard API 不可用，降级用 execCommand
  const fallback = () => {
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
      ElMessage.success(successMsg)
    } catch {
      ElMessage.error('复制失败，请手动复制')
    }
  }
  if (navigator.clipboard?.writeText) {
    navigator.clipboard
      .writeText(text)
      .then(() => ElMessage.success(successMsg))
      .catch(fallback)
  } else {
    fallback()
  }
}

function copyPath(p: string) {
  copyText(p, '路径已复制')
}

// B11 批量复制路径：全局结果模式复制全部命中文件，目录模式复制当前展开文件夹的筛选结果
function copyAllPaths() {
  const list = globalMode.value ? globalFiles.value : filteredFiles.value
  if (!list.length) {
    ElMessage.info(globalMode.value ? '当前筛选范围内没有文件' : '请先展开文件夹（或使用全局搜索）')
    return
  }
  copyText(list.map((f) => f.rel_path).join('\n'), `已复制 ${list.length} 条路径`)
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

// P0-1 搜索词：B2 起作用域升级为全部目录（文件名 / 相对路径 / 目录名）
const keyword = ref('')

// ---- B2 全局搜索 / 筛选模式 ----
// 有搜索词或选择了类型时进入全局结果模式：跨全部目录列出命中文件 + 命中目录
const globalMode = computed(() => keyword.value.trim() !== '' || typeFilter.value !== 'all')

// 全部文件平铺（B2 全局搜索 / B6 容量洞察共用）
const allFiles = computed<ResourceFile[]>(() => (data.value?.items ?? []).flatMap((i) => i.files))

// 排序复用：按当前列排序状态排序列表（目录模式与全局模式共用）
function sortFiles(list: ResourceFile[]): ResourceFile[] {
  const { key, order } = sortState.value
  if (!key || !order) return list
  const factor = order === 'asc' ? 1 : -1
  return [...list].sort((a, b) => {
    if (key === 'size') return (Number(a.size) - Number(b.size)) * factor
    const cmp = String(a[key as keyof ResourceFile]).localeCompare(String(b[key as keyof ResourceFile]))
    return cmp * factor
  })
}

// 全局命中文件（过滤 + 排序，虚拟滚动数据源）
const globalFiles = computed<ResourceFile[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = allFiles.value
  if (typeFilter.value !== 'all') list = list.filter((f) => f.category === typeFilter.value)
  if (kw) list = list.filter((f) => f.name.toLowerCase().includes(kw) || f.rel_path.toLowerCase().includes(kw))
  return sortFiles(list)
})

// 目录名命中搜索词的目录（点击可回到目录模式并展开）
const matchedFolders = computed<ResourceItem[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return []
  return sortedFolders.value.filter((i) => i.name.toLowerCase().includes(kw))
})

// B3 目录排序：时间最新优先（mtime 倒序）/ 名称
const sortedFolders = computed<ResourceItem[]>(() => {
  const list = [...(data.value?.items ?? [])]
  if (folderSort.value === 'time') list.sort((a, b) => b.mtime - a.mtime)
  else list.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
  return list
})

// B4 目录类型构成摘要：[图 12 · 视频 2 · 种子 1]，仅列出非零类型
function folderMix(item: ResourceItem): { label: string; count: number }[] {
  const counts: Record<string, number> = {}
  for (const f of item.files) counts[f.category] = (counts[f.category] ?? 0) + 1
  return (
    [
      { key: 'image', label: '图' },
      { key: 'video', label: '视频' },
      { key: 'torrent', label: '种子' },
      { key: 'text', label: '文本' },
      { key: 'other', label: '其他' },
    ] as const
  )
    .filter((m) => counts[m.key])
    .map((m) => ({ label: m.label, count: counts[m.key] }))
}

// B9 空壳目录：目录内只有磁力/云盘等文本清单、没有任何媒体文件（下载失败/被拦截的残留线索）
function isMedialess(item: ResourceItem): boolean {
  return item.files.every((f) => f.category === 'text')
}

// 目录模式（P0-1/2/3 原逻辑，作用于当前展开文件夹）过滤 + 排序后的文件列表
const filteredFiles = computed<ResourceFile[]>(() => {
  const kw = keyword.value.trim().toLowerCase()
  let list = activeFiles.value
  if (typeFilter.value !== 'all') list = list.filter((f) => f.category === typeFilter.value)
  if (kw) list = list.filter((f) => f.name.toLowerCase().includes(kw) || f.rel_path.toLowerCase().includes(kw))
  return sortFiles(list)
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
function fitHeight(rows: number): number {
  if (rows === 0) return 0
  return Math.min(rows * 36 + 40, 420)
}
const tableHeight = computed(() => fitHeight(filteredFiles.value.length))
const globalTableHeight = computed(() => fitHeight(globalFiles.value.length))

// 表格宽度跟随容器（用 :ref 函数避免 v-for 重复 ref）
const tableWidth = ref(900)
let tableWrapRef: HTMLDivElement | null = null
let resizeObserver: ResizeObserver | null = null

function setTableWrapRef(el: unknown, name: string) {
  if (name === active.value) tableWrapRef = (el as HTMLDivElement) ?? null
}

// B2 全局结果模式表格的独立容器（与目录模式共用宽度测量）
const globalWrap = ref<HTMLDivElement | null>(null)

function measureWidth() {
  // 全局结果模式表格在独立容器中，按当前模式取实际容器测宽
  const el = globalMode.value ? globalWrap.value : tableWrapRef
  if (el) tableWidth.value = Math.floor(el.getBoundingClientRect().width)
}

// 切换全局/目录模式后重测表格宽度（全局容器进入 DOM 后测量并纳入尺寸监听）
watch(globalMode, async () => {
  await nextTick()
  measureWidth()
  if (typeof ResizeObserver !== 'undefined' && resizeObserver && globalWrap.value) {
    resizeObserver.observe(globalWrap.value)
  }
})

// ---- B10 页面状态记忆（sessionStorage：搜索词/类型/列排序/目录排序/展开目录） ----
const STATE_KEY = 'resources_view_state'
function persistState() {
  try {
    sessionStorage.setItem(
      STATE_KEY,
      JSON.stringify({
        keyword: keyword.value,
        typeFilter: typeFilter.value,
        sortState: sortState.value,
        folderSort: folderSort.value,
        active: active.value,
      }),
    )
  } catch {
    /* 存储不可用（如隐私模式）时静默跳过，状态记忆为增强能力 */
  }
}
function restoreState() {
  try {
    const raw = sessionStorage.getItem(STATE_KEY)
    if (!raw) return
    const s = JSON.parse(raw) as Partial<{
      keyword: string
      typeFilter: typeof typeFilter.value
      sortState: typeof sortState.value
      folderSort: typeof folderSort.value
      active: string
    }>
    if (typeof s.keyword === 'string') keyword.value = s.keyword
    if (s.typeFilter) typeFilter.value = s.typeFilter
    if (s.sortState) sortState.value = s.sortState
    if (s.folderSort === 'time' || s.folderSort === 'name') folderSort.value = s.folderSort
    if (typeof s.active === 'string') active.value = s.active
  } catch {
    /* 历史状态损坏时忽略，按默认状态进入 */
  }
}
watch([keyword, typeFilter, sortState, folderSort, active], persistState)

// B5 图片预览：把 list 中全部图片拼为查看器地址列表，定位到当前文件
function previewImage(file: ResourceFile, list: ResourceFile[]) {
  const imgs = list.filter((f) => f.category === 'image')
  const idx = imgs.findIndex((f) => f.rel_path === file.rel_path)
  if (idx < 0) return
  viewerUrls.value = imgs.map((f) => resourceFileUrl(f.rel_path))
  viewerIndex.value = idx
  viewerVisible.value = true
}

function closeViewer() {
  viewerVisible.value = false
}

// B2 全局结果中点击命中目录：清空筛选并回到目录模式展开该目录
function clearFiltersAndExpand(name: string) {
  keyword.value = ''
  typeFilter.value = 'all'
  active.value = name
}

// B6 容量洞察：类型分布（按大小）、最大目录、Top 大文件
const categorySegments = computed(() => {
  const agg: Record<string, { count: number; size: number }> = {}
  for (const f of allFiles.value) {
    const a = (agg[f.category] ??= { count: 0, size: 0 })
    a.count += 1
    a.size += Number(f.size)
  }
  const total = allFiles.value.reduce((s, f) => s + Number(f.size), 0) || 1
  return Object.entries(agg)
    .map(([key, v]) => ({
      key,
      label: categoryMeta[key]?.label ?? key,
      color: categoryColors[key] ?? '#c0c4cc',
      count: v.count,
      size: v.size,
      pct: (v.size / total) * 100,
      pctText: `${((v.size / total) * 100).toFixed(1)}%`,
    }))
    .sort((a, b) => b.size - a.size)
})

const topFolders = computed<ResourceItem[]>(() =>
  [...(data.value?.items ?? [])].sort((a, b) => b.total_size - a.total_size).slice(0, 3),
)

const topFiles = computed<ResourceFile[]>(() =>
  [...allFiles.value].sort((a, b) => Number(b.size) - Number(a.size)).slice(0, 10),
)

// ---- 列定义 ----
// 目录模式：文件名列 / 类型 / 大小 / 相对路径 / 操作（图片行加「预览」，共用「复制路径」）
const columns: Columns<ResourceFile> = [
  {
    key: 'name',
    dataKey: 'name',
    title: '文件名',
    width: 320,
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
    width: 100,
    sortable: true,
    cellRenderer: ({ cellData }) => h('span', formatSize(Number(cellData))),
  },
  {
    key: 'rel_path',
    dataKey: 'rel_path',
    title: '相对路径',
    width: 250,
    ellipsis: true,
    cellRenderer: ({ rowData }) => h('span', { class: 'text-muted' }, `downloads/${rowData.rel_path}`),
  },
  {
    key: 'actions',
    title: '操作',
    width: 150,
    cellRenderer: ({ rowData }) => {
      const btns = [
        h(
          ElButton,
          { link: true, onClick: () => copyPath(rowData.rel_path) },
          () => '复制路径',
        ),
      ]
      if (rowData.category === 'image') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => previewImage(rowData, activeFiles.value) },
            () => '预览',
          ),
        )
      }
      return h('div', { class: 'row-actions' }, btns)
    },
  },
]

// 全局结果模式：额外展示「所属目录」列（B2）
const globalColumns: Columns<ResourceFile> = [
  {
    key: 'name',
    dataKey: 'name',
    title: '文件名',
    width: 300,
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
    width: 100,
    sortable: true,
    cellRenderer: ({ cellData }) => h('span', formatSize(Number(cellData))),
  },
  {
    key: 'folder',
    dataKey: 'rel_path',
    title: '所属目录',
    width: 220,
    ellipsis: true,
    cellRenderer: ({ rowData }) =>
      h('span', { class: 'text-muted' }, String(rowData.rel_path).split('/')[0]),
  },
  {
    key: 'actions',
    title: '操作',
    width: 150,
    cellRenderer: ({ rowData }) => {
      const btns = [
        h(
          ElButton,
          { link: true, onClick: () => copyPath(rowData.rel_path) },
          () => '复制路径',
        ),
      ]
      if (rowData.category === 'image') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => previewImage(rowData, globalFiles.value) },
            () => '预览',
          ),
        )
      }
      return h('div', { class: 'row-actions' }, btns)
    },
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

onMounted(() => {
  // 先恢复上次会话状态（搜索词/筛选/排序/展开目录），再拉数据
  restoreState()
  void load()
})

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

    <!-- B6 容量洞察：类型分布 / 最大目录 / Top10 大文件 -->
    <div v-if="data && data.total_files > 0" class="page-card insight-card">
      <div class="insight-block">
        <div class="insight-title">类型分布（按大小）</div>
        <div class="insight-bar">
          <span
            v-for="seg in categorySegments"
            :key="seg.key"
            class="seg"
            :style="{ width: `${seg.pct}%`, background: seg.color }"
          ></span>
        </div>
        <div class="insight-legend">
          <span v-for="seg in categorySegments" :key="seg.key" class="legend-item">
            <i class="legend-dot" :style="{ background: seg.color }"></i>
            {{ seg.label }} {{ formatSize(seg.size) }}（{{ seg.pctText }}）
          </span>
        </div>
      </div>
      <div class="insight-block">
        <div class="insight-title">最大目录（点击展开）</div>
        <div
          v-for="f in topFolders"
          :key="f.name"
          class="insight-line"
          :title="f.name"
          @click="clearFiltersAndExpand(f.name)"
        >
          <span class="insight-name">{{ f.name }}</span>
          <span class="text-muted">{{ formatSize(f.total_size) }}</span>
        </div>
      </div>
      <div class="insight-block">
        <div class="insight-title">Top10 大文件（点击复制路径）</div>
        <div
          v-for="f in topFiles"
          :key="f.rel_path"
          class="insight-line"
          :title="f.rel_path"
          @click="copyPath(f.rel_path)"
        >
          <span class="insight-name">{{ f.name }}</span>
          <span class="text-muted">{{ formatSize(Number(f.size)) }}</span>
        </div>
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
        <!-- P0-1/P0-2 工具栏；B2 起搜索与筛选作用于全部目录，B3 目录排序，B11 批量复制 -->
        <div class="toolbar">
          <el-input
            v-model="keyword"
            class="toolbar-search"
            placeholder="搜索全部目录内的文件名 / 路径 / 目录名"
            clearable
            :prefix-icon="'Search'"
          />
          <el-segmented v-model="typeFilter" :options="categoryOptions" />
          <el-select
            v-if="!globalMode"
            v-model="folderSort"
            class="folder-sort"
            size="default"
          >
            <el-option label="目录：按时间排序" value="time" />
            <el-option label="目录：按名称排序" value="name" />
          </el-select>
          <el-button class="copy-all" @click="copyAllPaths">复制全部路径</el-button>
        </div>

        <!-- B2 全局结果模式：命中目录 + 跨目录文件清单 -->
        <template v-if="globalMode">
          <div class="global-summary text-muted">
            命中 <b>{{ globalFiles.length }}</b> 个文件<template v-if="matchedFolders.length">
              ，<b>{{ matchedFolders.length }}</b> 个同名目录</template
            >
          </div>
          <div v-if="matchedFolders.length" class="matched-folders">
            <el-tag
              v-for="f in matchedFolders"
              :key="f.name"
              class="matched-folder-tag"
              @click="clearFiltersAndExpand(f.name)"
            >
              <el-icon><Folder /></el-icon>
              {{ f.name }}
            </el-tag>
          </div>
          <div ref="globalWrap">
            <el-table-v2
              v-if="globalFiles.length > 0"
              class="global-table"
              :columns="globalColumns"
              :data="globalFiles"
              :width="tableWidth"
              :height="globalTableHeight"
              :row-height="36"
              :header-height="40"
              :sort-state="sortState"
              @column-sort="onColumnSort"
            />
            <el-empty v-else :image-size="64" description="无匹配文件" />
          </div>
        </template>

        <!-- 目录模式（默认）：目录折叠列表 -->
        <template v-else>
          <div
            v-for="item in sortedFolders"
            :key="item.name"
            class="folder"
          >
            <div class="folder-head" @click="toggle(item.name)">
              <el-icon class="folder-arrow" :class="{ open: active === item.name }">
                <ArrowRight />
              </el-icon>
              <el-icon class="folder-icon"><Folder /></el-icon>
              <div class="folder-main">
                <div class="folder-line1">
                  <span class="folder-name" :title="item.name">{{ item.name }}</span>
                  <!-- B9 空壳目录标记：只有磁力/云盘清单、无媒体文件 -->
                  <el-tag v-if="isMedialess(item)" size="small" type="warning">未下载到媒体</el-tag>
                </div>
                <div class="folder-line2">
                  <span class="text-muted folder-meta">
                    {{ item.file_count }} 个文件 · {{ formatSize(item.total_size) }} · {{ fmtTime(item.mtime) }}
                  </span>
                  <!-- B4 类型构成摘要 -->
                  <span v-if="folderMix(item).length" class="folder-mix">
                    <template v-for="(m, i) in folderMix(item)" :key="m.label">
                      <span v-if="i" class="mix-dot">·</span>
                      <span>{{ m.label }} {{ m.count }}</span>
                    </template>
                  </span>
                  <!-- B1 来源帖回溯 -->
                  <template v-if="sourceOf(item.name)?.matched">
                    <span class="folder-source">
                      来源：{{ sourceOf(item.name)?.author || '未知作者'
                      }}<template v-if="sourceOf(item.name)?.date"> · {{ sourceOf(item.name)?.date }}</template>
                    </span>
                    <el-button link type="primary" class="src-btn" @click.stop="openSourceUrl(item.name)">
                      原帖
                    </el-button>
                    <el-button link type="primary" class="src-btn" @click.stop="goSourcePosts(item.name)">
                      看帖子
                    </el-button>
                  </template>
                  <!-- B7 下载任务关联 -->
                  <el-tag
                    v-if="taskOf(item.name)"
                    size="small"
                    type="success"
                    class="task-tag"
                    title="该目录由下载中心任务产生，点击查看"
                    @click.stop="goDownloads"
                  >
                    下载任务
                  </el-tag>
                </div>
              </div>
              <el-button link type="primary" class="copy-btn" @click.stop="copyPath(item.name)">
                复制目录名
              </el-button>
              <!-- B8 打开所在目录 -->
              <el-button link type="primary" class="open-btn" @click.stop="openFolder(item)">
                打开
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
      </template>
    </div>

    <!-- B5 图片大图预览（点击「预览」打开，Esc / 关闭按钮退出） -->
    <el-image-viewer
      v-if="viewerVisible"
      :url-list="viewerUrls"
      :initial-index="viewerIndex"
      teleported
      @close="closeViewer"
    />
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

.folder-sort {
  width: 170px;
}

.copy-all {
  margin-left: auto;
}

/* B6 容量洞察卡 */
.insight-card {
  display: flex;
  gap: 24px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.insight-block {
  flex: 1 1 260px;
  min-width: 0;
}

.insight-title {
  font-size: 13px;
  font-weight: 600;
  color: #1f2d3d;
  margin-bottom: 8px;
}

.insight-bar {
  display: flex;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
  background: #eef1f6;
}

.insight-bar .seg {
  display: block;
  height: 100%;
}

.insight-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin-top: 8px;
  font-size: 12px;
  color: #606266;
}

.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.legend-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.insight-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  line-height: 22px;
  cursor: pointer;
  border-radius: 4px;
  padding: 0 4px;
}

.insight-line:hover {
  background: #f6f8fc;
}

.insight-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* B2 全局结果模式 */
.global-summary {
  font-size: 13px;
  margin-bottom: 8px;
}

.matched-folders {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.matched-folder-tag {
  cursor: pointer;
  max-width: 420px;
}

.matched-folder-tag :deep(.el-icon) {
  vertical-align: -2px;
  margin-right: 2px;
}

.global-table {
  margin-bottom: 8px;
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
  padding: 10px 14px;
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
  flex-shrink: 0;
}

.folder-arrow.open {
  transform: rotate(90deg);
}

.folder-icon {
  color: #2f6fed;
  flex-shrink: 0;
}

.folder-main {
  flex: 1;
  min-width: 0;
}

.folder-line1 {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.folder-name {
  font-weight: 600;
  color: #1f2d3d;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-line2 {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
  margin-top: 2px;
}

.folder-meta {
  font-size: 12px;
}

.folder-mix {
  color: #606266;
}

.mix-dot {
  margin: 0 4px;
  color: #c0c4cc;
}

.folder-source {
  color: #606266;
}

.src-btn {
  padding: 0;
  height: auto;
}

.task-tag {
  cursor: pointer;
}

.copy-btn {
  flex-shrink: 0;
}

.open-btn {
  flex-shrink: 0;
  margin-left: 0;
}

.folder-body {
  padding: 0 14px 12px;
  border-top: 1px dashed var(--app-border);
  background: #fbfcfe;
}

/* 操作列按钮组 */
:deep(.row-actions) {
  display: flex;
  align-items: center;
  gap: 2px;
}
</style>
