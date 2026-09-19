<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ElMessage,
  ElMessageBox,
  ElNotification,
} from 'element-plus'
import {
  api,
  formatDuration,
  isAborted,
  sseUrl,
  type DownloadFailureItem,
  type DownloadTaskDetail,
  type DownloadTaskSummary,
  type ReDownloadItem,
} from '../api'
import { Download, Search } from '@element-plus/icons-vue'
import { useAppStore } from '../stores/app'
import { briefList } from '../utils/text'
import { postOpenUrl, postPathOf } from '../utils/postUrl'
import { colorForFid } from '../utils/fidColor'

const app = useAppStore()
const route = useRoute()
const router = useRouter()
/** 与布局层同一断点（<768px）：窄屏下表格列宽固定必然横向溢出，
 *  操作列会被截掉大半，故小屏改为卡片列表（与 ResourcesView 同一模式）。 */
const isMobile = computed(() => app.isMobile)

const REFRESH_INTERVAL = 3000 // 轮询降级通道间隔（SSE 正常时不轮询）

const tasks = ref<DownloadTaskSummary[]>([])
const loading = ref(false)
const error = ref('') // 轮询失败提示信息

let pollTimer: number | null = null

// ---- 视图：下载任务（任务级）/ 失败缺口（帖级） ----
// 两者是**不同量纲**，混在一张列表里必然对不上数：
// - 失败缺口 = 下载履历里「最近一次失败、此后未成功」的帖 / 链接，持久记录，
//   任务被清空或按 TASK_MAX_KEEP 轮转后依然存在（资产卡「下载失败 N」就是它）；
// - 失败任务 = 任务列表里 status === 'failed' 的任务数，一个任务可含多条失败链接，
//   且会随任务清空 / 轮转消失。
// 业界口径与此一致（Sonarr/Radarr 的 Wanted→Missing 与 Activity 分栏、qBittorrent 的 Errored 条目）；
// 且下钻必须落到**与 KPI 同口径**的列表（Grafana/Datadog 的 drill-down 一致性），
// 故资产卡下钻带 `?view=failures` 直达本视图，数字与清单条数严格相等。
//
// 第三视图「可重下」是「帖级 gone」：曾下载成功、此后目录被资源管理清理的帖。
// 它不在任务列表（任务可能早已清空 / 轮转），也不在失败缺口（那批从未下成过），
// 三者量纲互不相同，故各自独立分栏，且各自落一个与卡上数字严格同源的清单接口。
type ViewName = 'tasks' | 'failures' | 're-downloads'
const view = computed<ViewName>(() => {
  const q = route.query.view
  return q === 'failures' || q === 're-downloads' ? q : 'tasks'
})
/** 视图切换写入 URL query：刷新 / 前进后退 / 二次下钻都能保持同一视图 */
function setView(v: string | number | boolean) {
  const name = String(v)
  router.replace({ path: '/downloads', query: name === 'tasks' ? {} : { view: name } })
}

const failures = ref<DownloadFailureItem[]>([])
const failuresLoading = ref(false)

// ---- 可重下清单（帖级 gone）----
const reDownloads = ref<ReDownloadItem[]>([])
const reDownloadsLoading = ref(false)

// ---- 两帖级清单的筛选（参考帖子浏览：关键词跨 版块/标题/链接 模糊匹配）----
const failSearch = ref('')
const reSearch = ref('')
function _match<T extends { fid_name: string; title: string; url: string }>(
  items: T[],
  q: string,
): T[] {
  const k = q.trim().toLowerCase()
  if (!k) return items
  return items.filter(
    (r) =>
      (r.fid_name && r.fid_name.toLowerCase().includes(k)) ||
      (r.title && r.title.toLowerCase().includes(k)) ||
      r.url.toLowerCase().includes(k),
  )
}
const filteredFailures = computed(() => _match(failures.value, failSearch.value))
const filteredReDownloads = computed(() => _match(reDownloads.value, reSearch.value))
// 输入即回到第 1 页（筛选后停留越界页会显示空列表）
function onFailSearch() {
  failPage.value = 1
}
function onReSearch() {
  rePage.value = 1
}

/** 视图切换项：把各自的条数直接标在选项上（切换时无需先点进去才知道有多少） */
const viewOptions = computed(() => [
  { label: `下载任务（${tasks.value.length}）`, value: 'tasks' },
  { label: `失败缺口（${failures.value.length}）`, value: 'failures' },
  { label: `可重下（${reDownloads.value.length}）`, value: 're-downloads' },
])

/** 失败缺口清单（接口与资产卡同源：卡上 N = 清单 N 条）。
 *
 *  自带「在途则跳过」守卫：api 层对同 URL 请求会中止前一个在途请求，
 *  而本函数会被挂载与任务变化同时触发——不守卫就会出现「后发的把先发的掐掉」，
 *  徽标与清单要等下一轮才补上（实测表现为下钻后短暂显示 0 条）。 */
let failuresInflight = false
async function loadFailures(first = false) {
  if (failuresInflight) return
  if (first && !failures.value.length) failuresLoading.value = true
  failuresInflight = true
  try {
    const r = await api.downloadFailures()
    failures.value = r.items
  } catch {
    // 静默：保留旧值，等下一轮刷新（与页面其它补充数据同容错策略）
  } finally {
    failuresInflight = false
    failuresLoading.value = false
  }
}

/** 可重下清单（与失败缺口同一套守卫策略：在途跳过、失败静默保留旧值） */
let reDownloadsInflight = false
async function loadReDownloads(first = false) {
  if (reDownloadsInflight) return
  if (first && !reDownloads.value.length) reDownloadsLoading.value = true
  reDownloadsInflight = true
  try {
    const r = await api.downloadReDownloads()
    reDownloads.value = r.items
  } catch {
    // 静默：保留旧值，等下一轮刷新
  } finally {
    reDownloadsInflight = false
    reDownloadsLoading.value = false
  }
}

// 缺口清单分页：与「帖子浏览」同款——前端切片 + 顶/底双分页条 + 每页条数可选（[20,50,100,200]）。
// 数据已在内存（接口全量返回），故无需后端分页；双分页条与帖子浏览一致（长列表滚到顶也能翻页）。
const failPage = ref(1)
const failPageSize = ref(20)

// ---- 两帖级清单的表头排序（参考帖子浏览：sortable="custom" + @sort-change 三态）----
// 数据已在内存，故排序对**全量过滤结果**生效后再切片分页；不能用 el-table 本地排序
// （本地排序只作用于当前页，等于没排——与帖子浏览「分页列表必须走服务端排序」同一结论，
//  此处因数据本就全量在前端，故在切片前排序等价于服务端排序的效果）。
type SortState = { prop: string; order: 'ascending' | 'descending' | null }
const failSort = ref<SortState>({ prop: '', order: null })
const reSort = ref<SortState>({ prop: '', order: null })

/** 按排序状态排序全量数组：空值恒沉底（与「-」占位一致），文本按 localeCompare 比较 */
function sortItems<T>(items: T[], s: SortState): T[] {
  if (!s.prop || !s.order) return items
  const dir = s.order === 'ascending' ? 1 : -1
  const at = (o: T): unknown => (o as unknown as Record<string, unknown>)[s.prop]
  return [...items].sort((a, b) => {
    const av = at(a)
    const bv = at(b)
    const ae = av === undefined || av === null || av === ''
    const be = bv === undefined || bv === null || bv === ''
    if (ae && be) return 0
    if (ae) return 1 // 空值恒沉底，不随升降序翻转
    if (be) return -1
    return String(av).localeCompare(String(bv)) * dir
  })
}

/** 表头三态：升序 → 降序 → 取消（取消即回接口原始顺序）；排序变化回第 1 页 */
function onFailSortChange(s: { prop: string | null; order: 'ascending' | 'descending' | null }) {
  failSort.value = { prop: s.prop ?? '', order: s.order }
  failPage.value = 1
}

const pagedFailures = computed(() =>
  sortItems(filteredFailures.value, failSort.value).slice(
    (failPage.value - 1) * failPageSize.value,
    failPage.value * failPageSize.value,
  ),
)
// 可重下清单分页：与失败缺口共用同一分页模式（不另起一套分页参数）
const rePage = ref(1)
const rePageSize = ref(20)
function onReSortChange(s: { prop: string | null; order: 'ascending' | 'descending' | null }) {
  reSort.value = { prop: s.prop ?? '', order: s.order }
  rePage.value = 1
}
const pagedReDownloads = computed(() =>
  sortItems(filteredReDownloads.value, reSort.value).slice(
    (rePage.value - 1) * rePageSize.value,
    rePage.value * rePageSize.value,
  ),
)
/** 分页回调（与帖子浏览同签名）：翻页只切页码；改每页条数后回到第 1 页，避免停留在越界页 */
function onFailPageChange(p: number) {
  failPage.value = p
}
function onFailSizeChange(s: number) {
  failPageSize.value = s
  failPage.value = 1
}
function onRePageChange(p: number) {
  rePage.value = p
}
function onReSizeChange(s: number) {
  rePageSize.value = s
  rePage.value = 1
}
watch(view, () => {
  failPage.value = 1
  rePage.value = 1
})

// ---- D8 提交区：多行粘贴自动拆解 URL ----
const inputText = ref('') // 原始输入（每行一个 URL，兼容分号/逗号/空格分隔）
// 按 换行/分号/逗号/空白 拆分并去重保序
const parsedUrls = computed<string[]>(() => {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of splitTokens.value) {
    if (!raw || seen.has(raw)) continue
    seen.add(raw)
    out.push(raw)
  }
  return out
})
const splitTokens = computed<string[]>(() =>
  inputText.value.split(/[\n;,，；\s]+/).map((s) => s.trim()),
)
// 非空但不是 http(s) 开头的行（无效行前置提示，提交时忽略）
const invalidLines = computed<string[]>(() =>
  splitTokens.value.filter((t) => t && !isValidUrl(t)),
)
function isValidUrl(s: string): boolean {
  return /^https?:\/\//i.test(s)
}
const canSubmit = computed(() => parsedUrls.value.length > 0)

// ---- D3 状态筛选与汇总 ----
const TERMINAL = ['done', 'failed', 'cancelled']
/** 可「开始下载」的状态：失败 / 已取消（重跑未成功链接）、已暂停（继续未完成链接） */
const STARTABLE = ['failed', 'cancelled', 'paused']
/** 可「暂停」的状态：排队中 / 下载中（在跑的链接收尾后不再提交新链接） */
const PAUSABLE = ['running', 'pending']
const filterStatus = ref<'all' | 'active' | 'paused' | 'done' | 'failed' | 'cancelled'>('all')
const activeCount = computed(
  () => tasks.value.filter((t) => t.status === 'running' || t.status === 'pending').length,
)
const pausedCount = computed(() => tasks.value.filter((t) => t.status === 'paused').length)
const doneCount = computed(() => tasks.value.filter((t) => t.status === 'done').length)
const failedCount = computed(() => tasks.value.filter((t) => t.status === 'failed').length)
const cancelledCount = computed(
  () => tasks.value.filter((t) => t.status === 'cancelled').length,
)
const filterOptions = computed(() => [
  { label: `全部（${tasks.value.length}）`, value: 'all' },
  { label: `进行中（${activeCount.value}）`, value: 'active' },
  { label: `已暂停（${pausedCount.value}）`, value: 'paused' },
  { label: `已完成（${doneCount.value}）`, value: 'done' },
  // 明确写「失败任务」：与「失败缺口（帖）」区分量纲，避免两个数字被当成同一个（见 view 注释）
  { label: `失败任务（${failedCount.value}）`, value: 'failed' },
  { label: `已取消（${cancelledCount.value}）`, value: 'cancelled' },
])
/** 状态展示优先级：正在下载 → 排队中 → 已暂停 → 失败 → 已取消 → 已完成。
 *  后端按 dict 遍历返回，顺序与状态无关且刷新可能变化，故在展示层统一排序，
 *  保证「刷新前后位置不变、运行中的始终在最前」；终态内「已完成」排在「已取消」
 *  之后沉底——历史成功任务不再干扰对失败/已取消任务的后续处理。
 *  「已暂停」紧随「排队中」：它是**待用户处置**的状态（点「开始下载」即可继续），
 *  排在失败之前——用户第一眼就该看到「有任务被我暂停着」。 */
const STATUS_RANK: Record<string, number> = {
  running: 0,
  pending: 1,
  paused: 2,
  failed: 3,
  cancelled: 4,
  done: 5,
}
/** 未知状态的兜底序号：与「失败」同级（既非进行中也非已完成，需用户关注） */
const STATUS_RANK_FALLBACK = 3
function statusRank(status: string): number {
  return STATUS_RANK[status] ?? STATUS_RANK_FALLBACK
}

const filteredTasks = computed<DownloadTaskSummary[]>(() => {
  let list: DownloadTaskSummary[]
  if (filterStatus.value === 'all') list = tasks.value
  else if (filterStatus.value === 'active')
    list = tasks.value.filter((t) => t.status === 'running' || t.status === 'pending')
  else list = tasks.value.filter((t) => t.status === filterStatus.value)
  // 复制后排序（不原地改 tasks，避免影响其它引用）
  return [...list].sort((a, b) => {
    const ra = statusRank(a.status)
    const rb = statusRank(b.status)
    if (ra !== rb) return ra - rb
    // 排队中/进行中：与实际执行顺序一致——被「优先执行」置顶的在前（队列优先级 0），
    // 其余按入队令牌 ticket 升序（先入队先执行，与 PriorityQueue 出队顺序相同）。
    // 此前按创建时间倒序展示，与 FIFO 执行顺序相反，让人误以为最新任务会先下载。
    if (ra <= 1) {
      if (!!a.priority !== !!b.priority) return a.priority ? -1 : 1
      const ta = a.ticket ?? 0
      const tb = b.ticket ?? 0
      if (ta !== tb) return ta - tb
      return a.id < b.id ? -1 : 1
    }
    // 终态：按创建时间倒序（新的在前），时间相同用 id 兜底保证排序完全稳定
    if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1
    return a.id < b.id ? -1 : 1
  })
})

/** 任务列表分页：与链接明细同一模式的前端切片分页（业界实时面板做法的轻量实现：
 *  活跃任务按排序始终置顶、不受翻页影响；历史终态区按页浏览。任务保留上限 200，
 *  此规模无需虚拟滚动/后端分页）。 */
const TASK_PAGE_SIZE = 20
const taskPage = ref(1)
const pagedTasks = computed(() =>
  filteredTasks.value.slice(
    (taskPage.value - 1) * TASK_PAGE_SIZE,
    taskPage.value * TASK_PAGE_SIZE,
  ),
)
// 切换状态筛选时回到第一页，避免停留在超出范围的页码
watch(filterStatus, () => {
  taskPage.value = 1
})

// ---- 任务勾选：工具栏「开始下载 / 全部暂停」的作用域 ----
/** 勾选集合以任务 ID 为键：SSE 每 500ms 推送会整体替换任务数组、翻页也会换一批行，
 *  但勾选跟着任务走（与资源管理页「选中跟条目走，不跟视图走」同一策略）——
 *  否则一边看着选中状态一边被推送清空，用户会以为勾选失效。
 *  刻意不用 el-table 的 type="selection"：其内部选中态与移动端卡片是两套状态，
 *  需双向同步（reserve-selection + toggleRowSelection），一处漏同步即两端不一致；
 *  自持 Set 则桌面与移动端天然同源。 */
const selectedIds = ref<Set<string>>(new Set())
const selectedTasks = computed(() => tasks.value.filter((t) => selectedIds.value.has(t.id)))
// 列表刷新后裁剪勾选：已被清空/轮转掉的任务移出选中集合，避免提交无效 ID
watch(tasks, (list) => {
  if (!selectedIds.value.size) return
  const alive = new Set(list.map((t) => t.id))
  const next = new Set([...selectedIds.value].filter((id) => alive.has(id)))
  if (next.size !== selectedIds.value.size) selectedIds.value = next
})
function isSelected(id: string): boolean {
  return selectedIds.value.has(id)
}
function toggleSelect(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}
/** 表头复选框作用域 = 当前页（与分页一致，不做「跨页隐式全选」） */
const pageAllSelected = computed(
  () => pagedTasks.value.length > 0 && pagedTasks.value.every((t) => selectedIds.value.has(t.id)),
)
const pageSomeSelected = computed(() => pagedTasks.value.some((t) => selectedIds.value.has(t.id)))
function toggleSelectPage() {
  const next = new Set(selectedIds.value)
  if (pageAllSelected.value) {
    for (const t of pagedTasks.value) next.delete(t.id)
  } else {
    for (const t of pagedTasks.value) next.add(t.id)
  }
  selectedIds.value = next
}

// ---- 主操作按钮：有在途任务=「全部暂停」，无在途但有未完成任务=「全部开始 / 开始下载」 ----
/** 在途集合（排队中 + 下载中）与未完成任务集合（失败 / 已取消 / 已暂停）：
 *  口径与统计卡 / 状态筛选一致——按钮语义与用户看到的数字必须同源。
 *  设计对齐迅雷：进入下载中心，只要存在未完成任务，就直接给出「全部开始」一键启动，
 *  不必先逐个勾选；勾选只是用来「收窄作用域」（只动勾选的那几个），而不是「解锁按钮的开关」。
 *  旧设计里「开始下载」默认禁用、必须手动勾选才点亮，而手机端又无全选控件，
 *  导致用户进来看不到可点的开始按钮——这与暂停模式下「无需勾选即可全部暂停」不对称。 */
const pausableAll = computed(() => tasks.value.filter((t) => PAUSABLE.includes(t.status)))
const startableAll = computed(() => tasks.value.filter((t) => STARTABLE.includes(t.status)))
const startableSelected = computed(() =>
  selectedTasks.value.filter((t) => STARTABLE.includes(t.status)),
)
const pausableSelected = computed(() =>
  selectedTasks.value.filter((t) => PAUSABLE.includes(t.status)),
)
const pauseMode = computed(() => pausableAll.value.length > 0)
/** 暂停作用域：勾选了在途任务就只暂停这些（用户点名）；否则按按钮文案「全部暂停」作用于全部在途任务 */
const pauseTargets = computed(() =>
  pausableSelected.value.length ? pausableSelected.value : pausableAll.value,
)
/** 开始作用域（与暂停对称）：勾选了未完成任务就只动这些；否则按「全部开始」作用于全部未完成任务 */
const startTargets = computed(() =>
  startableSelected.value.length ? startableSelected.value : startableAll.value,
)
/** 主按钮本次点击影响的任务数（显示在按钮上，让作用域可见，不必点下去才知道） */
const primaryCount = computed(() =>
  pauseMode.value ? pauseTargets.value.length : startTargets.value.length,
)
const primaryLabel = computed(() => {
  if (pauseMode.value) return '全部暂停'
  // 未勾选时给「全部开始」（迅雷入口态）；勾选了未完成任务则收窄为「开始下载」
  return startableSelected.value.length ? '开始下载' : '全部开始'
})
const primaryDisabled = computed(() => primaryCount.value === 0)
const primaryTip = computed(() => {
  if (pauseMode.value) {
    const n = pausableSelected.value.length
    return n
      ? `暂停勾选的 ${n} 个任务：不再提交新链接，已提交的链接收尾后停止；剩余链接可再「开始下载」继续`
      : `未勾选任务，将暂停全部 ${pausableAll.value.length} 个进行中任务；剩余链接可再「开始下载」继续`
  }
  const all = startableAll.value.length
  const n = startableSelected.value.length
  if (n) {
    return `下载勾选的 ${n} 个任务中未完成的链接（失败/已取消重跑、已暂停继续；已成功的链接不重复下载）`
  }
  if (all) {
    return `一键开始全部 ${all} 个未完成任务中未完成的链接（失败/已取消重跑、已暂停继续；已成功的链接不重复下载）；如需只开始部分，先勾选对应任务`
  }
  return '当前没有未完成的任务（失败 / 已取消 / 已暂停），无需开始'
})

// ---- D2 重复提交提醒：区分「文件仍在 / 已不在 / 正在下载」三类 ----
// （escapeHtml / briefList 已抽到 utils/text.ts，与帖子浏览下载入口共用）

async function submitUrls() {
  const urls = parsedUrls.value
  if (!urls.length) {
    ElMessage.warning('请输入至少一个有效 URL（http/https 开头）')
    return
  }

  let pending = urls
  try {
    const dup = await api.checkDownloadDup(urls)

    // 1) 正在下载中的链接直接剔除，避免两个任务并发写同一文件
    const drop = new Set(dup.running)
    if (drop.size) {
      pending = pending.filter((u) => !drop.has(u))
      ElMessage.warning(`已移除 ${drop.size} 个正在下载中的链接，避免同一文件被并发写入`)
      if (!pending.length) {
        ElMessage.warning('所选链接均已在下载中，未重复提交')
        return
      }
    }

    // 2) 历史下载过的链接：文件「仍在」与「已不在」后果完全不同，必须分开说清楚
    const alive = dup.still_exists.filter((u) => !drop.has(u))
    const gone = dup.gone.filter((u) => !drop.has(u))
    if (alive.length || gone.length) {
      const lines: string[] = []
      if (alive.length) {
        lines.push(
          `<b>${alive.length} 个链接文件仍在</b>，提交后会跳过（不重复下载）：<br>${briefList(alive)}`,
        )
      }
      if (gone.length) {
        lines.push(
          `<b>${gone.length} 个链接曾下载过但文件已不在</b>，提交后会重新下载：<br>${briefList(gone)}`,
        )
      }
      const go = await ElMessageBox.confirm(lines.join('<br><br>'), '重复提交提醒', {
        type: 'warning',
        dangerouslyUseHTMLString: true,
        confirmButtonText: '仍要提交',
        cancelButtonText: '取消',
      })
        .then(() => true)
        .catch(() => false)
      if (!go) return
    }
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`提交前检查失败: ${(e as Error).message}`)
    return
  }

  try {
    const r = await api.submitDownload(pending)
    ElMessage.success(`任务已提交（${r.count} 个链接），可在下方查看进度`)
    inputText.value = ''
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`提交失败: ${(e as Error).message}`)
  }
}

// ---- D4 完成通知：轮询中检测任务状态从非终态转为终态 ----
const knownStatus = new Map<string, string>()
function diffAndNotify(list: DownloadTaskSummary[]) {
  for (const t of list) {
    const prev = knownStatus.get(t.id)
    if (prev && prev !== t.status) {
      if (t.status === 'done') {
        ElNotification.success({
          title: '下载任务完成',
          message: `全部 ${t.total} 个链接处理完毕`,
        })
      } else if (t.status === 'failed') {
        ElNotification.error({
          title: '下载任务失败',
          message: '任务执行中断，可在列表中点击「下载」重跑未成功链接',
        })
      } else if (t.status === 'cancelled') {
        ElNotification.info({ title: '下载任务已取消', message: `已完成 ${t.done}/${t.total}` })
      }
    }
    knownStatus.set(t.id, t.status)
  }
}

// ---- D1/D5/D9 任务操作 ----
/** 被跳过的任务按原因归并成一句话（如「正在下载 2 个、已完成 1 个」）：
 *  用户关心的是「为什么没开始」，逐条罗列任务 ID 没有信息量。 */
function skipSummary(skipped: { reason: string }[]): string {
  const byReason = new Map<string, number>()
  for (const s of skipped) byReason.set(s.reason, (byReason.get(s.reason) ?? 0) + 1)
  return [...byReason.entries()].map(([reason, n]) => `${reason} ${n} 个`).join('、')
}

/** 开始下载（批量与行内共用一套）：失败 / 已取消任务重跑未成功链接、已暂停任务继续未完成链接。
 *  行内「下载」= 只传 1 个 ID 的同一调用，故两个入口的语义与提示天然一致。 */
async function startTasks(list: DownloadTaskSummary[]) {
  if (!list.length) {
    ElMessage.warning('请先勾选未完成的任务（失败 / 已取消 / 已暂停）')
    return
  }
  try {
    const r = await api.startDownloads(list.map((t) => t.id))
    if (r.ids.length) ElMessage.success(`已开始 ${r.ids.length} 个任务（共 ${r.links} 个链接）`)
    // 跳过原因如实回传：静默忽略会让用户以为按钮没生效（与后端「不静默丢弃」对偶）
    if (r.skipped.length) ElMessage.warning(`跳过 ${r.skipped.length} 个：${skipSummary(r.skipped)}`)
    if (!r.ids.length && !r.skipped.length) ElMessage.info('没有可下载的链接')
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`开始下载失败: ${(e as Error).message}`)
  }
}

/** 暂停（非终态）：不再提交新链接，未跑链接保留，可再「开始下载」继续 */
async function pauseTasks(list: DownloadTaskSummary[]) {
  if (!list.length) {
    ElMessage.info('当前没有进行中的任务')
    return
  }
  try {
    const r = await api.pauseDownloads(list.map((t) => t.id))
    if (r.ids.length) {
      ElMessage.success(
        `已暂停 ${r.ids.length} 个任务${r.links ? `（剩余 ${r.links} 个链接）` : ''}`,
      )
    }
    if (r.skipped.length) ElMessage.warning(`跳过 ${r.skipped.length} 个：${skipSummary(r.skipped)}`)
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`暂停失败: ${(e as Error).message}`)
  }
}

/** 行内「下载」：失败 / 已取消（重跑未成功链接）、已暂停（继续未完成链接） */
function downloadTask(row: DownloadTaskSummary) {
  void startTasks([row])
}

/** 工具栏主按钮：有在途任务 → 全部暂停（勾选了在途任务则只暂停勾选的）；
 *  否则 → 开始下载（勾选了未完成任务则只动勾选的，否则一键全部开始） */
function onPrimaryAction() {
  if (pauseMode.value) void pauseTasks(pauseTargets.value)
  else void startTasks(startTargets.value)
}

async function prioritizeTask(row: DownloadTaskSummary) {
  try {
    await api.prioritizeDownload(row.id)
    ElMessage.success('任务已置顶，将优先执行')
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`置顶失败: ${(e as Error).message}`)
  }
}

async function clearFinished() {
  if (!tasks.value.some((t) => t.status === 'done')) {
    ElMessage.info('当前没有已完成的任务')
    return
  }
  const go = await ElMessageBox.confirm(
    '将删除全部「已完成」任务记录（不影响已下载文件）；失败 / 已取消任务会保留，确定吗？',
    '清空已完成任务',
    { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
  )
    .then(() => true)
    .catch(() => false)
  if (!go) return
  try {
    const r = await api.clearDownloads()
    ElMessage.success(`已清空 ${r.cleared} 条任务记录`)
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`清空失败: ${(e as Error).message}`)
  }
}

async function cancelTask(row: DownloadTaskSummary) {
  try {
    await api.cancelDownload(row.id)
    ElMessage.success('已请求取消')
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`取消失败: ${(e as Error).message}`)
  }
}

async function deleteTask(row: DownloadTaskSummary) {
  const go = await ElMessageBox.confirm('确定删除该任务记录吗？', '删除任务', {
    type: 'warning',
    confirmButtonText: '删除',
    cancelButtonText: '取消',
  })
    .then(() => true)
    .catch(() => false)
  if (!go) return
  try {
    await api.deleteDownload(row.id)
    ElMessage.success('已删除')
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

// ---- 详情抽屉（R1：明细与日志按需加载，列表仅持概要） ----
const detailVisible = ref(false)
const detailId = ref('')
const detailTask = ref<DownloadTaskDetail | null>(null)
const detailLoading = ref(false)

async function fetchDetail(first = false) {
  if (!detailId.value) return
  if (first) detailLoading.value = true
  try {
    detailTask.value = await api.downloadTask(detailId.value)
  } catch (e) {
    if (!isAborted(e)) ElMessage.error(`加载详情失败: ${(e as Error).message}`)
  } finally {
    if (first) detailLoading.value = false
  }
}

function showDetail(row: DownloadTaskSummary) {
  detailId.value = row.id
  detailTask.value = null
  detailVisible.value = true
  void fetchDetail(true)
}

// ---- 执行日志展示：任务事件区 + 按链接折叠的下载过程明细 ----
const logsBoxRef = ref()

/** 明细日志的渲染窗口（行数，按全部明细行计，非每组）。

  *  背景：一个任务的过程日志可达数千行（后端上限 2000 行 / 近 20 万字符），
  *  此前 <pre> 一次性渲染全部行，每 500ms 有更新就整体重建这个巨大的文本节点，
  *  浏览器重排/重绘成本极高——表现为「详情打开后日志一多就卡住」。
  *  而日志窗可视范围只有十几行，全量渲染纯属浪费。
  *  故按业界日志面板（CI / 终端）做法只保留最近 N 行：滚动窗口 + 超出部分丢弃，
  *  渲染量降到约 1/7，更新即流畅；需要看更早内容时可用「下载日志」。 */
const MAX_DETAIL_LINES = 300

/** 解析任务日志：非缩进行为任务事件 / 逐 URL 结果摘要；
 *  缩进的 [i/N] 行（下载过程明细）归入对应链接组，供折叠展示 */
const parsedLogs = computed(() => {
  const events: string[] = []
  /** 每个链接一组：必须按 seq 聚合，不能按出现顺序分组——
   *  并发下载时多个链接的日志是交错写入的（[9/50]、[8/50]、[9/50]…），
   *  按出现顺序分组会为每行新建一组（实测产生 1071 个折叠面板，是卡顿的主因）。 */
  const groupMap = new Map<
    string,
    { seq: string; title: string; lines: string[]; total: number; lastIdx: number }
  >()
  // idx = 该明细行在任务日志中的全局行序，用于「按最后活动时间」排序分组
  const detailLines: { seq: string; text: string; idx: number }[] = []

  for (const [idx, raw] of (detailTask.value?.logs || []).entries()) {
    // 明细行：前导空白 + [i/N]（由后端缩进标注归属）
    const detail = raw.match(/^\s+\[(\d+\/\d+)\]\s?(.*)$/)
    if (detail) {
      detailLines.push({ seq: detail[1], text: detail[2], idx })
      continue
    }
    // 结果摘要行：[i/N] 开头（无缩进）—— 兼作该链接分组的标题
    const summary = raw.match(/^\[(\d+\/\d+)\]\s?(.*)$/)
    if (summary) {
      const g = groupMap.get(summary[1]) ?? {
        seq: summary[1],
        title: '',
        lines: [],
        total: 0,
        lastIdx: -1,
      }
      g.title = summary[2]
      groupMap.set(summary[1], g)
      events.push(raw)
      continue
    }
    events.push(raw)
  }

  // 全局尾部窗口：只渲染最近 N 行明细（日志可达数千行，全量渲染必然卡）
  const hiddenTotal = Math.max(0, detailLines.length - MAX_DETAIL_LINES)
  for (const { seq, text, idx } of detailLines.slice(-MAX_DETAIL_LINES)) {
    const g = groupMap.get(seq) ?? { seq, title: '', lines: [], total: 0, lastIdx: -1 }
    g.lines.push(text)
    g.total += 1
    // 最后一次活动位置：用于把「最近还在输出」的组排到末尾
    g.lastIdx = idx
    groupMap.set(seq, g)
  }

  // 排序口径：按「最后一次活动时间」升序（业界 CI 日志面板做法——时间序流、最新在最后，
  // 配合已有的自动跟随到底部即 tail -f 体验），而不是按链接序号升序。
  // 原因：重下（行内「下载」/ 单条「重新下载」）只跑未成功的链接，其序号往往更小——按序号排会让
  // 正在输出的新日志落在列表中间的面板里，用户滚到底反而看不到最新内容。
  const groups = [...groupMap.values()]
    .filter((g) => g.lines.length > 0)
    .sort((a, b) => a.lastIdx - b.lastIdx)
  // 运行中标记：任务在跑且该组是最后活动的组（正在写入的那一个）
  const running = detailTask.value?.status === 'running'
  const activeSeq = running && groups.length ? groups[groups.length - 1].seq : ''
  return {
    events,
    groups: groups.map((g) => ({ ...g, active: g.seq === activeSeq })),
    hiddenTotal,
    detailTotal: detailLines.length,
  }
})

// 折叠项：默认全部展开——否则用户打开详情只看到任务事件，
// 会误以为「没有下载过程明细」（明细在下方折叠区里）
const expandedLogs = ref<string[]>([])
watch(
  () => parsedLogs.value.groups.map((g) => g.seq).join(','),
  (keys) => {
    expandedLogs.value = keys ? keys.split(',') : []
  },
  { immediate: true },
)

// 日志更新（SSE / 轮询）时自动滚动到最新
watch(
  () => detailTask.value?.logs?.length,
  async () => {
    await nextTick()
    const box = (logsBoxRef.value as { textarea?: HTMLTextAreaElement } | undefined)?.textarea
    if (box) box.scrollTop = box.scrollHeight
  },
)

// ---- 「按链接明细」固定高度日志窗：内容增长时自动跟随到底部 ----
const detailScrollRef = ref<HTMLElement | null>(null)
/** 是否跟随最新：用户上翻查看历史时自动关闭，滚回底部自动恢复 */
const followLatest = ref(true)

// ---- 链接明细：统计 + 状态筛选（几十条明细按业界 CI 做法：异常优先、可筛选） ----
/** 明细状态筛选：all=全部 / problem=异常（失败+取消）/ ok=成功 */
const itemFilter = ref<'all' | 'problem' | 'ok'>('all')

const itemStats = computed(() => {
  const items = detailTask.value?.items ?? []
  const n = (s: string) => items.filter((i) => i.status === s).length
  const ok = n('ok')
  const fail = n('fail')
  const cancelled = n('cancelled')
  return { total: items.length, ok, fail, cancelled, skip: n('skip'), problem: fail + cancelled }
})

/** 异常优先排序 + 状态筛选：让用户第一眼看到需要处理的链接 */
const filteredItems = computed(() => {
  const items = [...(detailTask.value?.items ?? [])]
  // 异常（fail/cancelled）在前，其次 pending/running，成功与跳过在后
  const rank: Record<string, number> = { fail: 0, cancelled: 0, running: 1, pending: 1 }
  items.sort((a, b) => (rank[a.status] ?? 2) - (rank[b.status] ?? 2))
  if (itemFilter.value === 'problem') return items.filter((i) => (rank[i.status] ?? 2) === 0)
  if (itemFilter.value === 'ok') return items.filter((i) => i.status === 'ok' || i.status === 'skip')
  return items
})

/** 明细分页：几十上百条链接全量渲染既卡又难翻找，数据已在内存，前端切片分页 */
const ITEM_PAGE_SIZE = 20
const itemPage = ref(1)
const pagedItems = computed(() =>
  filteredItems.value.slice(
    (itemPage.value - 1) * ITEM_PAGE_SIZE,
    itemPage.value * ITEM_PAGE_SIZE,
  ),
)
// 序号列跨页连续显示（第 2 页从 21 开始，而非每页重新从 1 计数）
function itemIndex(i: number): number {
  return (itemPage.value - 1) * ITEM_PAGE_SIZE + i + 1
}
// 切换任务或筛选条件时回到第一页，避免停留在超出范围的页码
watch([itemFilter, () => detailTask.value?.id], () => {
  itemPage.value = 1
})

/** 任务整体在跑（含暂停后仍在收尾的窗口）：单条重下会重新调度整个任务，
 *  与正在执行的 worker 并发跑同一任务，故禁用（与后端 retry_url 同一守卫）。 */
function itemRetryLocked(): boolean {
  const t = detailTask.value
  return t?.status === 'running' || t?.pause_requested === true
}
/** 明细行「重新下载」的可用性与提示：成功/跳过无需重下，进行中/排队中不可重下 */
function itemRetryDisabled(status: string): boolean {
  if (itemRetryLocked()) return true
  return status === 'ok' || status === 'skip' || status === 'running' || status === 'pending'
}
function itemRetryTip(status: string): string {
  if (itemRetryLocked()) return '任务正在下载中（或暂停后仍在收尾），请等它停下后再重下该链接'
  if (status === 'ok' || status === 'skip') return '该链接已成功，无需重下'
  if (status === 'running' || status === 'pending') return '该链接正在下载或排队中，暂不能重下'
  return '重新下载该链接（在本任务内重跑，结果显示在当前详情）'
}

/** 重新下载单条链接：就地重跑原任务的该链接（不另开任务），结果显示在当前详情页 */
async function retryItem(row: { url: string; status: string }) {
  const tid = detailTask.value?.id
  if (!tid) return
  try {
    await api.retryDownloadUrl(tid, row.url)
    ElMessage.success('已在本任务内重新下载，可在当前页面查看进度')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`重新下载失败: ${(e as Error).message}`)
  }
}

// ---- 帖级清单（失败缺口 / 可重下）共用：逐条重下 ----
/** 按链接新建任务重下（两个帖级清单共用这一份实现）。
 *
 *  与提交区 `submitUrls` 的差别（刻意，不是重复实现）：
 *  这两类条目必然「本地无文件」——缺口是没下成过、可重下是文件已被清理——
 *  故无需「文件仍在 / 已不在」的确认；真正要挡的只有**并发写同一文件**：
 *  该链接正在下载中就直接跳过并提示。
 *
 *  刷新范围：任务列表（新建了任务）+ 两个帖级清单（重下成功后该条目应退出清单）。
 */
async function redownloadByUrl(url: string) {
  try {
    const dup = await api.checkDownloadDup([url])
    if (dup.running.includes(url)) {
      ElMessage.warning('该链接正在下载中，无需重复提交')
      return
    }
    await api.submitDownload([url])
    ElMessage.success('已创建下载任务，可在「下载任务」视图查看进度')
    await loadTasks()
    await loadFailures()
    await loadReDownloads()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`重新下载失败: ${(e as Error).message}`)
  }
}

/** 日志指纹：行数 + 末行内容，任一变化即视为有新输出 */
const detailLogKey = computed(() => {
  const logs = detailTask.value?.logs ?? []
  return `${logs.length}:${logs[logs.length - 1] ?? ''}`
})

function onDetailScroll() {
  const el = detailScrollRef.value
  if (!el) return
  // 距底部 24px 内视为「在底部」，恢复跟随
  followLatest.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
}

watch(detailLogKey, async () => {
  if (!followLatest.value) return
  await nextTick()
  const el = detailScrollRef.value
  if (el) el.scrollTop = el.scrollHeight
})

/** 切换任务时也回到顶部重新跟随 */
watch(detailId, () => {
  followLatest.value = true
})

function statusTagType(status: string): string {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'primary'
  // 已暂停用 warning（需用户处置的临时态，需被一眼看到）；已取消改为 info（用户主动结束的终态）
  if (status === 'paused') return 'warning'
  return 'info' // pending / cancelled
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    pending: '排队中',
    running: '下载中',
    paused: '已暂停',
    done: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }
  return map[status] ?? status
}

function itemTagType(status: string): string {
  if (status === 'ok') return 'success'
  if (status === 'fail') return 'danger'
  if (status === 'running') return 'primary'
  if (status === 'pending') return 'info'
  return 'warning' // skip / cancelled
}

function itemText(status: string): string {
  const map: Record<string, string> = {
    pending: '等待中',
    running: '下载中',
    ok: '成功',
    skip: '已存在',
    fail: '失败',
    cancelled: '已取消',
  }
  return map[status] ?? status
}

// ---- 任务更新通道：R2 SSE 实时推送为主，3s 轮询作为断线降级 ----
/** 任务列表指纹：任一任务的状态 / 进度变化即视为「列表变了」 */
function taskSignature(list: DownloadTaskSummary[]): string {
  return list.map((t) => `${t.id}:${t.status}:${t.done}/${t.total}`).join('|')
}
let taskSig = ''

/** 应用任务列表的唯一入口（SSE 与轮询共用）：只在内容真的变化时才做副作用，
 *  否则 SSE 每 500ms 一帧会导致通知与缺口清单被反复重取。 */
function applyTasks(list: DownloadTaskSummary[]) {
  const sig = taskSignature(list)
  const changed = sig !== taskSig
  taskSig = sig
  tasks.value = list
  diffAndNotify(list)
  if (detailVisible.value && detailId.value) void fetchDetail()
  // 失败缺口由下载履历派生：任务推进 / 收尾会改变缺口，任务侧有变化就顺带刷新
  // （接口 5s TTL 缓存 + 写接口已失效，这里不会造成风暴）
  // 可重下清单同样由任务推进派生（重下成功后该帖应立刻退出清单），一并刷新
  if (changed) {
    void loadFailures()
    void loadReDownloads()
  }
}

async function loadTasks() {
  try {
    const r = await api.downloadTasks()
    error.value = ''
    applyTasks(r.tasks)
  } catch (e) {
    if (isAborted(e)) return
    error.value = (e as Error).message
  }
}

function tick() {
  if (document.hidden) return // 页面不可见时暂停轮询，切回可见立即刷新
  void loadTasks()
}

function onVisibility() {
  if (!document.hidden) void loadTasks()
}

// 轮询降级通道（SSE 正常时停止）
function startPolling() {
  if (pollTimer !== null) return
  pollTimer = window.setInterval(tick, REFRESH_INTERVAL)
}
function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

// SSE 主通道：EventSource 断线由浏览器自动重连（onerror 期间开启轮询兜底，onopen 恢复后停止）
let es: EventSource | null = null
const usingSse = ref(false)
function startSse() {
  if (es) return
  es = new EventSource(sseUrl('/downloads/events'))
  es.addEventListener('task_update', (e) => {
    usingSse.value = true
    error.value = ''
    stopPolling()
    const list = JSON.parse((e as MessageEvent).data) as DownloadTaskSummary[]
    applyTasks(list)
  })
  es.onopen = () => {
    usingSse.value = true
    error.value = ''
    stopPolling()
  }
  es.onerror = () => {
    usingSse.value = false
    startPolling()
  }
}

onMounted(() => {
  void loadTasks()
  // 缺口清单与任务列表并行取：视图切换徽标上的数字需要它，且它是下钻落点的数据源
  void loadFailures(true)
  void loadReDownloads(true)
  startSse()
  startPolling() // SSE 首帧到达前的兜底（收到首帧后自动停止）
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  if (es) {
    es.close()
    es = null
  }
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <div v-loading="loading">
    <!-- 视图切换：下载任务（任务级）/ 失败缺口（帖级）/ 可重下（帖级 gone）。
         三者量纲不同、互不覆盖，故分栏承载；资产卡下钻带 ?view=failures / ?view=re-downloads 直达 -->
    <div class="view-switch">
      <!-- 不用 v-model：view 是从 URL query 派生的只读 computed，写值统一走 setView 同步回 URL -->
      <el-segmented :model-value="view" :options="viewOptions" @change="setView" />
      <span class="view-hint text-muted">
        <template v-if="view === 'failures'">
          帖级口径：卡上「下载失败 N」= 此处 N 条
        </template>
        <template v-else-if="view === 're-downloads'">
          帖级口径：卡上「可重下 N」= 此处 N 条（曾下载成功、目录已被资源管理清理）
        </template>
        <template v-else>任务级口径：下方「失败任务」按任务计数，与「失败缺口」的帖数不同</template>
      </span>
    </div>

    <!-- ===== 下载任务视图 ===== -->
    <template v-if="view === 'tasks'">
    <!-- 统计卡片 -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background: #2f6fed">
          <el-icon><List /></el-icon>
        </div>
        <div>
          <div class="stat-label">任务总数</div>
          <div class="stat-value">{{ tasks.length }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #f59e0b">
          <el-icon><Loading /></el-icon>
        </div>
        <div>
          <div class="stat-label">进行中</div>
          <div class="stat-value">{{ activeCount }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #10b981">
          <el-icon><CircleCheck /></el-icon>
        </div>
        <div>
          <div class="stat-label">已完成</div>
          <div class="stat-value">{{ doneCount }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #ef4444">
          <el-icon><CircleClose /></el-icon>
        </div>
        <div>
          <!-- 明确「任务」二字：与「失败缺口」的帖数区分量纲（此前都叫「失败」，数字必然对不上） -->
          <div class="stat-label">失败任务</div>
          <div class="stat-value">{{ failedCount }}</div>
        </div>
      </div>
      <!-- 已暂停（非终态，仍需用户处置）/ 已取消（用户主动结束的终态）：
           后端共 6 种状态，统计卡须全部覆盖，否则「任务总数」与下方卡片之和对不上
           （此前只统计 4 种，漏掉这两种，表现为 100 != 99）。 -->
      <div class="stat-card">
        <div class="stat-icon" style="background: #e6a23c">
          <el-icon><Warning /></el-icon>
        </div>
        <div>
          <div class="stat-label">已暂停</div>
          <div class="stat-value">{{ pausedCount }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #909399">
          <el-icon><CircleClose /></el-icon>
        </div>
        <div>
          <div class="stat-label">已取消</div>
          <div class="stat-value">{{ cancelledCount }}</div>
        </div>
      </div>
    </div>

    <!-- 任务列表 -->
    <div class="page-card">
      <!-- D8 提交区：多行粘贴批量提交 -->
      <div class="submit-box">
        <div class="submit-title">新建下载任务</div>
        <el-input
          v-model="inputText"
          type="textarea"
          :rows="4"
          placeholder="粘贴下载链接，每行一个（也支持分号 / 逗号 / 空格分隔），支持 http/https 链接"
        />
        <div class="submit-meta">
          <span class="text-muted">
            <template v-if="inputText.trim()">
              共 <b>{{ parsedUrls.length }}</b> 个有效链接<template v-if="invalidLines.length">
                ，<span class="invalid-tip">{{ invalidLines.length }} 个无效行已忽略</span></template
              >
            </template>
            <template v-else>也可以从「帖子浏览」多选或「数据总览」热门榜直接发起下载</template>
          </span>
          <el-button type="primary" :disabled="!canSubmit" @click="submitUrls">提交下载</el-button>
        </div>
      </div>

      <!-- D3 状态筛选 + 批量开始/暂停 + D9 清空历史 + R2 实时通道标识 -->
      <div class="toolbar">
        <!-- 筛选项较多（6 个），窄屏放不下：外层可横向滑动，避免选项被裁掉 -->
        <div class="filter-scroll">
          <el-segmented v-model="filterStatus" :options="filterOptions" />
        </div>
        <span v-if="usingSse" class="sse-badge" title="服务端推送，进度亚秒级更新">实时推送</span>
        <div class="toolbar-right">
          <!-- 勾选数常驻占位改为「有勾选才显示」：无勾选时不留空标签 -->
          <span v-if="selectedIds.size" class="sel-hint">已勾选 {{ selectedIds.size }} 个</span>
          <!-- 单一主操作按钮（对齐迅雷）：有在途任务=「全部暂停」；
               无在途但有未完成任务=「全部开始」（默认作用于全部未完成任务，不必先勾选）
               或「开始下载」（已勾选未完成任务时收窄为只动勾选的）。
               按钮上的数字即本次点击的影响范围，作用域不必点下去才知道。 -->
          <el-tooltip :content="primaryTip" placement="top">
            <span class="tip-wrap">
              <el-button
                :type="pauseMode ? 'warning' : 'primary'"
                :disabled="primaryDisabled"
                @click="onPrimaryAction"
              >
                {{ primaryLabel }}<template v-if="primaryCount">（{{ primaryCount }}）</template>
              </el-button>
            </span>
          </el-tooltip>
          <!-- 只清「已完成」（done）任务：失败 / 已取消保留（2026-09-07 经用户确认变更）；
               自动轮转（TXXY_DOWNLOAD_TASK_MAX_KEEP）仍按全部终态裁剪以防持久化膨胀 -->
          <el-button class="clear-btn" @click="clearFinished">清空已完成</el-button>
        </div>
      </div>

      <div v-if="error" class="poll-error">
        轮询失败：{{ error }}（每 {{ REFRESH_INTERVAL / 1000 }} 秒自动重试）
      </div>

      <el-table v-if="!isMobile" :data="pagedTasks" size="default" style="width: 100%">
        <!-- 勾选列（自持选中集合，与移动端卡片同源）：表头复选框作用域 = 当前页 -->
        <el-table-column width="42" align="center">
          <template #header>
            <el-checkbox
              :model-value="pageAllSelected"
              :indeterminate="pageSomeSelected && !pageAllSelected"
              @change="toggleSelectPage"
            />
          </template>
          <template #default="{ row }">
            <el-checkbox :model-value="isSelected(row.id)" @change="toggleSelect(row.id)" />
          </template>
        </el-table-column>
        <el-table-column label="任务 ID" width="130">
          <template #default="{ row }">
            <span class="task-id" :title="row.id">{{ row.id.slice(0, 10) }}</span>
            <el-tag v-if="row.priority" size="small" type="success" class="prio-tag">置顶</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
              :stroke-width="8"
            >
              <span class="progress-text">{{ row.done }}/{{ row.total }}</span>
            </el-progress>
            <div v-if="row.speed != null" class="task-rate text-muted">
              <span>{{ row.speed }} 个/分</span>
              <span v-if="row.eta_sec">· 剩余 {{ formatDuration(row.eta_sec) }}</span>
            </div>
          </template>
        </el-table-column>
        <!-- show-overflow-tooltip：单行省略号 + 悬浮显示完整时间，杜绝换行 -->
        <el-table-column prop="created_at" label="创建时间" width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="showDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'pending'"
              link
              type="success"
              @click="prioritizeTask(row)"
            >
              优先执行
            </el-button>
            <!-- 「下载」= 下载该任务未完成的链接（工具栏「开始下载」的单任务版，
                 同一后端入口）：失败/已取消任务重跑未成功链接（原任务内更新进度），
                 已暂停任务继续剩余链接 -->
            <el-button
              v-if="STARTABLE.includes(row.status)"
              link
              type="warning"
              @click="downloadTask(row)"
            >
              下载
            </el-button>
            <el-button
              v-if="!TERMINAL.includes(row.status)"
              link
              type="danger"
              @click="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button v-if="TERMINAL.includes(row.status)" link type="danger" @click="deleteTask(row)">
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 移动端卡片：与桌面表格同源数据，主信息 + 副信息 + 操作（操作全部可见，
           不再被固定列宽截断；结构与 ResourcesView 的 file-cards 同一模式） -->
      <div v-else class="task-cards">
        <!-- 手机端补「全选本页」：桌面端靠表格表头勾选框，移动端卡片无对应控件，
             此前用户进手机端既看不到全选、开始按钮又默认禁用，无从批量开始。
             作用域与桌面表头勾选框一致（当前页），复用同一套选中集合。 -->
        <div class="tc-select-all">
          <el-checkbox
            :model-value="pageAllSelected"
            :indeterminate="pageSomeSelected && !pageAllSelected"
            @change="toggleSelectPage"
          >
            全选本页（{{ pagedTasks.length }}）
          </el-checkbox>
        </div>
        <div v-for="row in pagedTasks" :key="row.id" class="task-card">
          <div class="tc-head">
            <!-- 勾选框与桌面表格共用同一选中集合（list 勾选，不跟视图走） -->
            <el-checkbox
              class="tc-check"
              :model-value="isSelected(row.id)"
              @change="toggleSelect(row.id)"
            />
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
            <span class="tc-id" :title="row.id">{{ row.id.slice(0, 10) }}</span>
            <el-tag v-if="row.priority" size="small" type="success">置顶</el-tag>
          </div>
          <div class="tc-meta text-muted">
            <span>{{ row.created_at }}</span>
            <span>进度 {{ row.done }}/{{ row.total }}</span>
            <span v-if="row.speed != null">{{ row.speed }} 个/分</span>
            <span v-if="row.eta_sec">剩余 {{ formatDuration(row.eta_sec) }}</span>
          </div>
          <el-progress
            :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
            :stroke-width="6"
            class="tc-progress"
          />
          <div class="tc-ops">
            <el-button size="small" type="primary" link @click="showDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'pending'"
              size="small"
              type="success"
              link
              @click="prioritizeTask(row)"
            >
              优先执行
            </el-button>
            <!-- 「下载」= 下载该任务未完成的链接（工具栏「开始下载」的单任务版，同一后端入口） -->
            <el-button
              v-if="STARTABLE.includes(row.status)"
              size="small"
              type="warning"
              link
              @click="downloadTask(row)"
            >
              下载
            </el-button>
            <el-button
              v-if="!TERMINAL.includes(row.status)"
              size="small"
              type="danger"
              link
              @click="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button v-if="TERMINAL.includes(row.status)" size="small" type="danger" link @click="deleteTask(row)">
              删除
            </el-button>
          </div>
        </div>
        <el-empty
          v-if="!filteredTasks.length && !error"
          description="暂无下载任务，可在上方粘贴链接提交"
        />
      </div>

      <!-- 任务列表分页：仅超过一页时显示（桌面表格与移动卡片共用同一份切片数据）。
           注意必须放在 v-if/v-else 两个列表之后：v-else 要求与 v-if 相邻，
           分页条插在中间会把 v-else 错配到分页条的 v-if 上，导致两套列表同时渲染 -->
      <el-pagination
        v-if="filteredTasks.length > TASK_PAGE_SIZE"
        v-model:current-page="taskPage"
        :page-size="TASK_PAGE_SIZE"
        :total="filteredTasks.length"
        layout="total, prev, pager, next"
        class="tasks-pagination"
      />

      <el-empty v-if="!isMobile && tasks.length === 0 && !error" description="暂无下载任务，可在上方粘贴链接提交" />
    </div>
    </template>

    <!-- ===== 失败缺口视图（资产卡「下载失败」的下钻落点，与卡上数字同源同口径） ===== -->
    <div v-else-if="view === 'failures'">
      <div class="fail-head">
        <span class="text-muted">
          最近一次下载失败、此后未成功的帖：来自下载履历的持久记录，清空任务中心也不会消失。
          这是帖 / 链接数（一个失败任务可含多条失败链接），与「失败任务」的任务数不是同一口径。
        </span>
      </div>

      <!-- 筛选（与帖子浏览同款：带「关键词」标签的模糊搜索，跨 版块/标题/链接） -->
      <div class="page-card filter-bar">
        <div class="filter-row">
          <div class="filter-item grow">
            <span class="filter-label">关键词</span>
            <el-input
              v-model="failSearch"
              placeholder="搜索版块 / 标题 / 链接（模糊匹配）"
              clearable
              @keyup.enter="onFailSearch"
              @clear="onFailSearch"
            >
              <template #append>
                <el-button :icon="Search" @click="onFailSearch" />
              </template>
            </el-input>
          </div>
        </div>
      </div>

      <!-- 列表：与「帖子浏览」同一套「表格 + 顶/底双分页条」样式 -->
      <div class="page-card" style="margin-top: 16px">
        <template v-if="!isMobile">
          <!-- 顶部分页：与帖子浏览一致（长列表滚到顶也能翻页） -->
          <div class="pager pager-top">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredFailures.length"
              :current-page="failPage"
              :page-size="failPageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onFailPageChange"
              @size-change="onFailSizeChange"
            />
          </div>

          <el-table
            class="post-table"
            v-loading="failuresLoading"
            :data="pagedFailures"
            size="default"
            empty-text="暂无数据"
            style="width: 100%"
            @sort-change="onFailSortChange"
          >
            <!-- 标题：版块标签已合并进本列（与「帖子浏览」同款 .title-cell：标签前置 + 标题省略，
                 标题为空时回落显示链接路径）；表头按帖子浏览做法支持排序
                 （sortable="custom" + @sort-change 三态，实际排序见 onFailSortChange / sortItems） -->
            <el-table-column prop="title" label="标题" min-width="520" sortable="custom">
              <template #default="{ row }">
                <div class="title-cell">
                  <span v-if="row.fid_name" class="fid-chip" :style="{ '--fid-color': colorForFid(row.fid) }" :title="row.fid_name">{{ row.fid_name }}</span>
                  <a class="title-link" :href="postOpenUrl(row.url)" target="_blank" rel="noopener" :title="row.title || postPathOf(row.url) || row.url">{{ row.title || postPathOf(row.url) || row.url }}</a>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="fail_at" label="失败时间" min-width="120" show-overflow-tooltip sortable="custom">
              <template #default="{ row }"><span class="text-muted">{{ row.fail_at || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center" class-name="op-col">
              <template #default="{ row }">
                <div class="op-btns">
                  <el-tooltip content="重新下载" placement="top">
                    <el-button link type="success" :icon="Download" @click="redownloadByUrl(row.url)" />
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <span class="text-muted">{{ failSearch && filteredFailures.length === 0 && failures.length > 0 ? '没有匹配的帖子' : '没有失败缺口：下载过的帖子都已在本地有文件' }}</span>
            </template>
          </el-table>

          <!-- 底部分页：与顶部同状态 -->
          <div class="pager">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredFailures.length"
              :current-page="failPage"
              :page-size="failPageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onFailPageChange"
              @size-change="onFailSizeChange"
            />
          </div>
        </template>

        <!-- 窄屏卡片列表（isMobile）：主信息 + 副信息 + 操作，操作按钮全部可见、无横向滚动 -->
        <div v-else class="fail-cards">
          <div v-for="row in pagedFailures" :key="row.url" class="fail-card">
            <div class="fc-row">
              <span class="fc-k">标题</span>
              <span class="fc-v">
                <span v-if="row.fid_name" class="fid-chip" :style="{ '--fid-color': colorForFid(row.fid) }" :title="row.fid_name">{{ row.fid_name }}</span>
                <a class="title-link" :href="postOpenUrl(row.url)" target="_blank" rel="noopener" :title="row.title || postPathOf(row.url) || row.url">{{ row.title || postPathOf(row.url) || row.url }}</a>
              </span>
            </div>
            <div class="fc-row">
              <span class="fc-k">失败时间</span>
              <span class="fc-v text-muted">{{ row.fail_at || '-' }}</span>
            </div>
            <div class="fc-ops">
              <el-button size="small" type="warning" link @click="redownloadByUrl(row.url)">重新下载</el-button>
            </div>
          </div>
          <el-empty v-if="!filteredFailures.length" :description="failSearch && failures.length > 0 ? '没有匹配的帖子' : '没有失败缺口：下载过的帖子都已在本地有文件'" />
          <div v-else class="pager">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredFailures.length"
              :current-page="failPage"
              :page-size="failPageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onFailPageChange"
              @size-change="onFailSizeChange"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 可重下视图（资产卡「可重下」的下钻落点，与卡上数字同源同口径） =====
         刻意复用缺口清单的行结构（.fail-*）：两者都是「帖级一行 + 右侧重下按钮」，
         另起一套样式只会带来两处漂移（对齐、窄屏折行、配色都得改两遍） -->
    <div v-else>
      <div class="fail-head">
        <span class="text-muted">
          曾下载成功、此后目录被资源管理清理的帖：重新下载即可补回本地。
          与「失败缺口」互不重叠（那是下过但没下成），条数等于资产卡「可重下 N」。
        </span>
      </div>

      <!-- 筛选（与帖子浏览同款：带「关键词」标签的模糊搜索，跨 版块/标题/链接） -->
      <div class="page-card filter-bar">
        <div class="filter-row">
          <div class="filter-item grow">
            <span class="filter-label">关键词</span>
            <el-input
              v-model="reSearch"
              placeholder="搜索版块 / 标题 / 链接（模糊匹配）"
              clearable
              @keyup.enter="onReSearch"
              @clear="onReSearch"
            >
              <template #append>
                <el-button :icon="Search" @click="onReSearch" />
              </template>
            </el-input>
          </div>
        </div>
      </div>

      <!-- 列表：与「帖子浏览」「失败缺口」同一套「表格 + 顶/底双分页条」样式 -->
      <div class="page-card" style="margin-top: 16px">
        <template v-if="!isMobile">
          <div class="pager pager-top">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredReDownloads.length"
              :current-page="rePage"
              :page-size="rePageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onRePageChange"
              @size-change="onReSizeChange"
            />
          </div>

          <el-table
            class="post-table"
            v-loading="reDownloadsLoading"
            :data="pagedReDownloads"
            size="default"
            empty-text="暂无数据"
            style="width: 100%"
            @sort-change="onReSortChange"
          >
            <!-- 标题：版块标签已合并进本列（与「帖子浏览」同款 .title-cell：标签前置 + 标题省略，
                 标题为空时回落显示链接路径）；表头按帖子浏览做法支持排序
                 （sortable="custom" + @sort-change 三态，实际排序见 onReSortChange / sortItems） -->
            <el-table-column prop="title" label="标题" min-width="520" sortable="custom">
              <template #default="{ row }">
                <div class="title-cell">
                  <span v-if="row.fid_name" class="fid-chip" :style="{ '--fid-color': colorForFid(row.fid) }" :title="row.fid_name">{{ row.fid_name }}</span>
                  <a class="title-link" :href="postOpenUrl(row.url)" target="_blank" rel="noopener" :title="row.title || postPathOf(row.url) || row.url">{{ row.title || postPathOf(row.url) || row.url }}</a>
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="first_at" label="首次下载时间" min-width="120" show-overflow-tooltip sortable="custom">
              <template #default="{ row }"><span class="text-muted">{{ row.first_at || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="100" align="center" class-name="op-col">
              <template #default="{ row }">
                <div class="op-btns">
                  <el-tooltip content="重新下载" placement="top">
                    <el-button link type="success" :icon="Download" @click="redownloadByUrl(row.url)" />
                  </el-tooltip>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <span class="text-muted">{{ reSearch && filteredReDownloads.length === 0 && reDownloads.length > 0 ? '没有匹配的帖子' : '没有可重下的帖：下载过的帖子在本地都有文件' }}</span>
            </template>
          </el-table>

          <div class="pager">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredReDownloads.length"
              :current-page="rePage"
              :page-size="rePageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onRePageChange"
              @size-change="onReSizeChange"
            />
          </div>
        </template>

        <!-- 窄屏卡片列表（isMobile） -->
        <div v-else class="fail-cards">
          <div v-for="row in pagedReDownloads" :key="row.url" class="fail-card">
            <div class="fc-row">
              <span class="fc-k">标题</span>
              <span class="fc-v">
                <span v-if="row.fid_name" class="fid-chip" :style="{ '--fid-color': colorForFid(row.fid) }" :title="row.fid_name">{{ row.fid_name }}</span>
                <a class="title-link" :href="postOpenUrl(row.url)" target="_blank" rel="noopener" :title="row.title || postPathOf(row.url) || row.url">{{ row.title || postPathOf(row.url) || row.url }}</a>
              </span>
            </div>
            <div class="fc-row">
              <span class="fc-k">首次下载时间</span>
              <span class="fc-v text-muted">{{ row.first_at || '-' }}</span>
            </div>
            <div class="fc-ops">
              <el-button size="small" type="warning" link @click="redownloadByUrl(row.url)">重新下载</el-button>
            </div>
          </div>
          <el-empty v-if="!filteredReDownloads.length" :description="reSearch && reDownloads.length > 0 ? '没有匹配的帖子' : '没有可重下的帖：下载过的帖子在本地都有文件'" />
          <div v-else class="pager">
            <el-pagination
              background
              layout="total, sizes, prev, pager, next, jumper"
              :total="filteredReDownloads.length"
              :current-page="rePage"
              :page-size="rePageSize"
              :page-sizes="[20, 50, 100, 200]"
              @current-change="onRePageChange"
              @size-change="onReSizeChange"
            />
          </div>
        </div>
      </div>
    </div>

    <!-- 任务详情抽屉（R1：明细按需加载） -->
    <el-drawer
      v-model="detailVisible"
      :title="`任务详情 ${detailTask?.id ?? detailId}`"
      :size="isMobile ? '100%' : '55%'"
      v-loading="detailLoading"
    >
      <template v-if="detailTask">
        <div class="detail-summary">
          <el-descriptions :column="2" size="small" border>
            <el-descriptions-item label="状态">
              <el-tag size="small" :type="statusTagType(detailTask.status)">
                {{ statusText(detailTask.status) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="进度">{{ detailTask.done }}/{{ detailTask.total }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ detailTask.created_at }}</el-descriptions-item>
            <el-descriptions-item label="开始时间">{{ detailTask.started_at || '-' }}</el-descriptions-item>
            <el-descriptions-item label="结束时间">{{ detailTask.finished_at || '-' }}</el-descriptions-item>
            <el-descriptions-item label="取消标记">
              {{ detailTask.cancel_requested ? '是' : '否' }}
            </el-descriptions-item>
          </el-descriptions>
        </div>

        <div class="detail-title">执行日志</div>
        <el-input
          ref="logsBoxRef"
          :model-value="parsedLogs.events.join('\n')"
          type="textarea"
          :rows="8"
          readonly
          class="logs-box"
        />

        <div class="detail-title">
          按链接明细（{{ parsedLogs.groups.length }}）
          <span v-if="parsedLogs.hiddenTotal" class="follow-tip">
            仅显示最近 {{ MAX_DETAIL_LINES }} 行（共 {{ parsedLogs.detailTotal }} 行）
          </span>
          <span v-if="!followLatest" class="follow-tip">已暂停跟随，滚到底部恢复</span>
        </div>
        <!-- 固定高度的日志窗：内容增长时自动跟随到底部（业界 CI 日志面板做法），
             用户上翻看历史时暂停跟随，避免把正在阅读的内容顶走 -->
        <div
          ref="detailScrollRef"
          class="detail-log-scroll"
          @scroll="onDetailScroll"
        >
          <el-collapse
            v-if="parsedLogs.groups.length"
            v-model="expandedLogs"
            class="log-groups"
          >
            <el-collapse-item
              v-for="g in parsedLogs.groups"
              :key="g.seq"
              :name="g.seq"
            >
              <template #title>
                <span class="log-group-title">[{{ g.seq }}] {{ g.title }}</span>
                <!-- 正在写入的组：任务运行中且是最后活动的组，便于一眼定位最新输出 -->
                <el-tag v-if="g.active" size="small" type="success" class="log-group-tag">
                  进行中
                </el-tag>
              </template>
              <pre class="log-detail">{{ g.lines.join('\n') }}</pre>
            </el-collapse-item>
          </el-collapse>
          <div v-else class="text-muted log-empty">暂无下载过程明细</div>
        </div>

        <!-- 链接明细：几十条起步，按业界 CI 做法处理——
             先给统计与状态筛选，异常项优先排序，列表固定高度内部滚动（不撑长抽屉） -->
        <div class="detail-title">
          链接明细（{{ itemStats.total }}）
          <span class="items-summary">
            成功 {{ itemStats.ok }} · 失败 {{ itemStats.fail }} · 取消 {{ itemStats.cancelled }} ·
            跳过 {{ itemStats.skip }}
          </span>
        </div>
        <div class="items-filter">
          <el-radio-group v-model="itemFilter" size="small">
            <el-radio-button value="all">全部（{{ itemStats.total }}）</el-radio-button>
            <el-radio-button value="problem">
              异常（{{ itemStats.problem }}）
            </el-radio-button>
            <el-radio-button value="ok">成功（{{ itemStats.ok }}）</el-radio-button>
          </el-radio-group>
          <span class="text-muted items-hint">异常项排在最前，可直接「重新下载」</span>
        </div>
        <el-table :data="pagedItems" size="small" border height="300">
          <el-table-column type="index" label="#" width="46" :index="itemIndex" />
          <el-table-column label="URL" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">{{ row.url }}</template>
          </el-table-column>
          <el-table-column label="状态" width="82">
            <template #default="{ row }">
              <el-tag size="small" :type="itemTagType(row.status)">{{ itemText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" min-width="130" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else-if="Object.keys(row.stats || {}).length">
                {{ Object.entries(row.stats).map(([k, v]) => `${k} ${v}`).join(', ') }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="72">
            <template #default="{ row }">
              <span>{{ row.elapsed != null ? `${row.elapsed}s` : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="88" fixed="right">
            <template #default="{ row }">
              <!-- 成功/跳过无需重下；running/pending 正在处理不可重下 -->
              <el-tooltip :content="itemRetryTip(row.status)" placement="top">
                <!-- 包裹层不可省：disabled 按钮不派发鼠标事件，tooltip 会静默不显示 -->
                <span class="tip-wrap">
                  <el-button
                    link
                    type="primary"
                    :disabled="itemRetryDisabled(row.status)"
                    @click="retryItem(row)"
                  >
                    重新下载
                  </el-button>
                </span>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
        <!-- 明细分页：纯前端切片（数据已在内存），仅超过一页时显示 -->
        <el-pagination
          v-if="filteredItems.length > ITEM_PAGE_SIZE"
          v-model:current-page="itemPage"
          :page-size="ITEM_PAGE_SIZE"
          :total="filteredItems.length"
          layout="total, prev, pager, next"
          size="small"
          class="items-pagination"
        />
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 下载中心 6 张状态卡：桌面一行 6 列；窄屏逐级折行自适应（覆盖全局 .stat-grid 的 3 列）。
   作用域限定本组件，不影响资源页/看板的栅格。 */
.stat-grid {
  grid-template-columns: repeat(6, 1fr);
}

/* 桌面（>768px）始终保持 6 列一行；仅平板/手机逐级折行。
   注意：内容区已减去左侧导航，实际宽度常 < 1280，故折叠断点要低于常见桌面宽度，
   否则会过早折成 3 列（之前 1280 断点即此坑）。 */
@media (max-width: 768px) {
  .stat-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 520px) {
  .stat-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 380px) {
  .stat-grid {
    grid-template-columns: 1fr;
  }
}

/* 视图切换（下载任务 / 失败缺口）+ 当前视图的口径提示：窄屏换行显示，不裁剪选项 */
.view-switch {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.view-hint {
  font-size: 12px;
}

/* ===== 失败缺口 / 可重下 两清单的筛选栏（与帖子浏览同款：带「关键词」标签的模糊搜索） ===== */
.filter-bar {
  display: flex;
  flex-wrap: wrap;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  width: 100%;
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-item.grow {
  flex: 1;
  min-width: 220px;
}

.filter-label {
  color: #606266;
  font-size: 13px;
  white-space: nowrap;
}

/* 标题链接（.title-link）、版块标签（.fid-chip）等清单表格单元样式，
   与「帖子浏览」共用全局 style.css 的「帖子浏览系清单表格共用单元样式」段，
   本文件不再重复定义，避免两处漂移。 */

/* 窄屏（isMobile，<768px）：失败缺口 / 可重下清单改为单列卡片
   （与任务列表 .task-cards 同一模式），操作按钮全部可见、不产生横向滚动条 */
.fail-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 8px;
}

.fail-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 8px;
}

.fail-card .fc-row {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
  line-height: 1.5;
}

.fail-card .fc-k {
  flex: 0 0 56px;
  color: #909399;
  font-size: 12px;
}

.fail-card .fc-v {
  flex: 1 1 auto;
  min-width: 0;
  word-break: break-all;
}

.fail-card .fc-ops {
  display: flex;
  justify-content: flex-end;
  margin-top: 2px;
}

/* 标题为空时回落显示链接路径：已随标题统一走全局 .title-link 配色，
   不再单独定义 .fail-name-url。 */

/* ===== 失败缺口 / 可重下 清单：与「帖子浏览」同一套「表格 + 顶/底双分页条」样式 ===== */
.fail-head {
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.6;
}

/* 分页条：与帖子浏览一致——组件撑满卡片宽度，total 靠左、翻页控件靠右。
   顶部分页紧贴筛选/表格上方，底部分页对称置于表格下方 */
.pager {
  margin-top: 14px;
  display: flex;
}

.pager :deep(.el-pagination) {
  width: 100%;
}

.pager :deep(.el-pagination__total) {
  margin-right: auto;
}

.pager-top {
  margin-top: 12px;
  margin-bottom: 0;
}

.submit-box {
  border: 1px dashed var(--app-border, #dcdfe6);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}

.submit-title {
  font-weight: 600;
  font-size: 14px;
  color: #1f2d3d;
  margin-bottom: 8px;
}

.submit-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
}

.invalid-tip {
  color: #ef4444;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

/* 状态筛选项较多（6 个），窄屏放不下时由 .filter-scroll 横向滑动；
   该类已抽到全局 style.css（资源管理页同用），此处不再重复定义。 */

/* 右侧操作组：勾选提示 + 主操作按钮（开始下载 / 全部暂停）+ 清空已完成 */
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  flex-wrap: wrap;
}

.sel-hint {
  font-size: 12px;
  color: #909399;
}

/* 禁用按钮上的 tooltip 需要一层可命中的包裹元素：
   disabled 的 button 不派发鼠标事件，直接给它挂 tooltip 会静默不显示 */
.tip-wrap {
  display: inline-flex;
}

.poll-error {
  color: #ef4444;
  font-size: 12px;
  margin-bottom: 8px;
}

.task-id {
  font-family: Consolas, monospace;
  font-size: 12px;
}

.prio-tag {
  margin-left: 4px;
}

.progress-text {
  font-size: 12px;
  color: #606266;
}

.detail-summary {
  margin-bottom: 12px;
}

.detail-title {
  font-weight: 600;
  font-size: 13px;
  color: #1f2d3d;
  margin: 12px 0 6px;
}

.logs-box :deep(textarea) {
  font-family: Consolas, monospace;
  font-size: 12px;
}

/* 分组标题旁的「进行中」标记：与标题基线对齐，不撑高折叠行 */
.log-group-tag {
  margin-left: 6px;
}

/* ================= 移动端适配 =================
   与 ResourcesView 的 file-cards 同一模式：断点沿用布局层 isMobile（<768px），
   表格列宽固定 + 右侧固定列在窄屏会横向溢出、操作列被截去大半，
   故小屏改为单列卡片（主信息 + 副信息 + 操作），操作按钮全部可见。 */
.task-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

/* 手机端「全选本页」：与桌面表头勾选框同一作用域（当前页），承接批量操作的勾选入口 */
.tc-select-all {
  display: flex;
  align-items: center;
  padding: 4px 2px 2px;
  font-size: 13px;
  color: #606266;
}

.task-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 56px;
  padding: 10px 12px;
  background: #fafcff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
}

.tc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 卡片勾选框：与左侧文字对齐，且高度不加高整行 */
.tc-check {
  height: auto;
}

.tc-id {
  font-family: Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
}

.tc-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
}

.tc-progress {
  margin: 2px 0;
}

/* 操作区允许换行：保证按钮全部显示，不会被裁掉 */
.tc-ops {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  border-top: 1px dashed #ebeef5;
  padding-top: 6px;
}

/* 「按链接明细」日志窗：固定高度 + 内部滚动，新日志自动跟随到底部 */
.detail-log-scroll {
  height: 260px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 4px 8px;
  background: #fafcff;
}

.follow-tip {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #e6a23c;
}

.log-empty {
  font-size: 12px;
  padding: 8px 2px;
}

.log-trim-tip {
  color: #909399;
  font-size: 12px;
  padding: 2px 0 4px;
}

/* 链接明细：统计摘要 + 状态筛选（几十条时先给结论、再给筛选） */
.items-summary {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}

.items-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

/* 明细分页条：与表格保持紧凑间距 */
.items-pagination {
  margin-top: 8px;
  justify-content: flex-end;
}

/* 任务列表分页条 */
.tasks-pagination {
  margin-top: 10px;
  justify-content: flex-end;
}

.items-hint {
  font-size: 12px;
}

@media (max-width: 768px) {
  /* 手机屏高度紧张，日志窗略矮 */
  .detail-log-scroll {
    height: 200px;
  }

  /* 操作组独占一行：主操作按钮在手机上更好点，不被筛选项挤到折行之外 */
  .toolbar-right {
    width: 100%;
  }
}

/* 按链接折叠的下载过程明细（等宽，与任务日志区一致） */
.log-groups :deep(.log-group-title) {
  font-family: Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}

.log-groups :deep(.log-detail) {
  font-family: Consolas, monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  padding: 4px 8px;
  background: #fafafa;
  border-radius: 4px;
}

.error-text {
  color: #ef4444;
}

/* R2 SSE 实时推送标识 */
.sse-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #10b981;
}

.sse-badge::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 0 3px rgba(16, 185, 129, 0.18);
}

/* R4 进度条平滑过渡：消除推送/轮询粒度带来的台阶感 */
:deep(.el-progress-bar__inner) {
  transition: width 0.6s ease;
}
</style>
