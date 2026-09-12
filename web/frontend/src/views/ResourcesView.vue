<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElButton, ElCheckbox, ElMessage, ElMessageBox, ElResult, ElTag } from 'element-plus'
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
import { legacyCopy, copyText } from '../utils/clipboard'
import { categoryMeta, categoryColors, categoryOptions, categoryLabel, CATEGORY_ORDER } from '../utils/category'

const router = useRouter()
const route = useRoute()
// 移动端形态沿用布局层的统一断点（<768px），页面不自建第二套判定
const app = useAppStore()
const isMobile = computed(() => app.isMobile)

const data = ref<Resources | null>(null)
const loading = ref(false)
const loadError = ref('') // 加载失败信息（非空时展示重试界面）
const active = ref('') // 当前展开的文件夹

const totalSizeText = computed(() => formatSize(data.value?.total_size ?? 0))

// 类型元数据（标签/颜色/筛选选项）统一从 utils/category 引入，避免第二份硬编码
const typeFilter = ref<'all' | 'image' | 'video' | 'torrent' | 'text' | 'other'>('all') // P0-2 类型筛选

// P0-3 排序：el-table-v2 原生列排序状态（目录模式与全局结果模式共用）
const sortState = ref<{ key: string; order: 'asc' | 'desc' | null }>({ key: '', order: null })

// B3 目录排序：按下载时间（目录 mtime）最新优先 / 按名称
const folderSort = ref<'time' | 'name'>('time')

// 目录列表分页：145+ 个目录一次全渲染既长又慢（每个 header 还要聚合类型构成）。
// 业界网盘（百度/Google Drive）的标准做法是分页展示，故这里按页切片。
const folderPage = ref(1)
const folderPageSize = ref(30)
/** 当前页要渲染的目录（sortedFolders 的切片）；-1 表示不分页（全部） */
const pagedFolders = computed<ResourceItem[]>(() => {
  const all = sortedFolders.value
  if (folderPageSize.value < 0) return all
  const start = (folderPage.value - 1) * folderPageSize.value
  return all.slice(start, start + folderPageSize.value)
})
/** 排序/搜索口径变化时回到第一页，避免停留在空白页 */
function resetFolderPage() {
  folderPage.value = 1
}
function onFolderPage(p: number) {
  folderPage.value = p
}
function onFolderPageSize(s: number) {
  folderPageSize.value = s
  folderPage.value = 1
}
/** 「显示全部」：关闭分页（folderPageSize = -1，pagedFolders 返回全部） */
function showAllFolders() {
  folderPageSize.value = -1
}
/** 「分页显示」：从「全部」切回分页（默认每页 30） */
function resetToPaged() {
  folderPageSize.value = 30
  folderPage.value = 1
}

// B1 来源回溯缓存：目录名 -> 来源帖信息（会话内不重复请求）
const sourceMap = ref<Record<string, ResourceSource>>({})

// B5 图片预览查看器状态
const viewerVisible = ref(false)
const viewerUrls = ref<string[]>([])
const viewerIndex = ref(0)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    data.value = await api.resources()
    // 加载完成后并行补齐目录级信息：来源帖（B1）；回收站数量用于工具栏角标
    void loadSources((data.value?.items ?? []).map((i) => i.name))
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

/** Top10 大文件的「原帖」：rel_path 首段即目录名（= 帖子标题），据此回溯来源帖 */
function openFileSource(file: ResourceFile) {
  openSourceUrl(String(file.rel_path).split('/')[0])
}

/** Top10 大文件的所属目录名（用于判断是否已匹配到来源帖） */
function fileDir(file: ResourceFile): string {
  return String(file.rel_path).split('/')[0]
}

// B7 跳转下载中心查看产生该目录的任务

// 复制实现统一走 utils/clipboard（与帖子浏览「复制链接」共用，含 execCommand 降级
// 与返回值校验），本页不再自留一份

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

// 全局模式（搜索 / 类型筛选）为平铺文件列表 + 前端分页（2026-09-08 按用户要求去掉目录分组——
// 每张卡片已带类型标签，组头目录行是冗余）。命中量可达数千（全库 9000+ 文件），
// 必须受控渲染：分页切片，每页仅渲染一页（原「组内 20 / 总量 200」截断方案随之移除）。
// 不提供「显示全部」：数千卡片一次渲染会卡死页面（二十八节的教训）。
const globalPageSize = ref(30)
const globalPage = ref(1)

const globalPaged = computed<ResourceFile[]>(() =>
  globalFiles.value.slice(
    (globalPage.value - 1) * globalPageSize.value,
    globalPage.value * globalPageSize.value,
  ),
)

function onGlobalPage(p: number) {
  globalPage.value = p
}
function onGlobalPageSize(s: number) {
  globalPageSize.value = s
  globalPage.value = 1
}
// 筛选口径 / 排序变化回到第一页，避免停留在超出范围的页码
watch([keyword, typeFilter, sortState], () => {
  globalPage.value = 1
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

/** 文件所在路径（含子目录，如「目录名/子目录」；无子目录时即目录名），行内元信息展示用 */
function fileParentPath(f: ResourceFile): string {
  const i = f.rel_path.lastIndexOf('/')
  return i > 0 ? f.rel_path.slice(0, i) : f.rel_path.split('/')[0]
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

// 表格宽度跟随容器（用 :ref 函数避免 v-for 重复 ref）
const tableWidth = ref(900)
let tableWrapRef: HTMLDivElement | null = null
let resizeObserver: ResizeObserver | null = null

function setTableWrapRef(el: unknown, name: string) {
  if (name === active.value) tableWrapRef = (el as HTMLDivElement) ?? null
}

function measureWidth() {
  if (tableWrapRef) tableWidth.value = Math.floor(tableWrapRef.getBoundingClientRect().width)
}

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

// ===== 缩略图受控加载（限流友好 + 429 自动退避重试 + 错误态区分） =====
// 背景：预览接口限 300 次/分（原 60），但一页 30~60 张缩略图若瞬间并发仍会撞限流，
// 而 el-image 的 error 无法区分 429 / 文件损坏，一律显示「加载失败」。
// 做法：自管队列 → 受控并发取图（fetch 可拿到状态码）→ 成功转 blob URL 交给 el-image，
// 429 退避重试（最多 3 次），其余失败标记 broken；错误态给出「重试 / 打开」入口。
type ThumbStatus = 'queued' | 'loading' | 'retrying' | 'ok' | 'error'
interface ThumbState {
  status: ThumbStatus
  url?: string
  tries: number
}
const THUMB_CONCURRENCY = 6
const THUMB_MAX_TRIES = 3
const thumbs = ref<Record<string, ThumbState>>({})
const thumbQueue: string[] = []
let thumbActive = 0
let thumbTimer: ReturnType<typeof setTimeout> | null = null
const thumbBlobUrls: string[] = []

function thumbState(rel: string): ThumbState {
  return thumbs.value[rel] ?? { status: 'queued', tries: 0 }
}

function setThumb(rel: string, st: ThumbState) {
  thumbs.value = { ...thumbs.value, [rel]: st }
}

function enqueueThumbs(rels: string[]) {
  for (const rel of rels) {
    if (thumbs.value[rel]?.status === 'ok') continue
    if (!thumbs.value[rel]) setThumb(rel, { status: 'queued', tries: 0 })
    if (!thumbQueue.includes(rel)) thumbQueue.push(rel)
  }
  void pumpThumbs()
}

async function pumpThumbs() {
  while (thumbActive < THUMB_CONCURRENCY && thumbQueue.length) {
    const rel = thumbQueue.shift() as string
    thumbActive += 1
    void loadThumb(rel).finally(() => {
      thumbActive -= 1
      void pumpThumbs()
    })
  }
}

async function loadThumb(rel: string) {
  const st = thumbState(rel)
  if (st.status === 'ok') return
  setThumb(rel, { status: 'loading', tries: st.tries, url: st.url })
  try {
    const res = await fetch(resourceFileUrl(rel))
    if (res.status === 429) {
      const tries = st.tries + 1
      if (tries < THUMB_MAX_TRIES) {
        setThumb(rel, { status: 'retrying', tries })
        // 退避重试：1.5s / 3s，重新入队（不占用并发槽）
        thumbTimer = setTimeout(() => {
          thumbQueue.push(rel)
          void pumpThumbs()
        }, 1500 * tries)
      } else {
        setThumb(rel, { status: 'error', tries })
      }
      return
    }
    if (!res.ok) {
      setThumb(rel, { status: 'error', tries: st.tries })
      return
    }
    const url = URL.createObjectURL(await res.blob())
    thumbBlobUrls.push(url)
    setThumb(rel, { status: 'ok', url, tries: st.tries })
  } catch {
    setThumb(rel, { status: 'error', tries: st.tries })
  }
}

/** 手动重试：错误态的「重试」入口 */
function retryThumb(rel: string) {
  setThumb(rel, { status: 'queued', tries: 0 })
  if (!thumbQueue.includes(rel)) thumbQueue.push(rel)
  void pumpThumbs()
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

const magnetInputRef = ref<{ $el: HTMLDivElement } | null>(null)

async function copyMagnet(magnet: string) {
  const okMsg = () => ElMessage.success('磁链已复制，可粘贴到下载工具')
  // 1) 安全上下文（HTTPS / localhost）走 Clipboard API
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(magnet)
      okMsg()
      return
    } catch {
      /* 落到可见输入框方案 */
    }
  }
  // 2) 非安全上下文（手机 http 访问）：选中弹窗内「可见」的磁链输入框后复制。
  //    部分移动浏览器会拒绝复制 opacity:0 隐藏元素的内容（execCommand 返回 true
  //    但剪贴板为空，即「提示成功、粘贴为空」的根因），对可见元素复制最可靠。
  const input = magnetInputRef.value?.$el?.querySelector('input') as HTMLInputElement | null
  if (input) {
    input.focus()
    input.select()
    if (document.execCommand('copy')) {
      okMsg()
      return
    }
  }
  // 3) 隐藏 textarea 兜底
  if (legacyCopy(magnet)) {
    okMsg()
    return
  }
  ElMessage.error('复制失败，请在上方输入框长按磁链手动复制')
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

// ===== 统一查看入口：一次点击兼容所有类型 =====
// 图片→大图查看器（同列表连看）、视频→播放弹窗（同列表连播）、文本→文本查看、
// 种子→种子信息、其他→系统默认程序打开。此前预览/播放/查看/种子信息四套按钮
// 分散在 5 处模板里按类型 if/else 重复，点击语义不统一。
function openResource(file: ResourceFile, list: ResourceFile[]) {
  if (file.category === 'image') previewImage(file, list)
  else if (file.category === 'video') playVideo(file, list)
  else if (file.category === 'text') viewText(file)
  else if (file.category === 'torrent') showTorrent(file)
  else openFileLocal(file)
}

// ===== 分享：可单文件，也可一次性分享整个目录 / 勾选的多个文件（不暴露前端看板） =====
// 一个分享链接对应一组文件（业界网盘「分享文件夹 / 多选分享」同思路），
// 由独立 8090 分享服务渲染为画廊式预览页。
const shareVisible = ref(false)
// 待分享文件集合（单文件时只有一个元素）
const shareTargets = ref<ResourceFile[]>([])
// 是否进入「自由勾选」模式（浏览抽屉「分享此目录」使用）：勾选子集而非整目录
const shareSelectable = ref(false)
// 已勾选的相对路径集合（仅 selectable 模式使用）
const shareSelected = ref<Set<string>>(new Set())
const shareTtl = ref<'1h' | '24h' | '7d' | '30d'>('7d')
const shareUrl = ref('')
const shareLoading = ref(false)

// 单文件分享入口（卡片 / 行内「分享」）
function openShare(file: ResourceFile) {
  shareSelectable.value = false
  shareTargets.value = [file]
  shareUrl.value = ''
  shareTtl.value = '7d'
  shareVisible.value = true
}

// 浏览抽屉「分享此目录」：弹出可勾选清单（支持全选/全不选），默认全选当前筛选结果
function openShareBrowser() {
  shareSelectable.value = true
  shareTargets.value = [...browserFiltered.value]
  shareSelected.value = new Set(browserFiltered.value.map((f) => f.rel_path))
  shareUrl.value = ''
  shareTtl.value = '7d'
  shareVisible.value = true
}

// 勾选切换（重赋值以触发响应式）
function togglePick(rel: string, val: unknown) {
  const on = val === true
  const s = new Set(shareSelected.value)
  if (on) s.add(rel)
  else s.delete(rel)
  shareSelected.value = s
}
// 全选 / 全不选
function pickAll(on: boolean) {
  shareSelected.value = new Set(on ? shareTargets.value.map((f) => f.rel_path) : [])
}
// 弹窗标题：反映当前勾选数量
const shareTitle = computed(() =>
  shareSelectable.value
    ? `分享 ${shareSelected.value.size} 个文件`
    : shareTargets.value.length > 1
      ? `分享 ${shareTargets.value.length} 个文件`
      : '分享文件',
)

async function generateShare() {
  // selectable 模式只分享已勾选的文件
  const targets = shareSelectable.value
    ? shareTargets.value.filter((f) => shareSelected.value.has(f.rel_path))
    : shareTargets.value
  if (!targets.length) {
    ElMessage.warning('请至少选择一个文件')
    return
  }
  shareLoading.value = true
  try {
    const r = await api.createShare(
      targets.map((f) => f.rel_path),
      shareTtl.value,
    )
    shareUrl.value = r.url
    const ok = await copyText(r.url)
    ElMessage.success(ok ? '链接已生成并复制' : '链接已生成，请手动复制')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`生成失败: ${(e as Error).message}`)
  } finally {
    shareLoading.value = false
  }
}

async function copyShare() {
  if (!shareUrl.value) return
  const ok = await copyText(shareUrl.value)
  ElMessage.success(ok ? '链接已复制' : '复制失败，请手动复制')
}

// ===== 目录级浏览：不展开目录即可浏览该目录全部资源 =====
const browserVisible = ref(false)
const browserFolder = ref<ResourceItem | null>(null)
// 浏览抽屉数据源双模式（2026-09-08）：
// folder = 单目录浏览（目录头「浏览」入口）；global = 跨目录浏览全部命中资源
// （全局模式命中行「浏览」入口——列表当前页只渲染 30 个，抽屉里可翻页看到后续页的资源）
const browserMode = ref<'folder' | 'global'>('folder')
// 浏览抽屉固定排序：先按类型（图→视频→种子→文本→其他，与筛选选项同序）再按文件名。
// 不跟随表格列排序状态——浏览是「按类型聚类浏览」场景，同类文件相邻更符合直觉；
// 文件名用 zh-Hans-CN locale（与目录排序一致），中文按拼音序。
// global 模式不重排：globalFiles 已按用户排序状态排好，抽屉与列表顺序保持一致
const CATEGORY_RANK: Record<string, number> = { image: 0, video: 1, torrent: 2, text: 3, other: 4 }
// 抽屉内排序：默认「类型优先」保留聚类浏览习惯；也可按名称 / 大小 / 时间 / 尺寸
type BrowserSort = 'category' | 'name' | 'size' | 'time' | 'dimension'
const browserSort = ref<BrowserSort>('category')
const browserSortOptions: { label: string; value: BrowserSort }[] = [
  { label: '类型优先', value: 'category' },
  { label: '按名称', value: 'name' },
  { label: '按大小', value: 'size' },
  { label: '按时间', value: 'time' },
  { label: '按尺寸', value: 'dimension' },
]
const browserFiles = computed<ResourceFile[]>(() => {
  if (browserMode.value === 'global') return globalFiles.value
  const list = [...(browserFolder.value?.files ?? [])]
  const key = browserSort.value
  if (key === 'name') {
    list.sort((a, b) => a.name.localeCompare(b.name, 'zh-Hans-CN'))
  } else if (key === 'size') {
    list.sort((a, b) => Number(b.size) - Number(a.size))
  } else if (key === 'time') {
    list.sort((a, b) => Number(b.mtime ?? 0) - Number(a.mtime ?? 0))
  } else if (key === 'dimension') {
    // 无尺寸（非图片/解析失败）的排最后，其余按像素面积倒序
    const area = (f: ResourceFile) => Number(f.width ?? 0) * Number(f.height ?? 0)
    list.sort((a, b) => area(b) - area(a))
  } else {
    list.sort((a, b) => {
      const ca = CATEGORY_RANK[a.category] ?? 9
      const cb = CATEGORY_RANK[b.category] ?? 9
      if (ca !== cb) return ca - cb
      return a.name.localeCompare(b.name, 'zh-Hans-CN')
    })
  }
  return list
})

/** 抽屉内图片汇总：张数与占用（配合类型构成一起给出容量感） */
const browserImageStats = computed(() => {
  const imgs = browserFiles.value.filter((f) => f.category === 'image')
  return { count: imgs.length, size: imgs.reduce((s, f) => s + Number(f.size), 0) }
})

// 资源过多时的展示优化（业界集合浏览通行做法：类型筛选 + 名称过滤 + 前端分页；
// 与目录列表分页、任务列表分页同一模式。图片懒加载保留，每页数量有限天然不触限流）：
// folder 模式移动端每页 30（缩略图流量与滚动负担更小）、桌面 60；
// global 模式两端统一每页 30，与命中列表分页规格一致（用户口径：这一页 30 个、翻页看下一批 30 个）
const browserPageSize = computed(() =>
  browserMode.value === 'global' ? 30 : isMobile.value ? 30 : 60,
)
const browserTypeFilter = ref<'all' | 'image' | 'video' | 'torrent' | 'text' | 'other'>('all')
const browserKeyword = ref('')
const browserPage = ref(1)

const browserFiltered = computed<ResourceFile[]>(() => {
  let list = browserFiles.value
  if (browserTypeFilter.value !== 'all') {
    list = list.filter((f) => f.category === browserTypeFilter.value)
  }
  const kw = browserKeyword.value.trim().toLowerCase()
  if (kw) list = list.filter((f) => f.name.toLowerCase().includes(kw))
  return list
})
const browserPaged = computed(() =>
  browserFiltered.value.slice(
    (browserPage.value - 1) * browserPageSize.value,
    browserPage.value * browserPageSize.value,
  ),
)
// 筛选/过滤/排序变化回到第一页，避免停留在超出范围的页码
watch([browserTypeFilter, browserKeyword, browserSort], () => {
  browserPage.value = 1
})

function openBrowser(item: ResourceItem) {
  if (!item.files.length) {
    ElMessage.info('该目录没有文件')
    return
  }
  browserMode.value = 'folder'
  browserFolder.value = item
  // 每次打开从干净状态开始，避免上一次的筛选残留在新目录上
  browserTypeFilter.value = 'all'
  browserKeyword.value = ''
  browserPage.value = 1
  browserVisible.value = true
}

/** 命中行「浏览」→ 跨目录浏览全部命中资源（与列表同一筛选口径，抽屉内分页可翻看后续页） */
function openBrowserAll() {
  if (!globalFiles.value.length) {
    ElMessage.info('当前没有命中的资源')
    return
  }
  browserMode.value = 'global'
  browserFolder.value = null
  browserTypeFilter.value = 'all'
  browserKeyword.value = ''
  browserPage.value = 1
  browserVisible.value = true
}

/** 浏览抽屉标题随数据源模式切换 */
const browserTitle = computed(() =>
  browserMode.value === 'global'
    ? '浏览全部命中资源'
    : browserFolder.value
      ? `浏览目录：${browserFolder.value.name}`
      : '浏览目录',
)

/** 抽屉摘要的类型构成（folder 模式专用；global 模式为空数组不渲染） */
const browserMix = computed(() =>
  browserMode.value === 'folder' && browserFolder.value ? folderMix(browserFolder.value) : [],
)

// ===== 视频首帧预览：浏览器内 canvas 抓帧（零后端依赖）=====
// 后端生成缩略图需引入 ffmpeg——8.2.2 已评估「依赖 ffmpeg，建议不做」；
// 改为前端抓首帧：只 seek 到 0.1s，Range 仅拉取少量数据；视频接口同源，canvas 不会被污染。
const videoPosters = ref<Record<string, string>>({})
// 缓存含「失败标记空串」，避免反复重试浪费带宽与限流额度
const posterCache = new Map<string, string>()

function captureVideoFrame(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const v = document.createElement('video')
    v.preload = 'metadata'
    v.muted = true
    v.playsInline = true
    v.src = url
    const cleanup = () => {
      v.removeAttribute('src')
      v.load()
    }
    v.onloadedmetadata = () => {
      v.currentTime = Math.min(0.1, (v.duration || 1) * 0.1)
    }
    v.onseeked = () => {
      try {
        const c = document.createElement('canvas')
        c.width = v.videoWidth || 160
        c.height = v.videoHeight || 100
        const ctx = c.getContext('2d')
        if (ctx) {
          ctx.drawImage(v, 0, 0, c.width, c.height)
          resolve(c.toDataURL('image/jpeg', 0.6))
        } else reject(new Error('canvas 不可用'))
      } catch (e) {
        reject(e)
      } finally {
        cleanup()
      }
    }
    v.onerror = () => {
      cleanup()
      reject(new Error('视频加载失败'))
    }
  })
}

/** 只为「进入视口」的视频占位块抓帧（IntersectionObserver 懒触发）：
 *  一页最多 60 个视频，全量抓会浪费带宽且逼近视频接口 300 次/分限流 */
let posterObserver: IntersectionObserver | null = null
function bindVideoPosters() {
  posterObserver?.disconnect()
  const cells = document.querySelectorAll<HTMLElement>('.browser-video-ph')
  if (!cells.length) return
  posterObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue
      const el = e.target as HTMLElement
      posterObserver?.unobserve(el)
      const rel = el.dataset.rel
      if (!rel || posterCache.has(rel)) continue
      posterCache.set(rel, '') // 先占位，防止并发重复发起
      void captureVideoFrame(resourceVideoUrl(rel))
        .then((data) => {
          posterCache.set(rel, data)
          videoPosters.value = { ...videoPosters.value, [rel]: data }
        })
        .catch(() => {
          /* 失败保持类型文字占位 */
        })
    }
  })
  cells.forEach((el) => posterObserver?.observe(el))
}
watch([browserVisible, browserPaged], () => {
  void nextTick(bindVideoPosters)
  // 当前页图片进入受控加载队列（并发 6，避免瞬间打满预览限流）
  enqueueThumbs(
    browserPaged.value.filter((f) => f.category === 'image').map((f) => f.rel_path),
  )
})
onBeforeUnmount(() => {
  posterObserver?.disconnect()
  if (thumbTimer) clearTimeout(thumbTimer)
  for (const u of thumbBlobUrls) URL.revokeObjectURL(u)
})

/** 浏览抽屉卡片的悬浮提示：把行内放不下的元信息（修改时间 / 完整路径）集中给出 */
function cellTitle(f: ResourceFile): string {
  const parts = [f.name]
  if (f.width && f.height) parts.push(`${f.width}×${f.height}`)
  parts.push(formatSize(Number(f.size)))
  if (f.mtime) parts.push(formatMinuteTime(f.mtime))
  parts.push(f.rel_path)
  return parts.join(' · ')
}

/** 首帧作为占位块背景：未抓到前保持类型文字 */
function posterStyle(rel: string): Record<string, string> {
  const data = videoPosters.value[rel]
  return data
    ? { backgroundImage: `url(${data})`, backgroundSize: 'cover', backgroundPosition: 'center' }
    : {}
}

// ===== 勾选批量删除（业界网盘通行做法：逐条勾选 + 全选 + 批量操作） =====
// 文件选中集合以 rel_path 为键：目录模式下跨目录累积勾选、全局模式与筛选切换不丢勾选
// （Gmail / 百度网盘同策略——选中跟条目走，不跟视图走）；列表刷新后按现存文件裁剪，
// 已被删除或不再存在的路径自动移出选中，避免提交无效路径。
const selectedPaths = ref<Set<string>>(new Set())
const selectedCount = computed(() => selectedPaths.value.size)

// 目录选中集合以目录名为键（目录名 = 相对路径首段，全库唯一），供目录级批量删除
const selectedDirs = ref<Set<string>>(new Set())
const selectedDirCount = computed(() => selectedDirs.value.size)
// 已选总数（文件 + 目录），工具栏「删除所选」计数用
const selectedTotal = computed(() => selectedCount.value + selectedDirCount.value)

/** 当前作用域文件（目录模式 = 展开目录的筛选结果；全局模式 = 全部命中文件），供表格表头全选用 */
const scopeFiles = computed<ResourceFile[]>(() =>
  globalMode.value ? globalFiles.value : filteredFiles.value,
)

const allScopeSelected = computed(
  () =>
    scopeFiles.value.length > 0 &&
    scopeFiles.value.every((f) => selectedPaths.value.has(f.rel_path)),
)
// 半选态：作用域内有勾选但未全勾（表头复选框的 indeterminate）
const someScopeSelected = computed(() =>
  scopeFiles.value.some((f) => selectedPaths.value.has(f.rel_path)),
)

// 目录模式「全选当前结果」作用域 = 当前页目录（与目录列表分页一致，不跨页隐式全选）
const folderPageAllSelected = computed(
  () =>
    pagedFolders.value.length > 0 &&
    pagedFolders.value.every((i) => selectedDirs.value.has(i.name)),
)
const folderPageSomeSelected = computed(() =>
  pagedFolders.value.some((i) => selectedDirs.value.has(i.name)),
)

// 全局模式「全选当前结果」作用域 = 全部命中文件（含未渲染的后续页，一次请求可整批删）
const allGlobalSelected = computed(
  () =>
    globalFiles.value.length > 0 &&
    globalFiles.value.every((f) => selectedPaths.value.has(f.rel_path)),
)
const someGlobalSelected = computed(() =>
  globalFiles.value.some((f) => selectedPaths.value.has(f.rel_path)),
)

/** 工具栏「全选当前结果」的可用性与状态：全局模式作用于全部命中文件；目录模式作用于当前页目录 */
const canSelectAll = computed(() =>
  globalMode.value ? globalFiles.value.length > 0 : pagedFolders.value.length > 0,
)
const allToolbarSelected = computed(() =>
  globalMode.value ? allGlobalSelected.value : folderPageAllSelected.value,
)
const someToolbarSelected = computed(() =>
  globalMode.value ? someGlobalSelected.value : folderPageSomeSelected.value,
)

function isSelected(rel: string): boolean {
  return selectedPaths.value.has(rel)
}

function toggleSelect(rel: string) {
  const next = new Set(selectedPaths.value)
  if (next.has(rel)) next.delete(rel)
  else next.add(rel)
  selectedPaths.value = next
}

function isSelectedDir(name: string): boolean {
  return selectedDirs.value.has(name)
}

function toggleSelectDir(name: string) {
  const next = new Set(selectedDirs.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  selectedDirs.value = next
}

/** 全选 / 取消全选当前作用域（幂等：只增删作用域内的条目，不影响范围外的既有勾选） */
function toggleSelectAll() {
  if (globalMode.value) {
    const next = new Set(selectedPaths.value)
    if (allGlobalSelected.value) {
      for (const f of globalFiles.value) next.delete(f.rel_path)
    } else {
      for (const f of globalFiles.value) next.add(f.rel_path)
    }
    selectedPaths.value = next
  } else {
    const next = new Set(selectedDirs.value)
    if (folderPageAllSelected.value) {
      for (const i of pagedFolders.value) next.delete(i.name)
    } else {
      for (const i of pagedFolders.value) next.add(i.name)
    }
    selectedDirs.value = next
  }
}

function clearSelection() {
  selectedPaths.value = new Set()
  selectedDirs.value = new Set()
}

// 资源列表变化后裁剪选中集合（删除 / 后台文件变动后，选中项可能已不存在）
watch(allFiles, (files) => {
  if (!selectedPaths.value.size) return
  const alive = new Set(files.map((f) => f.rel_path))
  const next = new Set([...selectedPaths.value].filter((p) => alive.has(p)))
  if (next.size !== selectedPaths.value.size) selectedPaths.value = next
})
watch(sortedFolders, (folders) => {
  if (!selectedDirs.value.size) return
  const alive = new Set(folders.map((i) => i.name))
  const next = new Set([...selectedDirs.value].filter((n) => alive.has(n)))
  if (next.size !== selectedDirs.value.size) selectedDirs.value = next
})

// ===== 删除（软删除：移入回收站，保留期内可恢复）=====
/** 删除确认（三选）：确认=直接删除（不可恢复），取消按钮=移入回收站，右上角 X=放弃。
 *  EP 的 confirm 在 distinguishCancelAndClose 下只有 confirm 会 resolve；
 *  cancel 按钮固定 reject('cancel')，X/ESC/遮罩 reject('close')——必须按 reject 原因
 *  区分，不能把所有 rejection 当作「放弃」（否则「移入回收站」永远不生效）。 */
function askDeleteAction(title: string, message: string): Promise<'confirm' | 'cancel' | 'close'> {
  return ElMessageBox.confirm(message, title, {
    type: 'warning',
    distinguishCancelAndClose: true,
    confirmButtonText: '直接删除',
    cancelButtonText: '移入回收站',
  }).then(
    () => 'confirm' as const,
    (reason) => (reason === 'cancel' ? ('cancel' as const) : ('close' as const)),
  )
}

async function removeFile(file: ResourceFile) {
  const action = await askDeleteAction(
    '删除确认',
    `删除文件「${file.name}」（${formatSize(Number(file.size))}）——「直接删除」不可恢复；「移入回收站」可保留 ${trashKeepDays.value} 天。`,
  )
  if (action === 'close') return
  const permanent = action === 'confirm'
  try {
    await api.deleteResource(file.rel_path, false, permanent)
    ElMessage.success(permanent ? '已直接删除' : `已移入回收站，${trashKeepDays.value} 天内可恢复`)
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

async function removeFolder(item: ResourceItem) {
  const action = await askDeleteAction(
    '删除目录确认',
    `删除目录「${item.name}」及其 ${item.file_count} 个文件（${formatSize(item.total_size)}）——「直接删除」不可恢复；「移入回收站」可保留 ${trashKeepDays.value} 天。`,
  )
  if (action === 'close') return
  const permanent = action === 'confirm'
  try {
    await api.deleteResource(item.name, true, permanent)
    ElMessage.success(permanent ? '目录已直接删除' : '目录已移入回收站')
    if (active.value === item.name) active.value = ''
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

/** 批量删除已勾选的条目：文件勾选跨目录累积、目录勾选（整目录含其下全部文件），
 *  混合后一次请求整批提交。三选确认（直接删除不可恢复 / 移入回收站 / 放弃），
 *  复用 askDeleteAction。 */
async function batchRemove() {
  const files = allFiles.value.filter((f) => selectedPaths.value.has(f.rel_path))
  const dirs = sortedFolders.value.filter((i) => selectedDirs.value.has(i.name))
  if (!files.length && !dirs.length) {
    ElMessage.warning('请先勾选要删除的文件或目录（列表勾选 / 全选当前结果）')
    return
  }
  // 确认文案按实际勾选构成拼接（仅文件 / 仅目录 / 混合三种形态），明确告知删除规模
  const fileSize = files.reduce((s, f) => s + Number(f.size), 0)
  const dirSize = dirs.reduce((s, i) => s + i.total_size, 0)
  const parts: string[] = []
  if (files.length) parts.push(`${files.length} 个文件（${formatSize(fileSize)}）`)
  if (dirs.length) {
    const dirFileCount = dirs.reduce((s, i) => s + i.file_count, 0)
    parts.push(`${dirs.length} 个目录（含 ${dirFileCount} 个文件，${formatSize(dirSize)}）`)
  }
  const action = await askDeleteAction(
    '批量删除确认',
    `将删除已勾选的 ${parts.join('、')}——「直接删除」不可恢复；「移入回收站」可保留 ${trashKeepDays.value} 天。`,
  )
  if (action === 'close') return
  const permanent = action === 'confirm'
  try {
    const r = await api.batchDeleteResource(
      [
        ...files.map((f) => ({ path: f.rel_path, is_dir: false })),
        ...dirs.map((i) => ({ path: i.name, is_dir: true })),
      ],
      permanent,
    )
    if (r.failed.length) {
      ElMessage.warning(`已删除 ${r.deleted} 个，${r.failed.length} 个失败（可能被占用）`)
    } else {
      ElMessage.success(`已${permanent ? '直接删除' : '移入回收站'} ${r.deleted} 个`)
    }
    clearSelection()
    await load()
    await loadTrash()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`批量删除失败: ${(e as Error).message}`)
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

/** 目录行元素表（供「Top10 目录 / 命中目录」点击后滚动定位） */
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

// B6 容量洞察：类型分布（按大小）、Top10 目录、Top10 大文件
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

// Top10 目录：与「Top10 大文件」对标对齐（同为 Top10、同为按大小取前 10）
const topFolders = computed<ResourceItem[]>(() =>
  [...(data.value?.items ?? [])].sort((a, b) => b.total_size - a.total_size).slice(0, 10),
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
    // 勾选列：el-table-v2 无内建 selection，自定义复选框；表头为全选（含半选态）
    key: 'selection',
    title: '',
    width: 44,
    align: 'center',
    cellRenderer: ({ rowData }) =>
      h(ElCheckbox, {
        modelValue: selectedPaths.value.has((rowData as ResourceFile).rel_path),
        onChange: () => toggleSelect((rowData as ResourceFile).rel_path),
      }),
    headerCellRenderer: () =>
      h(ElCheckbox, {
        modelValue: allScopeSelected.value,
        indeterminate: someScopeSelected.value && !allScopeSelected.value,
        onChange: () => toggleSelectAll(),
      }),
  },
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
      // 统一查看入口：按类型自动分派（大图查看器 / 播放弹窗 / 文本 / 种子信息 / 系统打开）
      btns.unshift(
        h(
          ElButton,
          { link: true, type: 'primary', onClick: () => openResource(rowData, activeFiles.value) },
          () => '查看',
        ),
      )
      // 所有类型都可用系统默认程序打开（压缩包、种子等本地处理更直接）
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
  // 从首页资产卡「按类型」下钻进入时，按路由 query.type 强制切换类型筛选
  // （显式导航优先于会话记忆，保证「点哪个类型就看到哪类文件」，下钻口径自洽）
  const q = route.query.type
  if (q != null && (CATEGORY_ORDER as ReadonlyArray<string>).includes(String(q))) {
    typeFilter.value = String(q) as typeof typeFilter.value
  }
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
        <div class="insight-title">Top10 目录</div>
        <div
          v-for="f in topFolders"
          :key="f.name"
          class="insight-line"
          :title="`${f.name}（点击行展开并定位到列表）`"
          @click="clearFiltersAndExpand(f.name)"
        >
          <span class="insight-name">{{ f.name }}</span>
          <!-- 与 Top10 大文件行平行：元信息（文件数 · 大小）+ 类型化操作 -->
          <span class="text-muted insight-meta">{{ f.file_count }} 个文件 · {{ formatSize(f.total_size) }}</span>
          <span class="insight-ops">
            <!-- 浏览与列表目录头的「浏览」为同一功能（openBrowser 抽屉网格）；
                 移动端三个按钮挤掉目录名，收敛为一个「更多」菜单（与目录头同一模式） -->
            <template v-if="!isMobile">
              <el-button link type="primary" size="small" @click.stop="openBrowser(f)">
                浏览
              </el-button>
              <el-button
                v-if="sourceOf(f.name)?.matched"
                link
                type="primary"
                size="small"
                @click.stop="openSourceUrl(f.name)"
              >
                原帖
              </el-button>
              <el-button link type="danger" size="small" @click.stop="removeFolder(f)">
                删除
              </el-button>
            </template>
            <el-dropdown v-else trigger="click">
              <el-button link size="small" class="insight-more" @click.stop>更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="openBrowser(f)">浏览</el-dropdown-item>
                  <el-dropdown-item
                    v-if="sourceOf(f.name)?.matched"
                    @click="openSourceUrl(f.name)"
                  >
                    原帖
                  </el-dropdown-item>
                  <el-dropdown-item divided @click="removeFolder(f)">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </span>
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
            <!-- 统一查看入口：按类型自动分派；
                 移动端收敛为一个「更多」菜单（名称占满行宽） -->
            <template v-if="!isMobile">
              <el-button link type="primary" size="small" @click.stop="openResource(f, topFiles)">
                查看
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
            </template>
            <el-dropdown v-else trigger="click">
              <el-button link size="small" class="insight-more" @click.stop>更多</el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="openResource(f, topFiles)">查看</el-dropdown-item>
                  <el-dropdown-item
                    v-if="sourceOf(fileDir(f))?.matched"
                    @click="openFileSource(f)"
                  >
                    原帖
                  </el-dropdown-item>
                  <el-dropdown-item divided @click="removeFile(f)">删除</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
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
            @change="resetFolderPage"
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
            <!-- 全选当前作用域：全局模式 = 全部命中文件；目录模式 = 当前页目录 -->
            <el-button :disabled="!canSelectAll" @click="toggleSelectAll">
              {{ allToolbarSelected ? '取消全选' : '全选当前结果' }}
            </el-button>
            <!-- 删除所选：文件与目录勾选混合提交，未勾选时禁用；三选确认防误删 -->
            <el-button type="danger" plain :disabled="!selectedTotal" @click="batchRemove">
              删除所选<template v-if="selectedTotal">（{{ selectedTotal }}）</template>
            </el-button>
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
          <!-- 平铺命中文件列表 + 分页（单行形态：名称 + 类型标签 + 大小 + 所属目录一行铺开不换行，
               信息密度与 Top10 行/目录头一致）；「浏览」= 打开所属目录浏览抽屉（统一浏览入口）；
               勾选框点击不触发行内操作，与桌面表格勾选列同一选中集合 -->
          <div class="file-lines">
            <div v-for="f in globalPaged" :key="f.rel_path" class="file-line">
              <el-checkbox
                class="fc-check"
                :model-value="isSelected(f.rel_path)"
                @change="toggleSelect(f.rel_path)"
                @click.stop
              />
              <div class="fl-name" :title="f.name">
                <i
                  class="fc-dot"
                  :style="{ background: categoryColors[f.category] ?? '#c0c4cc' }"
                ></i>
                <span class="fl-name-text">{{ f.name }}</span>
              </div>
              <!-- 不再显示类型标签：筛选模式下类型即当前筛选条件（如筛选视频，行内无需再标「视频」），
                   色点已按类型着色保留最低限度的类型视觉线索 -->
              <span class="fl-size text-muted">{{ formatSize(Number(f.size)) }}</span>
              <!-- 修改时间：填充行内空白的有用元信息（窄屏隐藏）；0/缺省 = stat 失败不展示 -->
              <span v-if="f.mtime" class="fl-mtime text-muted">
                {{ formatMinuteTime(f.mtime) }}
              </span>
              <!-- 所属目录/子路径：平铺后目录上下文收进行内元信息（窄屏隐藏，悬停可见完整路径） -->
              <span class="fl-dir text-muted" :title="f.rel_path">
                {{ fileParentPath(f) }}
              </span>
              <div class="fc-ops">
                <!-- 桌面端：四个操作全部默认显示，与元信息紧挨排列 -->
                <template v-if="!isMobile">
                  <el-button size="small" type="primary" link @click="openResource(f, globalPaged)">
                    查看
                  </el-button>
                  <el-button size="small" type="primary" link @click="openBrowserAll()">
                    浏览
                  </el-button>
                  <el-button size="small" link @click="openFileLocal(f)">
                    打开
                  </el-button>
                  <el-button size="small" link @click="openShare(f)">
                    分享
                  </el-button>
                  <el-button size="small" type="danger" link @click="removeFile(f)">
                    删除
                  </el-button>
                </template>
                <!-- 移动端：位置不够时至少直显「浏览」（跨目录浏览入口），其余收进「更多」菜单 -->
                <template v-else>
                  <el-button size="small" type="primary" link @click="openBrowserAll()">
                    浏览
                  </el-button>
                  <el-dropdown trigger="click">
                    <el-button size="small" link class="fl-more">更多</el-button>
                    <template #dropdown>
                      <el-dropdown-menu>
                        <el-dropdown-item @click="openResource(f, globalPaged)">查看</el-dropdown-item>
                        <el-dropdown-item @click="openFileLocal(f)">打开</el-dropdown-item>
                        <el-dropdown-item @click="openShare(f)">分享</el-dropdown-item>
                        <el-dropdown-item divided @click="removeFile(f)">删除</el-dropdown-item>
                      </el-dropdown-menu>
                    </template>
                  </el-dropdown>
                </template>
              </div>
            </div>
          </div>
          <el-empty v-if="!globalFiles.length" :image-size="64" description="无匹配文件" />
          <!-- 命中结果分页（与目录列表分页同一模式）；不提供「显示全部」，数千卡片一次渲染会卡死 -->
          <div class="folder-pager">
            <el-pagination
              layout="total, sizes, prev, pager, next, jumper"
              :total="globalFiles.length"
              :page-size="globalPageSize"
              :current-page="globalPage"
              :page-sizes="[30, 50, 100]"
              small
              background
              @current-change="onGlobalPage"
              @size-change="onGlobalPageSize"
            />
          </div>
        </template>

        <!-- 目录模式（默认）：目录折叠列表 -->
        <template v-else>
          <div
            v-for="item in pagedFolders"
            :key="item.name"
            class="folder"
            :ref="(el) => setFolderRef(el, item.name)"
          >
            <div class="folder-head" @click="toggle(item.name)">
              <!-- 勾选框：目录级批量删除；点击不触发展开/折叠 -->
              <el-checkbox
                class="folder-check"
                :model-value="isSelectedDir(item.name)"
                @change="toggleSelectDir(item.name)"
                @click.stop
              />
              <el-icon class="folder-arrow" :class="{ open: active === item.name }">
                <ArrowRight />
              </el-icon>
              <el-icon class="folder-icon"><Folder /></el-icon>
              <div class="folder-main">
                <!-- 单行形态（参考 Top10 行）：名称省略 + 元信息/摘要/标记，不再分三行，
                     145+ 个目录的列表更紧凑；来源文字行并入操作区（原帖/看帖子按钮） -->
                <div class="folder-main">
                  <span class="folder-name" :title="item.name">{{ item.name }}</span>
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
                  <!-- 空目录（0 个文件，多为下载失败/取消残留）与空壳目录（仅磁力/云盘清单、
                       无媒体文件）互斥展示：isMedialess 的 every 对空数组恒真，须先判空目录 -->
                  <el-tag v-if="item.file_count === 0" size="small" type="info">空目录</el-tag>
                  <el-tag v-else-if="isMedialess(item)" size="small" type="warning">未下载到媒体</el-tag>
                  <!-- B7 下载任务关联标记已按用户要求移除（2026-09-08） -->
                </div>
              </div>
              <!-- 桌面端：操作平铺（横向空间充足）；
                   「浏览」= 不展开目录直接浏览该目录全部资源（统一查看入口）；
                   「原帖 / 看帖子」= 来源回溯（B1），仅在命中来源时显示 -->
              <template v-if="!isMobile">
                <el-button link type="primary" class="del-btn" @click.stop="openBrowser(item)">
                  浏览
                </el-button>
                <el-button
                  v-if="sourceOf(item.name)?.matched"
                  link
                  type="primary"
                  class="del-btn"
                  @click.stop="openSourceUrl(item.name)"
                >
                  原帖
                </el-button>
                <el-button link type="danger" class="del-btn" @click.stop="removeFolder(item)">
                  删除目录
                </el-button>
              </template>
              <!-- 移动端：操作收进「更多」菜单（网盘移动端通行做法），把宽度让给目录名 -->
              <el-dropdown v-else trigger="click">
                <el-button link class="folder-more" @click.stop>更多</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="openBrowser(item)">浏览</el-dropdown-item>
                    <el-dropdown-item
                      v-if="sourceOf(item.name)?.matched"
                      @click="openSourceUrl(item.name)"
                    >
                      原帖
                    </el-dropdown-item>
                    <el-dropdown-item divided @click="removeFolder(item)">删除目录</el-dropdown-item>
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
                      <!-- 勾选框：点击不触发卡片「查看」，与桌面表格勾选列同一选中集合 -->
                      <el-checkbox
                        class="fc-check"
                        :model-value="isSelected(f.rel_path)"
                        @change="toggleSelect(f.rel_path)"
                        @click.stop
                      />
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
                        <!-- 统一查看入口：按类型自动分派 -->
                        <el-button size="small" type="primary" link @click="openResource(f, activeFiles)">
                          查看
                        </el-button>
                        <el-dropdown trigger="click">
                          <el-button size="small" link>更多</el-button>
                          <template #dropdown>
                            <el-dropdown-menu>
                              <el-dropdown-item @click="openFileLocal(f)">打开</el-dropdown-item>
                              <el-dropdown-item @click="openShare(f)">分享</el-dropdown-item>
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
          <!-- 目录分页：145+ 个目录不一次性铺开，按页展示（业界网盘做法） -->
          <div class="folder-pager">
            <el-pagination
              v-if="folderPageSize !== -1"
              layout="total, sizes, prev, pager, next, jumper"
              :total="sortedFolders.length"
              :page-size="folderPageSize"
              :current-page="folderPage"
              :page-sizes="[30, 50, 100]"
              small
              background
              @current-change="onFolderPage"
              @size-change="onFolderPageSize"
            />
            <el-button
              v-if="folderPageSize !== -1"
              text
              type="primary"
              size="small"
              class="pager-all"
              @click="showAllFolders"
            >
              显示全部 {{ sortedFolders.length }}
            </el-button>
            <el-button
              v-else
              text
              type="primary"
              size="small"
              @click="resetToPaged"
            >
              分页显示
            </el-button>
          </div>
        </template>
      </template>
    </div>

    <!-- 目录资源浏览抽屉：不展开目录即可浏览该目录全部资源。
         图片缩略图走「受控并发队列 + 429 退避重试」（不再让 el-image 直接并发拉接口，
         一页几十张会撞预览限流而被显示成「加载失败」）；视频/文本/种子/其他显示类型占位块。
         点击任意卡片走统一查看入口 openResource 打开对应查看器（叠在抽屉之上）。 -->
    <!-- 手机端全屏：size 固定 720px 会超出手机视口，内容被截断错位 -->
    <el-drawer
      v-model="browserVisible"
      :title="browserTitle"
      :size="isMobile ? '100%' : '720px'"
    >
      <!-- folder = 单目录浏览；global = 跨目录浏览全部命中资源（与列表同一筛选口径） -->
      <template v-if="browserMode === 'global' || browserFolder">
        <div class="browser-summary text-muted">
          <template v-if="browserMode === 'global'">
            共 {{ browserFiles.length }} 个命中文件（跨全部目录，与列表同一筛选口径）
          </template>
          <template v-else>
            共 {{ browserFiles.length }} 个文件
            <template v-for="(m, i) in browserMix" :key="m.label">
              <span v-if="i">·</span>
              <span>{{ m.label }} {{ m.count }}</span>
            </template>
            <!-- 图片汇总：张数与占用（容量感） -->
            <template v-if="browserImageStats.count">
              <span>·</span>
              <span>图片 {{ browserImageStats.count }} 张 / {{ formatSize(browserImageStats.size) }}</span>
            </template>
          </template>
        </div>
        <!-- 类型筛选 + 名称过滤 + 排序：资源过多时先收敛范围再浏览 -->
        <div class="browser-toolbar">
          <el-segmented v-model="browserTypeFilter" :options="categoryOptions" size="small" />
          <el-input
            v-model="browserKeyword"
            class="browser-search"
            placeholder="按文件名过滤"
            clearable
            size="small"
            :prefix-icon="'Search'"
          />
          <el-select v-model="browserSort" size="small" class="browser-sort">
            <el-option
              v-for="o in browserSortOptions"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="!browserFiltered.length"
            @click="openShareBrowser"
          >
            分享此目录
          </el-button>
        </div>
        <div class="browser-grid">
          <div
            v-for="f in browserPaged"
            :key="f.rel_path"
            class="browser-cell"
            :title="cellTitle(f)"
            @click="openResource(f, browserFiltered)"
          >
            <!-- 图片：受控加载成功后用 blob URL 展示；其余状态显示对应占位 -->
            <el-image
              v-if="f.category === 'image' && thumbState(f.rel_path).status === 'ok'"
              :src="thumbState(f.rel_path).url"
              fit="cover"
              class="browser-thumb"
            />
            <div
              v-else-if="f.category === 'image'"
              class="browser-ph"
              :class="{ 'ph-warn': thumbState(f.rel_path).status === 'retrying' }"
            >
              <template v-if="thumbState(f.rel_path).status === 'retrying'">
                <span class="ph-text">限流中，稍后自动重试</span>
              </template>
              <template v-else-if="thumbState(f.rel_path).status === 'error'">
                <span class="ph-text">加载失败</span>
                <div class="ph-ops">
                  <el-button link type="primary" size="small" @click.stop="retryThumb(f.rel_path)">
                    重试
                  </el-button>
                  <el-button link size="small" @click.stop="openFileLocal(f)">打开</el-button>
                </div>
              </template>
              <template v-else>
                <span class="ph-text">加载中</span>
              </template>
            </div>
            <!-- 视频：显示抓到的首帧预览图；抓到前（或失败）显示类型文字 -->
            <div
              v-else-if="f.category === 'video'"
              class="browser-ph ph-video browser-video-ph"
              :data-rel="f.rel_path"
              :style="posterStyle(f.rel_path)"
            >
              <span v-if="!videoPosters[f.rel_path]">{{ categoryLabel(f.category) }}</span>
            </div>
            <div v-else class="browser-ph" :class="'ph-' + f.category">
              {{ categoryLabel(f.category) }}
            </div>
            <div class="browser-name">{{ f.name }}</div>
            <div class="text-muted browser-size">
              <!-- 元信息：像素尺寸 · 大小（真实格式标签已去除，二者用间隔点隔开，悬浮 title 另有修改时间与完整路径） -->
              <span v-if="f.width && f.height">{{ f.width }}×{{ f.height }}</span>
              <span v-if="f.width && f.height" class="sep">·</span>
              <span>{{ formatSize(Number(f.size)) }}</span>
            </div>
            <!-- 文件卡点击走统一查看入口；「分享」用 @click.stop 阻断冒泡，避免误开查看器 -->
            <div class="browser-ops">
              <el-button link type="primary" size="small" @click.stop="openShare(f)">
                分享
              </el-button>
            </div>
          </div>
        </div>
        <el-empty
          v-if="!browserFiltered.length"
          :image-size="64"
          description="无匹配文件"
        />
        <!-- 前端切片分页：仅超过一页时显示（与任务列表/目录列表同一模式） -->
        <el-pagination
          v-if="browserFiltered.length > browserPageSize"
          v-model:current-page="browserPage"
          :page-size="browserPageSize"
          :total="browserFiltered.length"
          layout="total, prev, pager, next"
          size="small"
          class="browser-pager"
        />
      </template>
    </el-drawer>

    <!-- B5 图片大图预览（点击「预览」打开，Esc / 关闭按钮退出） -->
    <el-image-viewer
      v-if="viewerVisible"
      :url-list="viewerUrls"
      :initial-index="viewerIndex"
      teleported
      @close="closeViewer"
    />

    <!-- 视频播放弹窗：同一列表内可连续播放，播放结束自动下一个 -->
    <!-- 居中 + 最大化展示：移动端全屏，桌面端 92% 宽并垂直居中（align-center） -->
    <el-dialog
      v-model="videoVisible"
      :title="videoTitle"
      :width="isMobile ? '100%' : '92%'"
      :fullscreen="isMobile"
      align-center
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
    <el-dialog
      v-model="textVisible"
      :title="`查看文本 · ${textName}`"
      :width="isMobile ? '92%' : '60%'"
      top="8vh"
    >
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
    <el-dialog
      v-model="torrentVisible"
      title="种子信息"
      :width="isMobile ? '92%' : '60%'"
      top="8vh"
    >
      <el-skeleton v-if="torrentLoading" :rows="6" animated />
      <template v-else-if="torrentData">
        <div class="torrent-head">
          <div class="torrent-name" :title="torrentData.name">{{ torrentData.name }}</div>
          <div class="text-muted">
            {{ torrentData.file_count }} 个文件 · {{ formatSize(torrentData.total_size) }}
          </div>
        </div>
        <div class="magnet-row">
          <el-input
            ref="magnetInputRef"
            :model-value="torrentData.magnet"
            readonly
            size="small"
            class="magnet-input"
          />
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

    <!-- 分享：可单文件，也可一次性分享整个目录 / 多个文件（独立 8090 分享服务，不暴露前端看板） -->
    <el-dialog
      v-model="shareVisible"
      :title="shareTitle"
      :width="isMobile ? '92%' : '480px'"
      top="8vh"
    >
      <div v-if="shareTargets.length" class="share-body">
        <!-- 自由勾选模式：复选清单 + 全选/全不选 -->
        <template v-if="shareSelectable">
          <div class="share-pick-bar">
            <span class="text-muted">已选 {{ shareSelected.size }} / {{ shareTargets.length }}</span>
            <div class="share-pick-ops">
              <el-button size="small" link type="primary" @click="pickAll(true)">全选</el-button>
              <el-button size="small" link @click="pickAll(false)">全不选</el-button>
            </div>
          </div>
          <div class="share-pick-list">
            <label v-for="f in shareTargets" :key="f.rel_path" class="share-pick-item">
              <el-checkbox
                :model-value="shareSelected.has(f.rel_path)"
                @change="(v) => togglePick(f.rel_path, v)"
              />
              <span class="share-pick-name" :title="f.rel_path">{{ f.name }}</span>
            </label>
          </div>
        </template>
        <!-- 普通模式：只读展示待分享文件 -->
        <template v-else>
          <div class="share-name-list">
            <div
              v-for="f in shareTargets.slice(0, 50)"
              :key="f.rel_path"
              class="share-name"
              :title="f.rel_path"
            >{{ f.name }}</div>
            <div v-if="shareTargets.length > 50" class="text-muted">
              等共 {{ shareTargets.length }} 个文件
            </div>
          </div>
        </template>
        <div class="text-muted share-sub">链接有效期</div>
        <el-select v-model="shareTtl" size="small" style="width: 100%">
          <el-option label="1 小时" value="1h" />
          <el-option label="24 小时" value="24h" />
          <el-option label="7 天" value="7d" />
          <el-option label="30 天" value="30d" />
        </el-select>
        <el-button
          class="share-gen"
          type="primary"
          size="small"
          :loading="shareLoading"
          @click="generateShare"
        >
          生成并复制链接
        </el-button>
        <template v-if="shareUrl">
          <el-input :model-value="shareUrl" readonly size="small" class="share-url">
            <template #append>
              <el-button @click="copyShare">复制</el-button>
            </template>
          </el-input>
          <div class="text-muted share-tip">
            该链接由独立端口提供，打开后只显示所选文件、不会展示本系统的管理界面。
          </div>
        </template>
      </div>
    </el-dialog>

    <!-- 回收站：软删除项，保留期内可恢复，也可彻底删除 -->
    <!-- 手机端全屏：size 固定 520px 会超出手机视口，列表被截断错位 -->
    <el-drawer v-model="trashVisible" title="回收站" :size="isMobile ? '100%' : '520px'">
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

/* 分享对话框 */
.share-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.share-name {
  font-weight: 600;
  font-size: 14px;
  word-break: break-all;
}
.share-name-list {
  max-height: 220px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 6px;
  background: #f5f7fa;
  border-radius: 8px;
}
.share-name-list .share-name {
  font-weight: 400;
  font-size: 13px;
}
/* 自由勾选清单（分享此目录） */
.share-pick-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.share-pick-ops {
  display: flex;
  gap: 8px;
}
.share-pick-list {
  max-height: 240px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  background: #f5f7fa;
  border-radius: 8px;
}
.share-pick-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 4px;
  border-radius: 6px;
  cursor: pointer;
}
.share-pick-item:hover {
  background: #ecf5ff;
}
.share-pick-name {
  font-size: 13px;
  word-break: break-all;
}
.share-sub {
  font-size: 12px;
  margin-top: 4px;
}
.share-gen {
  align-self: flex-start;
}
.share-url {
  margin-top: 4px;
}
.share-tip {
  font-size: 12px;
  line-height: 1.5;
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

/* 卡片勾选框：固定不收缩；高度不随 .file-card 的对齐拉伸 */
.fc-check {
  flex-shrink: 0;
  height: auto;
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

  /* 容量洞察卡：两个 Top10 明细块不再硬挤两列——窄屏下「名称 + 元信息 + 操作」
     在半幅宽度内必然放不下而错位，改为上下堆叠各占整行 */
  /* 目录头相关规则见「样式表末尾」的移动端块：它们需要覆盖 .folder-name / .folder-head
     等基础样式，同特异性下必须写在基础规则之后才会生效。
     容量洞察卡（.insight-card / .insight-line）的移动端降级同理，见该块末尾——
     此处写在基础规则（1741 行 2 列网格）之前会被后者覆盖，media query 不提升特异性。 */
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

/* Top10 目录行的元信息（N 个文件 · 大小）：不换行不压缩，保证与大文件行平行等高 */
.insight-meta {
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

/* 全局模式平铺单行文件行：名称 + 类型标签 + 大小 + 所属目录一行铺开，均不换行 */
.file-lines {
  display: flex;
  flex-direction: column;
}

.file-line {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 40px;
  padding: 2px 6px;
  border-radius: 6px;
}

.file-line:hover {
  background: #f6f8fc;
}

/* 文件名收缩自适应（不占满行宽）：名称与后面的类型/大小/目录紧挨排列，
   超长时省略收缩；行尾空白统一留给右侧操作区 */
.fl-name {
  flex: 0 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.fl-name-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 大小不换行不压缩 */
.fl-size {
  flex-shrink: 0;
  font-size: 12px;
}

/* 所属目录/子路径：放宽到 45% 并随内容伸展（含子目录的完整父路径），
   超长省略，悬停 title 可见完整路径 */
.fl-dir {
  flex: 0 1 auto;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}

/* 移动端「更多」按钮：紧凑行高，不撑高单行 */
.fl-more {
  min-height: 24px;
}

/* 单行内信息簇（名称+大小+时间+目录）依次紧挨排列，操作区右对齐（吸收剩余空白） */
.file-line .fc-ops {
  margin-left: auto;
}

/* 修改时间：不换行不压缩 */
.fl-mtime {
  flex-shrink: 0;
  font-size: 12px;
}

/* 目录头勾选框：固定不收缩，点击不触发头部的展开/折叠 */
.folder-check {
  flex-shrink: 0;
  height: auto;
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

/* 目录分页条：与目录卡片同宽，居中并错开上方留白 */
.folder-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 16px 0 4px;
}

.folder-pager .pager-all {
  margin-left: 4px;
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

/* 单行形态（参考 Top10 行）：名称省略收缩，元信息/摘要/标记不换行不压缩 */
.folder-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}

.folder-name {
  flex: 0 1 auto;
  min-width: 0;
  font-weight: 600;
  color: #1f2d3d;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-meta {
  flex-shrink: 0;
  font-size: 12px;
}

.folder-mix {
  flex-shrink: 0;
  color: #606266;
}

.mix-dot {
  margin: 0 4px;
  color: #c0c4cc;
}

/* B7 下载任务关联标记已移除（2026-09-08） */

.del-btn {
  flex-shrink: 0;
  margin-left: 0;
}

/* 视频播放弹窗 */
.video-player {
  width: 100%;
  /* 最大化展示：高度放宽到 82vh（播放窗口本身已居中） */
  max-height: 82vh;
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
   本块覆盖上方的 .folder-head / .folder-name 等基础规则。
   CSS 同特异性下「后定义者胜」，写在前面会被基础样式整块覆盖而静默失效
   （曾放在样式表中部导致规则完全没生效），故固定在末尾并注明原因。 */
@media (max-width: 767px) {
  /* 目录头单行形态（与 Top10 行一致）：不换行，名称走省略号 */
  .folder-head {
    padding: 10px 12px;
  }

  /* 类型摘要（图 N · 视频 N）与元信息（文件数 · 大小 · 时间）在窄屏全部让位：
     移动网盘目录行通行做法是「目录名占满 + 更多菜单」，空间优先给名称 */
  .folder-mix,
  .folder-meta {
    display: none;
  }

  /* 名称占满剩余宽度（tag/更多按钮除外），最大化可读字数 */
  .folder-name {
    flex: 1 1 auto;
  }

  /* 「更多」按钮：保持 44px 触控高度，同时尽量窄，把宽度让给目录名 */
  .folder-more {
    min-height: 44px;
    min-width: 40px;
    padding: 0 4px;
  }

  /* Top10 行的「更多」菜单按钮：行高仅 22px 的紧凑列表，
     不能用 44px 触控高度（会把每行撑高近一倍、行间出现大片空白像空行）；
     保持按钮自然高度，宽度收窄把空间让给名称 */
  .insight-more {
    min-height: 24px;
    min-width: 32px;
    padding: 0 4px;
  }

  /* 容量洞察卡移动端降级：必须写在基础规则（2 列网格）之后才生效。
     两个 Top10 卡片各占满整行宽度上下堆叠，不再并排半宽。 */
  .insight-card {
    grid-template-columns: 1fr;
    gap: 16px;
  }

  /* Top10 行保持单行展示：名称超长走省略号收缩，元信息与操作按钮
     不换行不压缩（用户确认单行形态） */
  .insight-line > .text-muted,
  .insight-line > .insight-ops {
    flex-shrink: 0;
  }

  .insight-line .insight-ops {
    margin-left: auto;
  }

  /* 全局模式单行文件行：窄屏隐藏所属目录与修改时间（挤掉文件名），
     行内只留 名称 + 类型标签 + 大小 + 操作，全部单行不换行 */
  .fl-dir,
  .fl-mtime {
    display: none;
  }
}

/* 目录资源浏览抽屉：缩略图网格 + 点击查看 */
.browser-summary {
  margin-bottom: 10px;
  font-size: 12px;
}

.browser-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.browser-search {
  width: 170px;
}

/* 抽屉内排序下拉：与搜索框同一行，窄屏随 toolbar 换行 */
.browser-sort {
  width: 110px;
}

/* 缩略图占位块：限流/失败态「文案 + 操作行」纵向排列，避免溢出格子 */
.ph-text {
  font-size: 12px;
}

.ph-ops {
  display: flex;
  align-items: center;
  gap: 2px;
}

.ph-warn {
  background: #fdf6ec;
  color: #e6a23c;
}

.browser-pager {
  margin-top: 10px;
  justify-content: flex-end;
}

.browser-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
}

.browser-cell {
  cursor: pointer;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 6px;
  transition: box-shadow 0.15s;
}

.browser-cell:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
}

.browser-thumb {
  width: 100%;
  height: 100px;
  border-radius: 4px;
  display: block;
}

/* 非图片类型占位块 */
.browser-ph {
  height: 100px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  background: #f5f7fa;
  color: #909399;
  font-size: 13px;
}

.ph-video {
  background: #ecf5ff;
  color: #409eff;
}

.ph-torrent {
  background: #fdf6ec;
  color: #e6a23c;
}

.ph-text {
  background: #f0f9eb;
  color: #67c23a;
}

.ph-other {
  background: #f4f4f5;
  color: #909399;
}

.browser-name {
  margin-top: 6px;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.browser-size {
  font-size: 12px;
}
.browser-size .sep {
  margin: 0 6px;
  color: #c0c4cc;
}

/* 文件卡底部「分享」操作：与元信息之间留白，堆叠在卡片内底部 */
.browser-ops {
  margin-top: 4px;
  text-align: right;
}
</style>
