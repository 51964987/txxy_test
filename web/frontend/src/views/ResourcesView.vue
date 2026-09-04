<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElButton, ElMessage, ElMessageBox, ElResult, ElTag } from 'element-plus'
import type { Columns } from 'element-plus'
import {
  api,
  formatSize,
  isAborted,
  resourceFileUrl,
  resourceVideoUrl,
  type ResourceFile,
  type ResourceItem,
  type ResourceSource,
  type Resources,
  type ResourceText,
  type TorrentInfo,
} from '../api'
import { useAppStore } from '../stores/app'
import { useTrash } from '../composables/useTrash'
import { formatMinuteTime } from '../utils/time'

const router = useRouter()
// 移动端形态沿用布局层的统一断点（<768px），页面不自建第二套判定
const app = useAppStore()
const isMobile = computed(() => app.isMobile)

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
    // 加载完成后并行补齐目录级信息：来源帖（B1）与下载任务关联（B7）；回收站数量用于工具栏角标
    void loadSources((data.value?.items ?? []).map((i) => i.name))
    void loadTasks()
    void loadTrash()
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

/** Top10 大文件的「原帖」：rel_path 首段即目录名（= 帖子标题），据此回溯来源帖 */
function openFileSource(file: ResourceFile) {
  openSourceUrl(String(file.rel_path).split('/')[0])
}

/** Top10 大文件的所属目录名（用于判断是否已匹配到来源帖） */
function fileDir(file: ResourceFile): string {
  return String(file.rel_path).split('/')[0]
}

// B7 跳转下载中心查看产生该目录的任务
function goDownloads() {
  void router.push('/downloads')
}

function taskOf(name: string) {
  return taskMap.value[name]
}

/** 复制文本到剪贴板（带 execCommand 降级）；当前仅用于复制种子磁链 */
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

// 时间展示统一走 utils/time：本文件曾自带一份补零与拼接，与 utils/time 重复

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

/** 类型枚举 → 中文标签（复用筛选选项，避免第二处硬编码） */
function categoryLabel(c: string): string {
  return categoryOptions.find((o) => o.value === c)?.label ?? c
}

// ---- 移动端排序：小屏没有表头可点，用「字段下拉 + 升降序」代替，复用同一套 sortState ----
const mobileSortKey = ref('name')
const mobileSortOrder = ref<'asc' | 'desc'>('asc')

watch([mobileSortKey, mobileSortOrder], ([k, o]) => {
  onColumnSort({ key: k, order: o })
})

// 桌面端点击表头排序后反向同步下拉显示，避免两处状态不一致
watch(
  sortState,
  (s) => {
    if (s.key) mobileSortKey.value = s.key
    if (s.order) mobileSortOrder.value = s.order
  },
  { deep: true },
)

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

// ===== 视频播放（弹窗，支持同列表连续播放）=====
const videoVisible = ref(false)
const videoList = ref<ResourceFile[]>([])
const videoIndex = ref(0)
const videoRef = ref<HTMLVideoElement | null>(null)
const videoTitle = computed(() => videoList.value[videoIndex.value]?.name ?? '')
const videoUrl = computed(() => {
  const cur = videoList.value[videoIndex.value]
  return cur ? resourceVideoUrl(cur.rel_path) : ''
})

function playVideo(file: ResourceFile, list: ResourceFile[]) {
  const vids = list.filter((f) => f.category === 'video')
  const idx = vids.findIndex((f) => f.rel_path === file.rel_path)
  if (idx < 0) return
  videoList.value = vids
  videoIndex.value = idx
  videoVisible.value = true
}

function goVideo(step: number) {
  const next = videoIndex.value + step
  if (next < 0 || next >= videoList.value.length) return
  videoIndex.value = next
}

/** 播放结束自动连播下一个；已是最后一个则不做处理 */
function onVideoEnded() {
  if (videoIndex.value < videoList.value.length - 1) goVideo(1)
}

/** 关闭时暂停并释放 src，避免后台继续缓冲占用带宽 */
function closeVideo() {
  const el = videoRef.value
  if (el) {
    el.pause()
    el.removeAttribute('src')
    el.load()
  }
  videoVisible.value = false
}

// ===== 类型兼容操作：文本查看 / 种子信息 / 用系统默认程序打开 =====
// 此前只有图片（预览）与视频（播放）有操作入口，文本 / 种子 / 其他类型只能复制路径或删除。
const textVisible = ref(false)
const textLoading = ref(false)
const textName = ref('')
const textData = ref<ResourceText | null>(null)

async function viewText(file: ResourceFile) {
  textName.value = file.name
  textData.value = null
  textVisible.value = true
  textLoading.value = true
  try {
    textData.value = await api.resourceText(file.rel_path)
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`读取文本失败: ${(e as Error).message}`)
    textVisible.value = false
  } finally {
    textLoading.value = false
  }
}

const torrentVisible = ref(false)
const torrentLoading = ref(false)
const torrentData = ref<TorrentInfo | null>(null)

async function showTorrent(file: ResourceFile) {
  torrentData.value = null
  torrentVisible.value = true
  torrentLoading.value = true
  try {
    torrentData.value = await api.resourceTorrent(file.rel_path)
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`解析种子失败: ${(e as Error).message}`)
    torrentVisible.value = false
  } finally {
    torrentLoading.value = false
  }
}

function copyMagnet(magnet: string) {
  copyText(magnet, '磁链已复制，可粘贴到下载工具')
}

/** 用系统默认程序打开文件（路径校验在后端；非 Windows 返回 501） */
async function openFileLocal(file: ResourceFile) {
  try {
    await api.openResourceFile(file.rel_path)
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`打开失败: ${(e as Error).message}`)
  }
}

// ===== 删除（软删除：移入回收站，保留期内可恢复）=====
async function removeFile(file: ResourceFile) {
  try {
    await ElMessageBox.confirm(
      `确定删除文件「${file.name}」（${formatSize(Number(file.size))}）？\n移入回收站后 ${trashKeepDays.value} 天内可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '移入回收站', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.deleteResource(file.rel_path, false)
    ElMessage.success('已移入回收站')
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

async function removeFolder(item: ResourceItem) {
  try {
    await ElMessageBox.confirm(
      `确定删除目录「${item.name}」及其 ${item.file_count} 个文件（${formatSize(item.total_size)}）？\n移入回收站后 ${trashKeepDays.value} 天内可恢复。`,
      '删除目录确认',
      { type: 'warning', confirmButtonText: '移入回收站', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.deleteResource(item.name, true)
    ElMessage.success('目录已移入回收站')
    if (active.value === item.name) active.value = ''
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

// ===== 回收站 =====
const trashVisible = ref(false)
// 回收站的数据与操作统一由 useTrash 提供（与 TrashView 表格版共用同一份实现）：
// 此前本文件与 TrashView 各写一套，恢复是否二次确认、清空文案是否带总量都已漂移不一致。
// onChanged 用于回收站变动后同步刷新本页资源列表。
const {
  items: trashItems,
  keepDays: trashKeepDays,
  totalSize: trashTotalSize,
  load: loadTrash,
  restoreItem,
  purgeItem,
  purgeAll,
} = useTrash({ onChanged: load })

async function openTrash() {
  trashVisible.value = true
  await loadTrash()
}

// B2 全局结果中点击命中目录：清空筛选并回到目录模式展开该目录
async function clearFiltersAndExpand(name: string) {
  keyword.value = ''
  typeFilter.value = 'all'
  active.value = name
  await scrollToFolder(name)
}

/** 目录行元素表（供「最大目录 / 命中目录」点击后滚动定位） */
const folderRefs = new Map<string, HTMLElement>()

function setFolderRef(el: unknown, name: string) {
  if (el) folderRefs.set(name, el as HTMLElement)
  else folderRefs.delete(name)
}

/**
 * 滚动到指定目录行。
 * 展开走的是折叠过渡动画，必须等高度稳定后再定位，否则目标位置会偏；
 * 系统开启「减少动态效果」时改为瞬时跳转，避免平滑滚动引起不适。
 */
async function scrollToFolder(name: string) {
  await nextTick()
  await new Promise((resolve) => setTimeout(resolve, 320))
  const el = folderRefs.get(name)
  if (!el) return
  const reduce =
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' })
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
// 目录模式：文件名 / 类型 / 大小 / 操作。
// 文件名列曾在 2026-09-02 移除，随即发现同目录下它是区分条目的唯一依据——
// 只剩「类型 / 大小 / 操作」时满屏同类条目无从分辨，故已恢复。
// 宽度收敛到 320，并用 flexGrow 吸收剩余空间，兼顾可读性与铺满。
const columns: Columns<ResourceFile> = [
  {
    key: 'name',
    dataKey: 'name',
    title: '文件名',
    width: 320,
    flexGrow: 1,
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
    key: 'actions',
    title: '操作',
    width: 170,
    cellRenderer: ({ rowData }) => {
      const btns = [
        h(
          ElButton,
          { link: true, type: 'danger', onClick: () => removeFile(rowData) },
          () => '删除',
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
      } else if (rowData.category === 'video') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => playVideo(rowData, activeFiles.value) },
            () => '播放',
          ),
        )
      } else if (rowData.category === 'text') {
        btns.unshift(
          h(ElButton, { link: true, type: 'primary', onClick: () => viewText(rowData) }, () => '查看'),
        )
      } else if (rowData.category === 'torrent') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => showTorrent(rowData) },
            () => '种子信息',
          ),
        )
      }
      // 所有类型都可用系统默认程序打开（压缩包、种子等本地处理更直接）
      btns.unshift(h(ElButton, { link: true, onClick: () => openFileLocal(rowData) }, () => '打开'))
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
    width: 260,
    cellRenderer: ({ rowData }) => {
      const btns = [
        h(
          ElButton,
          { link: true, type: 'danger', onClick: () => removeFile(rowData) },
          () => '删除',
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
      } else if (rowData.category === 'video') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => playVideo(rowData, globalFiles.value) },
            () => '播放',
          ),
        )
      } else if (rowData.category === 'text') {
        btns.unshift(
          h(ElButton, { link: true, type: 'primary', onClick: () => viewText(rowData) }, () => '查看'),
        )
      } else if (rowData.category === 'torrent') {
        btns.unshift(
          h(
            ElButton,
            { link: true, type: 'primary', onClick: () => showTorrent(rowData) },
            () => '种子信息',
          ),
        )
      }
      btns.unshift(h(ElButton, { link: true, onClick: () => openFileLocal(rowData) }, () => '打开'))
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
      <!-- 第 4 格原为「刷新」按钮（占一整格却只有一个动作）；改为回收站指标，
           既补上「待清理/可恢复」的信息，又可直接点开回收站；刷新移入工具栏 -->
      <div class="stat-card stat-card-action" title="点击查看回收站" @click="openTrash">
        <div class="stat-icon" style="background: #909399">
          <el-icon><Delete /></el-icon>
        </div>
        <div>
          <div class="stat-label">回收站</div>
          <div class="stat-value">
            {{ trashItems.length }} 项<template v-if="trashItems.length">
              · {{ formatSize(trashTotalSize) }}</template
            >
          </div>
        </div>
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
        <div class="insight-title">Top10 大文件</div>
        <div v-for="f in topFiles" :key="f.rel_path" class="insight-line insight-file">
          <span class="insight-name" :title="f.name">{{ f.name }}</span>
          <!-- Top10 跨目录聚合，单看文件名不知道属于哪个帖子，故补上所属目录名 -->
          <span class="insight-dir text-muted" :title="fileDir(f)">{{ fileDir(f) }}</span>
          <span class="text-muted">{{ formatSize(Number(f.size)) }}</span>
          <span class="insight-ops">
            <el-button
              v-if="f.category === 'image'"
              link
              type="primary"
              size="small"
              @click.stop="previewImage(f, topFiles)"
            >
              预览
            </el-button>
            <el-button
              v-else-if="f.category === 'video'"
              link
              type="primary"
              size="small"
              @click.stop="playVideo(f, topFiles)"
            >
              播放
            </el-button>
            <el-button
              v-else-if="f.category === 'text'"
              link
              type="primary"
              size="small"
              @click.stop="viewText(f)"
            >
              查看
            </el-button>
            <el-button
              v-else-if="f.category === 'torrent'"
              link
              type="primary"
              size="small"
              @click.stop="showTorrent(f)"
            >
              种子信息
            </el-button>
            <el-button
              v-if="sourceOf(fileDir(f))?.matched"
              link
              type="primary"
              size="small"
              @click.stop="openFileSource(f)"
            >
              原帖
            </el-button>
            <el-button link type="danger" size="small" @click.stop="removeFile(f)">
              删除
            </el-button>
          </span>
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
          <!-- 移动端没有表头可点，排序改用下拉 + 升降序切换（复用同一套 sortState） -->
          <div v-if="isMobile" class="mobile-sort">
            <el-select v-model="mobileSortKey" size="default" class="ms-key">
              <el-option label="按名称" value="name" />
              <el-option label="按类型" value="category" />
              <el-option label="按大小" value="size" />
            </el-select>
            <el-button
              size="default"
              @click="mobileSortOrder = mobileSortOrder === 'asc' ? 'desc' : 'asc'"
            >
              {{ mobileSortOrder === 'asc' ? '升序' : '降序' }}
            </el-button>
          </div>
          <!-- 动作组统一靠右（互联网文件/网盘列表的常见布局：筛选在左、动作在右） -->
          <div class="toolbar-right">
            <el-button type="warning" plain @click="openTrash">
              回收站<template v-if="trashItems.length">（{{ trashItems.length }}）</template>
            </el-button>
            <el-button :icon="'Refresh'" :loading="loading" @click="load">刷新</el-button>
          </div>
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
              v-if="!isMobile && globalFiles.length > 0"
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
            <!-- 移动端卡片：结构与目录模式一致（改一处需同步另一处），
                 副信息里的完整路径天然充当「所属目录」 -->
            <div v-else-if="isMobile && globalFiles.length > 0" class="file-cards">
              <div v-for="f in globalFiles" :key="f.rel_path" class="file-card">
                <div class="fc-main">
                  <div class="fc-name" :title="f.name">
                    <i
                      class="fc-dot"
                      :style="{ background: categoryColors[f.category] ?? '#c0c4cc' }"
                    ></i>
                    {{ f.name }}
                  </div>
                  <div class="fc-meta text-muted">
                    <span>{{ categoryLabel(f.category) }}</span>
                    <span>{{ formatSize(Number(f.size)) }}</span>
                  </div>
                </div>
                <div class="fc-ops">
                  <el-button
                    v-if="f.category === 'image'"
                    size="small"
                    type="primary"
                    link
                    @click="previewImage(f, globalFiles)"
                  >
                    预览
                  </el-button>
                  <el-button
                    v-else-if="f.category === 'video'"
                    size="small"
                    type="primary"
                    link
                    @click="playVideo(f, globalFiles)"
                  >
                    播放
                  </el-button>
                  <el-button
                    v-else-if="f.category === 'text'"
                    size="small"
                    type="primary"
                    link
                    @click="viewText(f)"
                  >
                    查看
                  </el-button>
                  <el-button
                    v-else-if="f.category === 'torrent'"
                    size="small"
                    type="primary"
                    link
                    @click="showTorrent(f)"
                  >
                    种子信息
                  </el-button>
                  <el-dropdown trigger="click">
                    <el-button size="small" link>更多</el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item @click="openFileLocal(f)">打开</el-dropdown-item>
                        <el-dropdown-item @click="removeFile(f)">删除</el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </div>
              </div>
            </div>
            <el-empty v-else :image-size="64" description="无匹配文件" />
          </div>
        </template>

        <!-- 目录模式（默认）：目录折叠列表 -->
        <template v-else>
          <div
            v-for="item in sortedFolders"
            :key="item.name"
            class="folder"
            :ref="(el) => setFolderRef(el, item.name)"
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
                    {{ item.file_count }} 个文件 · {{ formatSize(item.total_size) }} · {{ formatMinuteTime(item.mtime) }}
                  </span>
                  <!-- B4 类型构成摘要 -->
                  <span v-if="folderMix(item).length" class="folder-mix">
                    <template v-for="(m, i) in folderMix(item)" :key="m.label">
                      <span v-if="i" class="mix-dot">·</span>
                      <span>{{ m.label }} {{ m.count }}</span>
                    </template>
                  </span>
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
                <!-- B1 来源回溯独立一行：与基础属性分行，避免目录头信息密度过高难以扫读 -->
                <div v-if="sourceOf(item.name)?.matched" class="folder-line3">
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
                </div>
              </div>
              <!-- 桌面端：操作平铺（横向空间充足） -->
              <template v-if="!isMobile">
                <el-button link type="danger" class="del-btn" @click.stop="removeFolder(item)">
                  删除目录
                </el-button>
              </template>
              <!-- 移动端：三个按钮平铺会占掉约 200px，把目录名挤成十来个字；
                   收进「更多」菜单（网盘移动端通行做法），把宽度让给标题 -->
              <el-dropdown v-else trigger="click">
                <el-button link class="folder-more" @click.stop>更多</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="removeFolder(item)">删除目录</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>

            <el-collapse-transition>
              <div v-show="active === item.name" class="folder-body">
                <div :ref="(el) => setTableWrapRef(el, item.name)">
                  <!-- 桌面端：虚拟滚动表格（列宽固定，窄屏必然横向溢出，故小屏换形态） -->
                  <el-table-v2
                    v-if="!isMobile && active === item.name && filteredFiles.length > 0"
                    :columns="columns"
                    :data="filteredFiles"
                    :width="tableWidth"
                    :height="tableHeight"
                    :row-height="36"
                    :header-height="40"
                    :sort-state="sortState"
                    @column-sort="onColumnSort"
                  />
                  <!-- 移动端：单列卡片列表（网盘移动端的通行做法：主信息 + 副信息 + 操作） -->
                  <div
                    v-else-if="isMobile && active === item.name && filteredFiles.length > 0"
                    class="file-cards"
                  >
                    <div v-for="f in filteredFiles" :key="f.rel_path" class="file-card">
                      <div class="fc-main">
                        <div class="fc-name" :title="f.name">
                          <i
                            class="fc-dot"
                            :style="{ background: categoryColors[f.category] ?? '#c0c4cc' }"
                          ></i>
                          {{ f.name }}
                        </div>
                        <div class="fc-meta text-muted">
                          <span>{{ categoryLabel(f.category) }}</span>
                          <span>{{ formatSize(Number(f.size)) }}</span>
                          <!-- 全局模式下只显示所属目录名，与桌面端「所属目录」列一致 -->
                          <span>{{ f.rel_path.split('/')[0] }}</span>
                        </div>
                      </div>
                      <div class="fc-ops">
                        <el-button
                          v-if="f.category === 'image'"
                          size="small"
                          type="primary"
                          link
                          @click="previewImage(f, activeFiles)"
                        >
                          预览
                        </el-button>
                        <el-button
                          v-else-if="f.category === 'video'"
                          size="small"
                          type="primary"
                          link
                          @click="playVideo(f, activeFiles)"
                        >
                          播放
                        </el-button>
                        <el-button
                          v-else-if="f.category === 'text'"
                          size="small"
                          type="primary"
                          link
                          @click="viewText(f)"
                        >
                          查看
                        </el-button>
                        <el-button
                          v-else-if="f.category === 'torrent'"
                          size="small"
                          type="primary"
                          link
                          @click="showTorrent(f)"
                        >
                          种子信息
                        </el-button>
                        <el-dropdown trigger="click">
                          <el-button size="small" link>更多</el-button>
                          <template #dropdown>
                            <el-dropdown-menu>
                              <el-dropdown-item @click="openFileLocal(f)">打开</el-dropdown-item>
                              <el-dropdown-item divided @click="removeFile(f)">
                                删除
                              </el-dropdown-item>
                            </el-dropdown-menu>
                          </template>
                        </el-dropdown>
                      </div>
                    </div>
                  </div>
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

    <!-- 视频播放弹窗：同一列表内可连续播放，播放结束自动下一个 -->
    <el-dialog
      v-model="videoVisible"
      :title="videoTitle"
      width="70%"
      top="6vh"
      destroy-on-close
      @close="closeVideo"
    >
      <video
        ref="videoRef"
        class="video-player"
        :src="videoUrl"
        controls
        preload="metadata"
        autoplay
        @ended="onVideoEnded"
      />
      <template #footer>
        <div class="video-footer">
          <span class="text-muted">
            第 {{ videoList.length ? videoIndex + 1 : 0 }} / {{ videoList.length }} 个
          </span>
          <div>
            <el-button :disabled="videoIndex <= 0" @click="goVideo(-1)">上一个</el-button>
            <el-button
              type="primary"
              :disabled="videoIndex >= videoList.length - 1"
              @click="goVideo(1)"
            >
              下一个
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <!-- 文本查看：受控读取 .txt/.md/.log，编码兜底，超大文件截断 -->
    <el-dialog v-model="textVisible" :title="`查看文本 · ${textName}`" width="60%" top="8vh">
      <el-skeleton v-if="textLoading" :rows="6" animated />
      <template v-else-if="textData">
        <div class="text-meta text-muted">
          {{ formatSize(textData.size) }} · 编码 {{ textData.encoding }}
          <template v-if="textData.truncated"> · 文件较大，仅显示前 512 KB</template>
        </div>
        <pre class="text-body">{{ textData.text }}</pre>
      </template>
      <el-empty v-else :image-size="64" description="无内容" />
    </el-dialog>

    <!-- 种子信息：解析 .torrent，给出文件清单与磁链（本地打开/下载用） -->
    <el-dialog v-model="torrentVisible" title="种子信息" width="60%" top="8vh">
      <el-skeleton v-if="torrentLoading" :rows="6" animated />
      <template v-else-if="torrentData">
        <div class="torrent-head">
          <div class="torrent-name" :title="torrentData.name">{{ torrentData.name }}</div>
          <div class="text-muted">
            {{ torrentData.file_count }} 个文件 · {{ formatSize(torrentData.total_size) }}
          </div>
        </div>
        <div class="magnet-row">
          <el-input :model-value="torrentData.magnet" readonly size="small" class="magnet-input" />
          <el-button type="primary" size="small" @click="copyMagnet(torrentData.magnet)">
            复制磁链
          </el-button>
        </div>
        <div class="torrent-hash text-muted">infohash：{{ torrentData.infohash }}</div>
        <div class="torrent-files">
          <div v-for="(f, i) in torrentData.files" :key="i" class="torrent-file">
            <span class="tf-path" :title="f.path">{{ f.path }}</span>
            <span class="text-muted">{{ formatSize(f.size) }}</span>
          </div>
          <div v-if="torrentData.files_truncated" class="text-muted">
            文件较多，仅显示前 {{ torrentData.files.length }} 个
          </div>
        </div>
      </template>
      <el-empty v-else :image-size="64" description="无法解析该种子" />
    </el-dialog>

    <!-- 回收站：软删除项，保留期内可恢复，也可彻底删除 -->
    <el-drawer v-model="trashVisible" title="回收站" size="520px">
      <div class="trash-head">
        <span class="text-muted">
          共 {{ trashItems.length }} 项 · {{ formatSize(trashTotalSize) }} · 保留
          {{ trashKeepDays }} 天
        </span>
        <el-button
          type="danger"
          plain
          size="small"
          :disabled="!trashItems.length"
          @click="purgeAll"
        >
          清空回收站
        </el-button>
      </div>
      <el-empty v-if="!trashItems.length" description="回收站为空" />
      <div v-else class="trash-list">
        <div v-for="it in trashItems" :key="it.id" class="trash-item">
          <div class="trash-main">
            <div class="trash-name" :title="it.rel">
              {{ it.name }}
              <el-tag v-if="it.is_dir" size="small" class="trash-tag">目录</el-tag>
            </div>
            <div class="trash-meta text-muted">
              {{ formatSize(it.size) }} ·
              <template v-if="it.expired">已过保留期</template>
              <template v-else>剩余 {{ it.remain_days }} 天</template>
            </div>
          </div>
          <div class="trash-ops">
            <el-button link type="primary" size="small" @click="restoreItem(it)">恢复</el-button>
            <el-button link type="danger" size="small" @click="purgeItem(it)">彻底删除</el-button>
          </div>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* P0-4 刷新按钮所在卡片：作为第 4 格，内容居中放置按钮 */
/* 第 4 格改为「回收站」指标卡（可点击打开抽屉）。
   原先这整格只放一个「刷新」按钮，占满一格却只有一个动作，性价比低 */
.stat-card-action {
  cursor: pointer;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}

.stat-card-action:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
  transform: translateY(-1px);
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

/* 动作组统一靠右（复制全部 / 回收站 / 刷新），窄屏随 toolbar 的 wrap 整体换行 */
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

/* 目录头第三行：来源回溯独立成行，降低单行信息密度 */
.folder-line3 {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
  margin-top: 2px;
}

/* 文本查看弹窗：等宽字体 + 独立滚动，长文本不撑破弹窗 */
.text-meta {
  font-size: 12px;
  margin-bottom: 8px;
}

.text-body {
  margin: 0;
  padding: 12px;
  max-height: 55vh;
  overflow: auto;
  background: #f7f8fa;
  border-radius: 6px;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 种子信息弹窗 */
.torrent-head {
  margin-bottom: 10px;
}

.torrent-name {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.magnet-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.magnet-input {
  flex: 1;
  min-width: 0;
  font-family: Consolas, Monaco, 'Courier New', monospace;
  font-size: 12px;
}

.torrent-hash {
  font-size: 12px;
  margin-top: 6px;
  word-break: break-all;
}

.torrent-files {
  margin-top: 10px;
  max-height: 40vh;
  overflow: auto;
  border-top: 1px solid #ebeef5;
}

.torrent-file {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  font-size: 12px;
  border-bottom: 1px solid #f5f7fa;
}

.tf-path {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ================= 移动端适配 =================
   断点与布局层 isMobile 一致（<768px）。表格列宽固定，窄屏必然横向溢出，
   故小屏改为单列卡片列表（网盘移动端的通行做法：主信息 + 副信息 + 操作）。 */
.file-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.file-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  /* 触控友好：最小高度 56px（移动端可点区域建议 ≥44px） */
  min-height: 56px;
  padding: 8px 10px;
  background: #fafcff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
}

.fc-main {
  flex: 1;
  min-width: 0;
}

.fc-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fc-dot {
  flex-shrink: 0;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.fc-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: 12px;
  margin-top: 2px;
}

.fc-ops {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 2px;
}

.mobile-sort {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ms-key {
  width: 120px;
}

@media (max-width: 767px) {
  /* 搜索框独占一行，动作组换行铺满（避免与筛选控件挤在一行） */
  .toolbar-search {
    width: 100%;
  }

  .toolbar-right {
    width: 100%;
    margin-left: 0;
    flex-wrap: wrap;
  }

  .insight-card {
    gap: 16px;
  }

  /* 目录头相关规则见「样式表末尾」的移动端块：它们需要覆盖 .folder-name / .folder-head
     等基础样式，同特异性下必须写在基础规则之后才会生效。 */
}

/* B6 容量洞察卡 */
/* 洞察卡布局（2026-09-02）：改为 2 列网格。
   业界仪表盘（Grafana / GA / BI 看板）的通行分法——
   「整体构成」这类全局概览给整行（视觉权重最高），
   两个同级的明细列表并排各占 1/2（信息密度相当，不该一大一小）。 */
.insight-card {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 24px;
  margin-bottom: 12px;
}

/* 类型分布是全局构成，独占整行 */
.insight-block:first-child {
  grid-column: 1 / -1;
}

.insight-block {
  min-width: 0;
}

/* Top10 行的所属目录名：限宽 + 省略，避免把文件名挤没 */
.insight-dir {
  flex: 0 1 auto;
  max-width: 40%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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

/* Top10 大文件：名称（省略）+ 大小 + 类型化操作。
   不再整行可点复制路径，故覆盖 .insight-line 的可点光标与 hover 反馈。 */
.insight-file {
  cursor: default;
}

.insight-file:hover {
  background: transparent;
}

.insight-file .insight-name {
  flex: 1;
  min-width: 0;
}

.insight-ops {
  flex-shrink: 0;
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

.del-btn {
  flex-shrink: 0;
  margin-left: 0;
}

/* 视频播放弹窗 */
.video-player {
  width: 100%;
  max-height: 70vh;
  background: #000;
  border-radius: 6px;
  display: block;
}

.video-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

/* 回收站抽屉 */
.trash-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.trash-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.trash-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--app-border);
  border-radius: 6px;
}

.trash-main {
  min-width: 0;
  flex: 1;
}

.trash-name {
  font-size: 13px;
  color: #303133;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.trash-tag {
  margin-left: 6px;
}

.trash-meta {
  margin-top: 4px;
  font-size: 12px;
}

.trash-ops {
  flex-shrink: 0;
  display: flex;
  gap: 4px;
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

/* ================= 移动端目录头（必须放在样式表末尾） =================
   本块覆盖上方的 .folder-head / .folder-line1 / .folder-name 基础规则。
   CSS 同特异性下「后定义者胜」，写在前面会被基础样式整块覆盖而静默失效
   （曾放在样式表中部导致两行截断完全没生效），故固定在末尾并注明原因。 */
@media (max-width: 767px) {
  .folder-head {
    flex-wrap: wrap;
    align-items: flex-start;
    padding: 12px;
  }

  /* 标题旁的「未下载到媒体」等 Tag 允许换行，不与标题争抢横向空间 */
  .folder-line1 {
    flex-wrap: wrap;
  }

  /* 目录名即帖子标题，普遍很长：单行省略只能看到十来个字。
     改为两行截断（Material / iOS 文件列表的通行做法）。 */
  .folder-name {
    white-space: normal;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.4;
  }

  /* 「更多」按钮：保持 44px 触控高度，同时尽量窄，把宽度让给目录名 */
  .folder-more {
    min-height: 44px;
    min-width: 40px;
    padding: 0 4px;
  }
}
</style>
