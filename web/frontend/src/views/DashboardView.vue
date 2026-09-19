<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, nextTick, watch, type ShallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { graphic, init as echartsInit, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, EffectScatterChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TooltipComponent,
} from 'echarts/components'
import type { ECharts } from 'echarts/core'
import { ElMessage } from 'element-plus'
import { Download, FolderOpened, Star } from '@element-plus/icons-vue'
import { useDownloadSubmit } from '../composables/useDownloadSubmit'
import { useAssets } from '../composables/useAssets'
import { api, formatDuration, formatSize, isAborted, type Boards, type BoardItemState, type BoardSort, type Compare, type FidDistItem, type Health, type Overview, type PendingDownloads, type RunSummary, type ScheduleAction, type ScheduleStatus, type TodayTop, type TodayTopItem, type TopAuthor, type TopFid, type TrendByFid, type TrendPoint } from '../api'
import { useDashboardStore } from '../stores/dashboard'
import { useAppStore } from '../stores/app'
import { formatDate, formatShortTime, pad2 } from '../utils/time'
import { colorByIndex, colorForFid } from '../utils/fidColor'
import { stateBadge } from '../utils/downloadState'
import { postOpenUrl } from '../utils/postUrl'
import RollingNumber from '../components/RollingNumber.vue'

use([
  CanvasRenderer,
  BarChart,
  LineChart,
  EffectScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  // 标线组件必须显式注册：ECharts 按需引入模式下未注册的组件会被静默忽略
  // （2026-09-13 修复：分版块图「联动聚焦日」的 markLine 此前从未注册，标线实际不渲染；
  //  互动量趋势图的「今日未完整」虚线同样依赖它）
  MarkLineComponent,
  // 同上教训（规则 17）：markArea（趋势图空窗标注）是新引入的组件，必须显式注册
  MarkAreaComponent,
])

const router = useRouter()

// 内容资产（媒体库沉淀进度）共享逻辑：数据总览只取「精简 KPI 摘要」所需的子集，
// 完整卡片在资源管理页承接（同一份实现，禁止两处各写一份，见 composables/useAssets.ts）
const { assets, loadAssets, assetsEmpty, goalBarWidth, goalTip, goalAria, goResources, goPendingPosts, goDownloadFailures } = useAssets()

const store = useDashboardStore()
const app = useAppStore()

// 页面根容器：ResizeObserver 的观察目标。侧栏折叠 / 移动抽屉 / 进入全屏都不会改变
// 窗口尺寸，仅靠 window.resize 无法驱动 ECharts 重排，必须由容器尺寸变化驱动
const rootRef = ref<HTMLDivElement | null>(null)

/**
 * ECharts Tooltip 挂载配置：
 * - 常态挂 body 顶层（appendToBody），防止被卡片裁切
 * - 真全屏时浏览器只渲染全屏元素及其后代，挂 body 的 tooltip 不可见，
 *   改为就地挂到全屏元素内
 * - 降级伪全屏是文档流内的 fixed 覆盖层，body 弹层仍可见，维持 appendToBody
 */
function tipMount(): { appendToBody: boolean; appendTo?: () => HTMLElement } {
  if (app.fullscreen && !app.pseudoFullscreen) {
    return { appendToBody: false, appendTo: () => document.fullscreenElement as HTMLElement }
  }
  return { appendToBody: true }
}

/** 真全屏时 ElMessage 挂在 body 上不可见：先退出全屏再提示，保证用户能看到反馈 */
async function notifyError(msg: string): Promise<void> {
  if (app.fullscreen && !app.pseudoFullscreen) await app.exitFullscreen()
  ElMessage.error(msg)
}

/** 成功提示同理：全屏态点击榜单「下载」后需要看到创建结果 */
async function notifySuccess(msg: string): Promise<void> {
  if (app.fullscreen && !app.pseudoFullscreen) await app.exitFullscreen()
  ElMessage.success(msg)
}

/** 警告提示同理（判重剔除提示用） */
async function notifyWarning(msg: string): Promise<void> {
  if (app.fullscreen && !app.pseudoFullscreen) await app.exitFullscreen()
  ElMessage.warning(msg)
}

// ===== P0：首屏区块 =====
const overview = ref<Overview | null>(null)
const trend = ref<TrendPoint[]>([])
const fidDist = ref<FidDistItem[]>([])
const loadingP0 = ref(false)

// ===== P1：懒加载区块（热门榜）=====
const boards = ref<Boards | null>(null)
const todayTop = ref<TodayTop | null>(null)
const monthTop = ref<TodayTop | null>(null)
const loadingBoards = ref(false)

// 最新最热 / 本月最热的排序维度（两卡独立记忆），切换只重拉对应榜单
const todaySort = ref<BoardSort>('engagement')
const monthSort = ref<BoardSort>('engagement')
const loadingToday = ref(false)
const loadingMonth = ref(false)
const p1AreaRef = ref<HTMLDivElement | null>(null)

// ===== R1-R4：采集健康条 / 周期对比 / 资产漏斗 / 待下载推荐（后端接口已就绪，前端接入）=====
const health = ref<Health | null>(null)
const compare = ref<Compare | null>(null)
const pending = ref<PendingDownloads | null>(null)
const loadingPending = ref(false)
// B1：定时抓取状态（GET /api/schedule，与设置页同源）。健康条报告抓取的「结果」，
// 这里补齐「计划」——自动抓取是否活着、下次几点跑、今天跑了几轮，一眼可见
const sched = ref<ScheduleStatus | null>(null)

let trendObserver: IntersectionObserver | null = null
let p1Observer: IntersectionObserver | null = null

const trendRef = shallowRef<HTMLDivElement | null>(null)
const trendChart = shallowRef<ECharts | null>(null)

// 每日互动量趋势（与「全站发布趋势」同窗口、同一天数，趋势区第三张图）：
// 2026-09-13 从「本月最热」卡内抽出——榜单卡只做列表，互动量趋势单独成图。
// 数据量小（近 28 日实测 66ms），随首屏 P0 一起加载，不做懒加载。
const trendEng = ref<TrendPoint[]>([])
const trendEngRef = shallowRef<HTMLDivElement | null>(null)
const trendEngChart = shallowRef<ECharts | null>(null)
const trendEngCardRef = ref<HTMLDivElement | null>(null)
let lastTrendEngKey = ''

// ===== 活跃作者 / 活跃版块 榜（随首屏加载，横向条形图）=====
/** 活跃榜统计口径：all=累计 / 7d=近 7 日 / 30d=近 30 日 */
type RankRange = 'all' | '7d' | '30d'
const topAuthors = ref<TopAuthor[]>([])
const topFids = ref<TopFid[]>([])
const authorRange = ref<RankRange>('all')
const fidRange = ref<RankRange>('all')

/** 口径中文名，用于卡片副标题与 tooltip（两卡共用，保持文案一致） */
const RANGE_LABEL: Record<RankRange, string> = {
  all: '累计',
  '7d': '近 7 日',
  '30d': '近 30 日',
}

/**
 * 按当前统计口径给出下钻的时间范围，使榜单数字与帖子页结果自洽：
 * 在「近 7 日」榜上看到 431 条，点进去就应是这 431 条，而不是该作者的全部 4314 条。
 * all 口径返回 null（不限制日期，等价于看全部）。
 * 「近 N 日」= 含今天往前 N 天，与后端 _top_rank 的算法口径一致。
 */
function rankDateRange(range: RankRange): { date_from: string; date_to: string } | null {
  if (range === 'all') return null
  const days = range === '7d' ? 7 : 30
  const fmt = formatDate // 日期格式化复用 utils/time，不在本文件内联补零拼接
  const to = new Date()
  const from = new Date()
  from.setDate(from.getDate() - (days - 1))
  return { date_from: fmt(from), date_to: fmt(to) }
}
// B1 抓取中徽标：最新一条 running 运行记录（null 表示当前无批次在跑）
const runningBatch = ref<RunSummary | null>(null)
const authorChartRef = shallowRef<HTMLDivElement | null>(null)
const fidChartRef = shallowRef<HTMLDivElement | null>(null)
const authorChart = shallowRef<ECharts | null>(null)
const fidChart = shallowRef<ECharts | null>(null)
let lastAuthorKey = ''
let lastFidKey = ''

// 默认近 7 天（与趋势 tooltip 自动轮播的起始维度一致）
const trendDays = ref(7)
// 天数切换选项（下拉框，支持预设 + 自定义输入）
const trendDayOptions = ref([7, 14, 21, 28])

// ===== 分版块每日趋势（多系列折线，懒加载）=====
// 分版块天数由全站趋势联动下钻控制，不再单独切换
const fidTrend = ref<TrendByFid>({ dates: [], series: [] })
const fidTrendCache = new Map<number, TrendByFid>()
const loadingFidTrend = ref(false)
const fidTrendSwitching = ref(false)
const fidTrendVisible = ref(false)
const fidTrendRef = ref<HTMLDivElement | null>(null)
const fidTrendChart = shallowRef<ECharts | null>(null)
let fidTrendTipPaused: boolean = false
let fidTrendObserver: IntersectionObserver | null = null
let fidTrendTipTimer: number | null = null
// 联动：分版块图例点击高亮的版块（同步到总趋势卡片配色）
const linkedFid = ref<{ name: string; color: string } | null>(null)
// 分版块名称 -> 颜色 映射（渲染时填充）
const fidColorByName = ref<Record<string, string>>({})
// 反向联动：点总趋势某天 -> 分版块同天高亮（垂直标线）
const linkedDay = ref<string | null>(null)
// 联动区间：单指标趋势图（全站发布 / 每日互动量）的缩放条（dataZoom）选区。
// 两张单指标图双向同步视窗；分版块图按同一日期区间裁剪
// （后端 trend_by_fid 只支持 days=最近N天，无日期区间参数，故在前端按选区裁剪已加载的数据）
const trendZoomRange = ref<{ start: string; end: string } | null>(null)

/**
 * 选区徽标文案：同年只显示 MM-DD（紧凑），**跨年必须带年份**。
 * 365 天窗口必然跨年（如 2025-09-30 ~ 2026-08-27），只取 MM-DD 会读成「起止倒序」
 * （实测徽标曾显示「跟随趋势区间：09-30 ~ 08-27」，看起来像选区反了）。
 */
const zoomRangeLabel = computed(() => {
  const r = trendZoomRange.value
  if (!r) return ''
  return r.start.slice(0, 4) === r.end.slice(0, 4)
    ? `${r.start.slice(5)} ~ ${r.end.slice(5)}`
    : `${r.start} ~ ${r.end}`
})

// ===== 单卡片全屏（复用 useAppStore 的元素级全屏，与整页「大屏」共用状态）=====
const trendCardRef = ref<HTMLDivElement | null>(null)
const fidTrendCardRef = ref<HTMLDivElement | null>(null)
/** 当前处于卡片全屏的卡片：伪全屏降级时需要它来加 fixed 覆盖层样式 */
const fsCard = ref<'trend' | 'fid' | 'eng' | null>(null)

/** 分版块卡片渲染出来后（overview 加载完成）再挂懒加载 observer。
 *  mounted 时卡片尚未渲染（骨架屏分支），直接 observe(null) 会静默失败。
 *  观察目标就是卡片根元素（fidTrendCardRef，与卡片全屏共用同一元素）。 */
watch(
  fidTrendCardRef,
  (el) => {
    if (!el || fidTrendObserver || !('IntersectionObserver' in window)) return
    fidTrendObserver = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          fidTrendObserver?.disconnect()
          fidTrendObserver = null
          fidTrendVisible.value = true
          loadFidTrend()
        }
      },
      { rootMargin: '200px 0px' },
    )
    fidTrendObserver.observe(el)
  },
  { immediate: true },
)



// 自动刷新：开关状态存于 dashboard store（header 控件共享），每 5 秒静默刷新一次
// 仅刷新已加载的区块，未进入视口的懒加载区块保持不动
const REFRESH_INTERVAL = 5000
// 活跃榜（活跃作者 / 活跃版块）窄屏断点：低于该容器宽度走「紧凑留白」配置，
// 否则桌面的 74px 类目标签 + 92px 条尾会把柱条压到只剩约 44% 宽
const RANK_NARROW_W = 400
// 当前是否已按窄屏配置渲染（null = 尚未渲染），用于跨断点时强制重绘
let rankNarrow: boolean | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null
// 页面可见性：后台隐藏时暂停全部轮询与轮播动画，恢复可见时立即刷新并重启
let pageVisible = true
let refreshing = false

/**
 * 每日趋势统计（峰值 / 谷值 / 日均）：**排除最后一天（今天）**。
 *
 * 今天只含当日凌晨批次的抓取，数据天然不完整——实测发布量 322 / 前一日 1007（32%）、
 * 互动量 1,215 / 前一日 12,207（10%）。若纳入计算，「谷值」会永远落在今天、日均被系统性拉低，
 * 统计卡失去参考价值；图表上今天的点单独做「未完整」标注（见 buildDayLineOption）。
 * 发布量与互动量两张图共用此实现，避免并排两卡对同一指标各算一套口径。
 */
function daySeriesStats(points: TrendPoint[]) {
  const full = points.slice(0, -1) // 去掉今天
  if (!full.length) return null
  const values = full.map((t) => t.value)
  const max = Math.max(...values)
  const min = Math.min(...values)
  const total = values.reduce((s, v) => s + v, 0)
  return {
    max,
    min,
    maxDate: full[values.indexOf(max)]?.date ?? '',
    minDate: full[values.indexOf(min)]?.date ?? '',
    total,
    avg: Math.round(total / values.length),
  }
}

const trendStats = computed(() => daySeriesStats(trend.value))
const trendEngStats = computed(() => daySeriesStats(trendEng.value))

// 指标卡副指标：环比 / 活跃率 / 数据新鲜度
const kpiSub = computed(() => {
  const o = overview.value
  if (!o) return null
  const diff = o.today - o.yesterday
  const pct = o.yesterday > 0 ? Math.abs((diff / o.yesterday) * 100).toFixed(1) : null
  const todayDiff =
    pct === null
      ? { cls: 'sub-neutral', text: '昨日无数据' }
      : diff >= 0
        ? { cls: 'sub-up', text: `较昨日 ↑ ${pct}%` }
        : { cls: 'sub-down', text: `较昨日 ↓ ${pct}%` }
  const activeShare = o.total_users > 0 ? ((o.active_users / o.total_users) * 100).toFixed(1) : null
  // 数据新鲜度：最近入库活动时间（run_days 最新批次）距今天数；
  // 2026-08-27 起 posts.date 为帖子真实发布日、不随跑批推进，新鲜度改用入库活动时间
  let gapText = '暂无数据'
  let gapCls = 'sub-neutral'
  if (o.latest_run_at) {
    const gap = daysBetween(o.latest_run_at.slice(0, 10))
    if (gap <= 0) gapText = '今天'
    else if (gap === 1) gapText = '昨天'
    else gapText = `${gap} 天前`
    gapCls = gap <= 1 ? 'sub-up' : gap <= 3 ? 'sub-neutral' : 'sub-down'
  }
  return {
    todayDiff,
    activeShare,
    gap: { cls: gapCls, text: gapText },
    // 主值显示最近入库日期（随批次推进）；发布日口径由趋势图承载
    latestDate: o.latest_run_at ? o.latest_run_at.slice(0, 10) : '',
    // 兼容旧 ISO 字符串与新 Unix 秒时间戳两种形态，统一换算为 HH:MM
    updatedAt: formatShortTime(o.latest_run_at),
  }
})

/**
 * 发帖作者卡悬浮：只放口径说明 + 常驻位没展示的那个值（近 30 日新增），
 * 不再重复今日活跃 / 活跃率（已回常驻副行，第 20 条「一处事实一处表达」）。
 * 口径写明「近 N 个完整日（不含今天）」——今天抓取未覆盖全天，计入会被稀释（第 18 条）。
 */
const authorTip = computed(() => {
  const o = overview.value
  if (!o) return ''
  return (
    `近 7 个完整日（不含今天）首次出现的作者，含只发 1 帖的新面孔；` +
    `近 30 日新增 ${o.new_authors_30d.toLocaleString()} 人`
  )
})

/** 最新数据日期与今天相差的天数（大于 0 表示滞后）。 */
function daysBetween(dateStr: string): number {
  const d = new Date(`${dateStr}T00:00:00`)
  if (Number.isNaN(d.getTime())) return 0
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.round((today.getTime() - d.getTime()) / 86400000)
}

// ===== R1-R4：健康条 / 周期对比 / 资产漏斗的派生数据 =====

/**
 * 周期环比文案（R2）：涨绿 / 跌红 / 持平灰 / 前值为 0 无基准时橙「新增」。
 * 返回 text 不带前缀，调用方按场景自行拼接（累计收录卡带「近7日」前缀，趋势卡头不带）。
 */
function cmpDelta(delta: number | null | undefined): { cls: string; color: string; text: string } {
  if (delta === null || delta === undefined) return { cls: 'sub-new', color: '#f59e0b', text: '新增' }
  if (delta === 0) return { cls: 'sub-neutral', color: '#909399', text: '持平' }
  return delta > 0
    ? { cls: 'sub-up', color: '#10b981', text: `↑${delta}%` }
    : { cls: 'sub-down', color: '#ef4444', text: `↓${Math.abs(delta)}%` }
}

/** 近 7 日环比（累计收录卡副指标）：与活跃榜 7d 环比同一滚动窗口口径 */
const weekCmp = computed(() => {
  if (!compare.value) return null
  const d = cmpDelta(compare.value.week.delta)
  return { ...d, text: `近7日 ${d.text}` }
})

/** 近 7 日环比（全站趋势卡头统计条）：与分版块趋势 tooltip 的 7 日环比同源同窗，
 *  避免「全站看 30 日、分版块看 7 日」的口径不对称 */
const trendCmp = computed(() => (compare.value ? cmpDelta(compare.value.week.delta) : null))

/** 最活版块的近 7 日环比（分版块卡头统计卡）：与全站卡头的周期环比形成对称，
 *  逐版块的环比明细另附在分版块 tooltip 每行尾部（数据来自 trend_by_fid 的 delta） */
const fidTopDelta = computed(() => {
  const s = fidTrendStats.value
  return s ? cmpDelta(s.topDelta) : null
})

/**
 * 今日发布 KPI 下钻：跳帖子页并锁定「今日」日期窗（date_from=date_to=today_str）。
 * 卡片主值 = posts.date == 今天的条数，下钻列表用同一日期窗过滤，保证「卡片 N 帖 = 列表 N 帖」口径自洽。
 * 复用 goPostsInRange（与「最新最热」同口径的日期并入逻辑），排序按发布时间倒序（最新在前），贴合「今日发布」语义。
 */
function goTodayPosts() {
  const today = overview.value?.today_str
  if (!today) return
  goPostsInRange({ sort: 'date_desc' }, dayRange(today))
}

/** 健康条补充信息（R1）：悬浮展示批次明细（点击进运行记录页） */
const healthDetail = computed(() => {
  const h = health.value
  if (!h) return ''
  const parts: string[] = []
  if (h.run_date) parts.push(`批次 ${h.run_date}${h.run_time ? ` ${h.run_time}` : ''}`)
  if (h.duration != null) parts.push(`耗时 ${formatDuration(h.duration)}`)
  if (h.success_rate != null) parts.push(`版块成功率 ${h.success_rate}%`)
  if (h.latest_date) parts.push(`最新发布日 ${h.latest_date}`)
  parts.push('点击查看运行记录')
  return parts.join(' · ')
})

// ===== P0：首屏加载（KPI + 趋势 + 分布）=====
async function loadP0(initial = false) {
  if (initial) loadingP0.value = true
  try {
    // 健康条 / 周期对比 / 定时抓取状态：随首屏并行加载（不阻塞主数据 await）；
    // 失败静默保留旧值（与 runningBatch 徽标同一容错策略），下一轮刷新自动重试
    void Promise.allSettled([
      api.health(),
      api.compare(),
      api.schedule(),
    ]).then(([h, c, s]) => {
      if (h.status === 'fulfilled') health.value = h.value
      if (c.status === 'fulfilled') compare.value = c.value
      if (s.status === 'fulfilled') sched.value = s.value
    })
    void loadAssets()
    const [o, t, te, f, authors, fids] = await Promise.all([
      api.overview(),
      api.trend(trendDays.value),
      // 互动量趋势与发布趋势同窗口、同一次请求发出（互相等待无意义，且保证两图横轴对齐）
      api.trend(trendDays.value, 'engagement'),
      api.fidDist(),
      api.topAuthors(10, authorRange.value),
      api.topFids(10, fidRange.value),
    ])
    overview.value = o
    trend.value = t
    trendEng.value = te
    fidDist.value = f
    topAuthors.value = authors
    topFids.value = fids
    trendCache.set(trendDays.value, t)
    trendEngCache.set(trendDays.value, te)
    // B1 抓取中徽标（B1）：失败不影响总览主流程，静默置空
    try {
      const runs = await api.runs()
      runningBatch.value = runs.dates.find((r) => r.status === 'running') ?? null
    } catch {
      runningBatch.value = null
    }
    store.setUpdatedAt(o.latest_run_at ?? null)
    await nextTick()
    renderTrendChart()
    renderTrendEngChart()
    renderAuthorChart()
    renderFidChart()
    // 首屏：等折线逐点描线动画完成后再启动趋势 tooltip 轮播（非首屏自动刷新不中断当前轮播）
    if (initial) startTrendCarousel(trend.value.length * 24 + 900)
  } catch (e) {
    if (isAborted(e)) return
    if (!initial) return // 轮询失败静默，下轮自动重试
    void notifyError(`加载总览数据失败: ${(e as Error).message}`)
  } finally {
    loadingP0.value = false
  }
}

// 上一次加载 boards 时对应的黑名单版本号；初始 -1 保证首屏必加载
const lastBoardsVersion = ref(-1)

// ===== P1：懒加载热门榜（点赞/回复/最新最热/本月最热）=====
async function loadBoards(force = false) {
  // boards 是 Pinia 跨页面导航持久缓存：若仅按 boards.value 非空早退，
  // 「设置页改完黑名单再回看板」会因缓存未失效而显示旧榜单（含已屏蔽帖）。
  // 故改为校验黑名单版本号——版本不变且不强制则早退（沿用「不参与轮询刷新」设计），
  // 版本变了（设置页增删过黑名单）则必须重拉，确保热门榜即时同步。
  if (!force && store.blacklistVersion === lastBoardsVersion.value) return
  if (loadingBoards.value) return
  loadingBoards.value = true
  try {
    const [b, tt, mt] = await Promise.all([
      api.boards(),
      api.todayTop(10, todaySort.value),
      api.monthTop(10, monthSort.value),
    ])
    boards.value = b
    todayTop.value = tt
    monthTop.value = mt
    lastBoardsVersion.value = store.blacklistVersion
  } catch (e) {
    if (isAborted(e)) return
    if (boards.value) return // 轮询刷新（榜单已存在）失败静默，下轮重试
    void notifyError(`加载热门榜失败: ${(e as Error).message}`)
  } finally {
    loadingBoards.value = false
  }
}

// 黑名单变更（设置页增删后 bumpBlacklist）→ 强制重拉全部卡片口径。
// 后端 posts_filtered 视图已过滤，这里专门解决「点赞/回复最高帖等卡片不参与轮询刷新」导致的不同步。
watch(
  () => store.blacklistVersion,
  () => {
    loadBoards(true)
    reloadTodayTop()
    reloadMonthTop()
    loadPending()
    loadP0(false)
  },
)

/**
 * R3 待下载推荐：近 30 日互动量最高、且尚未下载到本地的帖子。
 * 与 loadBoards 分开实现：loadBoards 首次加载后即早退（热门榜不参与轮询刷新），
 * 而待下载列表必须随下载进度动态更新——刚下载完成的帖子要退出推荐位。
 */
async function loadPending() {
  if (loadingPending.value) return
  loadingPending.value = true
  try {
    // 取 10 条：与「本月最热」同为 10 条，列表超出 360px 即滚动（.board-list 同款）
    pending.value = await api.pendingDownloads(10, 30)
    // 本卡与「每日互动量趋势」同行等高：清单行数决定整行高度，左侧图表区（flex 自适应）
    // 的可用高度随之变化。ECharts 不会自己监听容器尺寸，容器变高后画布停在旧高度会露白，
    // 故每次拿到新清单后主动让它重新量一次尺寸。
    await nextTick()
    trendEngChart.value?.resize()
  } catch (e) {
    if (isAborted(e)) return
    if (pending.value) return // 已有数据，轮询失败静默，下轮重试
    void notifyError(`加载待下载推荐失败: ${(e as Error).message}`)
  } finally {
    loadingPending.value = false
  }
}

/**
 * 下载类写操作成功后的「定向刷新」：只刷新**数据依赖下载状态**的卡——
 * ① 待下载推荐（提交后该帖变「在途」，必须立即退出推荐位，而不是等下一轮 5s 轮询）；
 * ② 内容资产卡（在途 / 已沉淀 / 缺口 / 覆盖率随之变化）。
 *
 * 刻意**不刷新**榜单四卡（点赞最高帖 / 回复最多帖 / 最新最热 / 本月最热）：它们按点赞 /
 * 回复 / 互动量排序，与「是否已下载」无关，下载后重拉只会得到同样的数据，反而会打断
 * 列表滚动位置与 NEW 标。这正是业界「按依赖关系定向失效」的用法（React Query
 * invalidateQueries / Apollo refetchQueries：写操作只失效受其影响的查询，不做全量重拉）。
 */
function refreshDownloadDependent() {
  void loadPending()
  void loadAssets()
}

/** 榜单排序维度 → 帖子页 sort 参数，保证下钻后列表顺序与榜单一致 */
const BOARD_SORT_TO_POSTS: Record<BoardSort, string> = {
  engagement: 'engagement_desc',
  likes: 'likes_desc',
  replies: 'replies_desc',
  hot: 'hot_desc',
}

/** 排序维度下拉的候选项（与后端 _BOARD_SORTS 一一对应） */
const BOARD_SORT_OPTIONS: { value: BoardSort; label: string }[] = [
  { value: 'engagement', label: '综合' },
  { value: 'likes', label: '点赞' },
  { value: 'replies', label: '回复' },
  { value: 'hot', label: '热度' },
]

/** 各排序维度的口径说明，进卡头 tooltip，避免「按什么排」靠猜 */
const BOARD_SORT_HINT: Record<BoardSort, string> = {
  engagement: '点赞+回复',
  likes: '点赞数',
  replies: '回复数',
  hot: '时间衰减热度（同分下越新越靠前）',
}

// 下载状态标（已沉淀 / 下载中 / 可重下）的映射与取用统一走 utils/downloadState，
// 与帖子浏览列表共用同一份，禁止两处各写一份（项目通用工程约束第 1 条）。

/** 行内下载按钮 tooltip 按状态切换：已沉淀 / 在途时提醒判重行为，其余为普通「下载」 */
function downloadTip(state?: BoardItemState): string {
  if (state === 'downloaded') return '重新下载（文件已在本地，判重会拦截已存在的文件）'
  if (state === 'running') return '下载任务进行中（重复提交会被剔除）'
  return '下载'
}

async function reloadTodayTop() {
  loadingToday.value = true
  try {
    todayTop.value = await api.todayTop(10, todaySort.value)
  } catch (e) {
    if (isAborted(e)) return
    void notifyError(`切换「最新最热」排序失败: ${(e as Error).message}`)
  } finally {
    loadingToday.value = false
  }
}

async function reloadMonthTop() {
  loadingMonth.value = true
  try {
    monthTop.value = await api.monthTop(10, monthSort.value)
  } catch (e) {
    if (isAborted(e)) return
    void notifyError(`切换「本月最热」排序失败: ${(e as Error).message}`)
  } finally {
    loadingMonth.value = false
  }
}



/** 切换排序：只重拉对应榜单，不整页刷新 */
function onTodaySortChange(v: BoardSort) {
  todaySort.value = v
  void reloadTodayTop()
}

function onMonthSortChange(v: BoardSort) {
  monthSort.value = v
  void reloadMonthTop()
}

/** 互动率（回复/点赞）：<0.3 围观型（高赞低回）、≥1 热议型（讨论度高于点赞） */
function replyRate(item: TodayTopItem) {
  const likes = Number(item.likes ?? 0)
  const replies = Number(item.replies ?? 0)
  if (likes <= 0) return replies > 0 ? Number.POSITIVE_INFINITY : 0
  return replies / likes
}

/** 互动率说明（进 tooltip）：解释「它凭什么排在这」 */
function rateText(item: TodayTopItem) {
  const r = replyRate(item)
  const shown = Number.isFinite(r) ? r.toFixed(2) : '∞'
  const kind = r >= 1 ? '热议型' : r >= 0.3 ? '均衡型' : '围观型'
  return `互动率 ${shown}（回复 ${item.replies} / 点赞 ${item.likes}）· ${kind}`
}

/** 仅热议型显示行内「热议」小标，否则每行都挂标签等于没有标签 */
function isHotTalk(item: TodayTopItem) {
  const r = replyRate(item)
  return Number.isFinite(r) ? r >= 1 : Number(item.replies ?? 0) > 0
}

/** P1-8：各图表数据指纹缓存，数据未变化时跳过重复 setOption，避免轮询期间空重绘 */
let lastTrendKey = ''
let lastFidTrendKey = ''

/**
 * 单指标每日折线图的 option 构造（「全站发布趋势」与「每日互动量趋势」共用，唯一实现）。
 *
 * 共用而非各写一份：两图除口径文案与配色外完全同构（网格线规范、tooltip 皮肤、轴配置、
 * 数据缩放、「今日未完整」标注）。硬编码两份意味着这些规范每次调整都要改两处，必然漏改一处。
 *
 * 今日点处理：窗口最后一天是「今天」，只含当日凌晨批次的抓取，数据天然不完整
 * （实测发布量 322 / 前一日 1007、互动量 1,215 / 前一日 12,207）。故对该点：
 * ① 用 markArea 打一层浅色底标出「这一格数据未完整」；② tooltip 追加说明。
 * 统计卡（峰值/谷值/日均）另在 daySeriesStats 里排除今天。
 */
function buildDayLineOption(
  points: TrendPoint[],
  opts: {
    seriesName: string
    valueLabel: string
    unit: string
    color: string
    /** 联动聚焦日（YYYY-MM-DD）：任一张图点击某天后，三张图都画同日标线 */
    markDate?: string | null
  },
): Record<string, unknown> {
  const data = points.map((p) => p.value)
  const needZoom = points.length > 31
  const lastIdx = data.length - 1
  const lastLabel = points[lastIdx]?.date.slice(5) ?? ''
  const { color, unit } = opts
  // 标线集合：① 今日未完整（末端，橙虚线）；② 联动聚焦日（蓝实线）。
  // 聚焦日与末端同一天时不重复画——末端线已在那里，避免两条线叠在一起。
  // X 轴是 MM-DD 分类轴（days ≤ 365，窗口内各 MM-DD 唯一），故用标签定位即可。
  const focusLabel = opts.markDate ? opts.markDate.slice(5) : ''
  const marks: Record<string, unknown>[] = []
  if (lastLabel) marks.push({ xAxis: lastLabel })
  if (focusLabel && focusLabel !== lastLabel) {
    marks.push({
      xAxis: focusLabel,
      lineStyle: { color: '#2f6fed', type: 'solid', width: 1 },
      label: {
        show: true,
        formatter: '聚焦日',
        color: '#2f6fed',
        fontSize: 10,
        position: 'insideEndTop',
      },
    })
  }
  // B3（原 R10）：空窗标注——连续 0 值日打浅灰底带，让「数据断档」在图上一眼可见。
  // 0 值有两种含义（站点当天确实没动静 / 抓取缺勤），图上不替用户下结论，
  // tooltip 里引导对照健康条；末日（今天）不标——它已有「今日未完整」橙虚线，0 属常态而非空窗。
  const gapAreas: Record<string, unknown>[][] = []
  {
    let runStart = -1
    for (let i = 0; i <= lastIdx; i++) {
      const isZero = i < lastIdx && data[i] === 0
      if (isZero && runStart < 0) runStart = i
      if (runStart >= 0 && !isZero) {
        const end = i - 1
        const s = points[runStart]?.date.slice(5)
        const e = points[end]?.date.slice(5)
        const n = end - runStart + 1
        if (s && e) {
          gapAreas.push([
            {
              xAxis: s,
              // 单日空窗太窄放不下文字，只留底色；连续 2 天以上才标「空窗 N 天」
              label: {
                show: n >= 2,
                formatter: `空窗 ${n} 天`,
                position: 'insideTop',
                color: '#98a3b3',
                fontSize: 10,
              },
            },
            { xAxis: e },
          ])
        }
        runStart = -1
      }
    }
  }
  return {
    // 大屏态关闭过渡动画：图表放大后重绘成本更高，避免逐点描线拖慢轮询
    animation: !app.fullscreen,
    tooltip: {
      trigger: 'axis',
      ...tipMount(),
      z: 99999,
      // tooltip 皮肤与「分版块发布对比」保持一致（富格式内容保留）
      backgroundColor: 'rgba(20,28,48,0.92)',
      borderColor: 'rgba(255,255,255,0.12)',
      borderWidth: 1,
      textStyle: { color: '#e6ebf5', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(0,0,0,0.35)' } },
      formatter: (params: any[]) => {
        const p = params[0]
        if (!p) return ''
        const idx = p.dataIndex
        const cur = data[idx]
        const trendUp = idx > 0 && cur >= data[idx - 1]
        const diffColor = trendUp ? '#10b981' : '#ef4444'
        const trendIcon = trendUp ? '▲' : '▼'
        let html = `<div style="font-weight:600;font-size:13px;color:#a8c5ff;margin-bottom:6px">${p.axisValue}</div>`
        html += `<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">`
        html += `<span style="color:#8b95a7;font-size:12px">${opts.valueLabel}</span>`
        html += `<span style="font-size:20px;font-weight:700;color:#fff;font-variant-numeric:tabular-nums">${cur.toLocaleString()}</span>`
        if (unit) html += `<span style="color:#8b95a7;font-size:12px">${unit}</span>`
        html += `</div>`
        if (idx > 0) {
          const prev = data[idx - 1]
          const diff = cur - prev
          const pct = prev > 0 ? ((diff / prev) * 100).toFixed(1) : '—'
          html += `<div style="display:flex;align-items:center;gap:4px;font-size:12px;margin-bottom:2px">`
          html += `<span style="color:#8b95a7">较上日</span>`
          html += `<span style="color:${diffColor};font-weight:600">${trendIcon} ${diff >= 0 ? '+' : ''}${diff}</span>`
          html += `<span style="color:${diffColor};opacity:0.85">(${diff >= 0 ? '+' : ''}${pct}%)</span>`
          html += `</div>`
        }
        if (idx >= 6) {
          const slice = data.slice(idx - 6, idx + 1)
          const weekAvg = Math.round(slice.reduce((s, v) => s + v, 0) / slice.length)
          html += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.1);font-size:12px;color:#8b95a7">`
          html += `<span>7日均</span> <span style="color:#a8c5ff;font-weight:600">${weekAvg.toLocaleString()}</span>`
          html += unit ? ` <span>${unit}</span>` : ''
          html += `</div>`
        }
        if (idx === lastIdx) {
          html += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.1);font-size:11px;color:#f0b775">`
          html += `今日数据未完整（当日抓取尚未覆盖全天），不计入峰值/谷值/日均`
          html += `</div>`
        }
        // B3：空窗日提示——0 值不替用户定性，只给出「对照健康条」的判断路径
        if (idx < lastIdx && cur === 0) {
          html += `<div style="margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,0.1);font-size:11px;color:#98a3b3">`
          html += `空窗日（${opts.valueLabel}为 0）：健康条正常则为站点当日真实无动静，否则疑似抓取缺勤`
          html += `</div>`
        }
        return html
      },
    },
    grid: { left: 44, right: 20, top: 30, bottom: needZoom ? 46 : 28 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: points.map((t) => t.date.slice(5)),
      axisLine: { lineStyle: { color: 'rgba(0,0,0,0.25)' } },
      axisTick: { show: false },
      axisLabel: { color: '#6b7280', fontSize: 11, hideOverlap: true },
      // 不显示 X 轴（竖向）网格线
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      min: 0,
      minInterval: 1,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#6b7280', fontSize: 11, formatter: (v: number) => String(Math.round(v)) },
      // 显示 Y 轴（横向）网格线
      splitLine: { show: true, lineStyle: { color: 'rgba(0,0,0,0.08)' } },
    },
    dataZoom: needZoom
      ? [
          { type: 'inside', start: 0, end: 100 },
          { type: 'slider', height: 18, bottom: 8, start: 0, end: 100 },
        ]
      : [],
    series: [
      {
        name: opts.seriesName,
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 4,
        showSymbol: false,
        emphasis: { focus: 'series' },
        // 允许「点折线」触发 click（默认 false，且本图 showSymbol: false 不渲染符号——
        // 两者叠加会让「点击某天联动」实际上点不中任何东西，实测扫描点击全部落空）
        triggerLineEvent: true,
        data,
        lineStyle: { width: 2, color, shadowColor: color, shadowBlur: 6 },
        itemStyle: { color },
        areaStyle: {
          opacity: 0.06,
          color: new graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color },
            { offset: 1, color: 'rgba(255,255,255,0)' },
          ]),
        },
        // 标注：① 今日未完整（末端橙虚线）；② 联动聚焦日（蓝实线）。
        // 两条线同属 markLine，用 data 数组 + 逐项 lineStyle/label 覆盖（数组第一项的样式
        // 由 series.markLine 上的同名配置提供）。
        // 实测教训：最初用 markArea 打浅色底，但 markArea 的 data 必须是「坐标对」
        // （[[起点, 终点]]），只给单个对象会让 ECharts 读 undefined.coord 抛错，
        // 进而中断后续渲染（当时活跃榜两张图也一起空白）。markLine 接受单个对象，语义也够。
        markLine: marks.length
          ? {
              silent: true,
              symbol: 'none',
              label: {
                show: true,
                formatter: '今日未完整',
                color: '#c98a2b',
                fontSize: 10,
                position: 'end',
              },
              lineStyle: { color: '#e6a23c', type: 'dashed', width: 1 },
              data: marks,
            }
          : undefined,
        // B3：空窗浅灰底带。markArea 的 data 必须是「坐标对」[[起点, 终点]]（见上方实测教训），
        // 且组件 MarkAreaComponent 必须已在 use() 注册——按需引入下未注册会静默不渲染（规则 17）
        markArea: gapAreas.length
          ? {
              silent: true,
              itemStyle: { color: 'rgba(144,147,153,0.13)' },
              data: gapAreas,
            }
          : undefined,
      },
    ],
  }
}

function renderTrendChart() {
  if (trendRef.value) {
    // P1-8：数据指纹（含联动配色 / 聚焦日依赖），无变化跳过 setOption
    const trendKey =
      trend.value.map((t) => `${t.date}:${t.value}`).join('|') +
      `|${linkedFid.value?.color ?? ''}|${linkedDay.value ?? ''}`
    if (trendKey === lastTrendKey) return
    lastTrendKey = trendKey

    trendChart.value ??= initChart(trendRef.value)
    // 双向 Tooltip 联动：全站趋势 ⟷ 分版块（仅注册一次）
    if (!trendTipSynced) {
      trendTipSynced = true
      trendChart.value.on('showTip', (params: any) => syncTipTo(fidTrendChart.value, params))
      trendChart.value.on('hideTip', () => {
        if (tipSyncing) return
        tipSyncing = true
        fidTrendChart.value?.dispatchAction({ type: 'hideTip' })
        tipSyncing = false
      })
      // 点某天 → 三图都标该日（linkedDay 是唯一状态，另两图点某天同样走 focusDay）。
      // 点击后暂停本图 tooltip 轮播：用户正在看某天，不要被轮播抢走
      bindPlotClick(
        trendChart.value,
        () => trend.value.map((t) => t.date),
        () => {
          trendTipPaused = true
        },
      )
      // 缩放条（dataZoom）选区联动：拖选后分版块图按同一区间裁剪，
      // 此前未监听该事件，导致选了区间后另一张图纹丝不动
      trendChart.value.on('datazoom', () => applyTrendZoom())
    }
    // 联动配色：当分版块图例聚焦某版块时，全站趋势同步换为该版块色
    const base = linkedFid.value?.color ?? '#2f6fed'
    trendChart.value.setOption(
      buildDayLineOption(trend.value, {
        seriesName: '发布帖子',
        valueLabel: '发布',
        unit: '条',
        color: base,
        markDate: linkedDay.value,
      }),
    )
  }
}

/**
 * 每日互动量趋势（与「全站发布趋势」同窗口、同一天数）。
 *
 * 口径：互动量 = 点赞 + 回复（与热门榜排序口径同源，后端 stats_trend 的 engagement 分支）。
 * 定位：回答「这个站哪天最热闹」，与「全站发布趋势」（发帖量 = 供给侧活动）并排对照。
 *
 * 联动边界（2026-09-13 修正）：
 * - **不参与**「全站 ⟷ 分版块」的 Tooltip 双向联动与自动轮播——三图同时弹 tooltip 会让
 *   联动逻辑与视觉失控，本图独立读数即可；
 * - **参与**缩放选区联动：它与全站图是同一类单指标折线（共用 buildDayLineOption、
 *   同样在 >31 点时出现滑动条），而「三图同一窗口、横轴严格对齐」是既有承诺，
 *   可视区间也必须一致 —— 此前该图完全没接线（既不监听 datazoom 也不消费 trendZoomRange），
 *   表现为同日同样输入 365 天、全站拖动滑动条后只有分版块联动、本图纹丝不动。
 *   现在与全站图**双向同步视窗**（applyTrendZoom）。
 */
function renderTrendEngChart() {
  if (!trendEngRef.value) return
  const key = trendEng.value.map((t) => `${t.date}:${t.value}`).join('|') + `|${linkedDay.value ?? ''}`
  if (key === lastTrendEngKey) return
  lastTrendEngKey = key
  trendEngChart.value ??= initChart(trendEngRef.value)
  // 缩放选区联动：三图双向同步视窗（仅注册一次，避免数据刷新后重复绑定累积）
  if (!trendEngBound) {
    trendEngBound = true
    trendEngChart.value.on('datazoom', () => applyTrendZoom('eng'))
    // 聚焦日联动同样是三图双向：本图点某天 → 三图同标该日
    // （Tooltip 联动仍刻意不参与，见上方函数头说明）
    bindPlotClick(trendEngChart.value, () => trendEng.value.map((t) => t.date))
  }
  trendEngChart.value.setOption(
    buildDayLineOption(trendEng.value, {
      seriesName: '互动量',
      valueLabel: '互动量',
      unit: '',
      color: '#8b5cf6',
      markDate: linkedDay.value,
    }),
  )
}

function goDist(fid?: string) {
  if (fid) {
    router.push({ path: '/posts', query: { fid, ...(rankDateRange(fidRange.value) ?? {}) } })
  }
}

/** R1 健康条下钻：查看运行记录明细 */
function goRuns() {
  router.push('/runs')
}

/** B1：定时抓取徽标点击去设置页（计划时刻与开关在那里维护） */
function goSettings() {
  router.push('/settings')
}

/** B2：资产条「下载失败」入口跳下载中心（带 view=failures 直达同口径的失败缺口清单） */
function goDownloads() {
  goDownloadFailures()
}

/** 调度动作的中文短文案（与设置页口径一致，此处仅用于悬浮说明） */
const SCHEDULE_ACTION_TEXT: Record<ScheduleAction, string> = {
  started: '已启动批次',
  skipped: '跳过（上一批仍在跑）',
  missed: '错过（当时服务未运行）',
  failed: '启动失败',
}

/** 健康条定时抓取徽标：启用 → 「定时 08:00/20:00 · 下次 今天 20:00 · 今日 1/2」；未启用 → 灰字提示 */
const schedLabel = computed(() => {
  const s = sched.value
  if (!s) return ''
  if (!s.enabled || !s.times.length) return '定时抓取未启用'
  return `定时 ${s.times.join('/')} · 下次 ${shortNextRun(s.next_run_at)} · 今日 ${s.today_done.length}/${s.times.length}`
})

const schedTip = computed(() => {
  const s = sched.value
  if (!s) return ''
  if (!s.enabled || !s.times.length)
    return '定时抓取未启用：大屏数据只随手动抓取更新。点击前往设置页启用。'
  const last = s.last
    ? `上次判定 ${s.last.at.slice(5, 16)}：${SCHEDULE_ACTION_TEXT[s.last.action]}${s.last.reason ? `（${s.last.reason}）` : ''}`
    : '服务启动后还没有产生过调度判定'
  return `定时抓取只在 Web 服务存活时执行，错过不补跑。「今日 n/N」为已处理的计划时刻数（含跳过/错过）。${last}。点击前往设置页调整计划时刻。`
})

/** next_run_at "YYYY-MM-DD HH:MM" → 「今天 20:00」/「明天 08:00」/「09-21 08:00」 */
function shortNextRun(v: string | null): string {
  if (!v) return '--'
  const [d, t] = v.split(' ')
  const now = new Date()
  const dayStr = (ms: number) => {
    const x = new Date(now.getTime() + ms)
    return `${x.getFullYear()}-${pad2(x.getMonth() + 1)}-${pad2(x.getDate())}`
  }
  if (d === dayStr(0)) return `今天 ${t}`
  if (d === dayStr(86_400_000)) return `明天 ${t}`
  return `${d.slice(5)} ${t}`
}

/** B2：磁盘低位判据——剩余不足总量 10% 或不足 20 GB（本机存媒体，盘满是头号风险） */
const diskLow = computed(() => {
  const a = assets.value
  if (!a || !a.disk_total) return false
  return a.disk_free < a.disk_total * 0.1 || a.disk_free < 20 * 1024 ** 3
})





function initChart(el: HTMLDivElement): ECharts {
  return echartsInit(el)
}

/** 排名榜横向条形图通用渲染：Top-N 主数值 + 副指标 tooltip，可指定点击回调 */
/** 环比展示：null=前 7 日无基准（视为新增），0=持平，其余为百分比 */
function deltaText(delta: number | null | undefined): { text: string; cls: string } {
  if (delta === null || delta === undefined) return { text: '新增', cls: 'new' }
  if (delta === 0) return { text: '持平', cls: 'flat' }
  return delta > 0
    ? { text: `↑${delta}%`, cls: 'up' }
    : { text: `↓${Math.abs(delta)}%`, cls: 'down' }
}

function renderHBarChart(
  el: HTMLDivElement,
  chart: ShallowRef<ECharts | null>,
  lastKeyRef: { v: string },
  items: {
    name: string
    value: number
    extra: string
    delta?: number | null
    valueLabel?: string
  }[],
  colors: string[],
  onClick?: (i: number) => void,
  forceRedraw = false,
) {
  // 指纹纳入口径：切换口径后即使数值相同也要重绘
  const key = items.map((d) => `${d.name}:${d.value}:${d.valueLabel ?? ''}`).join('|')
  if (key === lastKeyRef.v && !forceRedraw) return // P1-8：数据指纹无变化跳过重绘
  lastKeyRef.v = key
  // 窄屏判定以「图表容器实际宽度」为准（比 window 宽度可靠：侧栏/全屏都会改变容器）
  const narrow = (el.clientWidth || window.innerWidth) < RANK_NARROW_W
  const c = chart.value ??= initChart(el)
  c.setOption(
    {
      animation: !app.fullscreen,
      tooltip: {
        trigger: 'item',
        // 项目规范：Tooltip 顶层；真全屏时改挂到全屏元素内（挂 body 不显示）
        ...tipMount(),
        // 鼠标不可进入 tooltip，移除后立即隐藏——避免 appendToBody 下 tooltip DOM
        // 残留在 body 内导致「鼠标移开后 tooltip 不消失」的观感
        enterable: false,
        hideDelay: 0,
        transitionDuration: 0,
        z: 99999,
        formatter: (p: any) => {
          const label = p.data.valueLabel ?? '当前'
          const d = deltaText(p.data.delta)
          const cmp =
            d.cls === 'new'
              ? '环比：新增（前 7 日无数据）'
              : d.cls === 'flat'
                ? '环比：与前一个 7 日持平'
                : `环比：${d.text}（近 7 日 vs 前 7 日）`
          return `${p.name}<br/>${label} ${p.value.toLocaleString()} 条<br/>${p.data.extra ?? ''}<br/>${cmp}`
        },
      },
      // 右侧留足条尾「数值 + 环比」的空间，避免被裁切。
      // 窄屏（画布 < RANK_NARROW_W）按自适应收窄：收紧类目标签（56px）与右留白（84px），
      // 既不让柱条被压到一半，也保证「数值 + 环比」始终在条尾完整显示（不再挪进柱体内部）。
      grid: narrow
        ? { left: 4, right: 84, top: 6, bottom: 6, containLabel: true }
        : { left: 8, right: 92, top: 6, bottom: 6, containLabel: true },
      // Y 轴横向线显示，X 轴竖向线隐藏（项目图表网格线规则）
      xAxis: {
        type: 'value',
        min: 0,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#909399', fontSize: 11 },
        splitLine: { show: true, lineStyle: { color: 'rgba(0,0,0,0.08)' } },
      },
      yAxis: {
        type: 'category',
        inverse: true, // 第一名在顶部
        data: items.map((d) => d.name),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: narrow
          ? { color: '#1f2d3d', fontSize: 11, width: 56, overflow: 'truncate' }
          : { color: '#1f2d3d', fontSize: 12, width: 74, overflow: 'truncate' },
        splitLine: { show: false },
      },
      series: [
        {
          type: 'bar',
          barWidth: 12,
          data: items.map((d, i) => ({
            value: d.value,
            extra: d.extra,
            delta: d.delta,
            valueLabel: d.valueLabel,
            // colorByIndex / colorForFid 均为取模取值，不会越界，无需兜底色
            itemStyle: { color: colors[i], borderRadius: [0, 6, 6, 0] },
          })),
          label: {
            show: true,
            position: 'right',
            distance: 4,
            // 条尾同时给出主值与环比：涨跌用颜色区分，一眼看出谁在上升。
            // 窄屏同样在条尾显示（仅字号略小），靠 grid 右留白（84px）保证「数值 + 环比」完整不裁切。
            formatter: (p: any) => {
              const d = deltaText(p.data.delta)
              return `{v|${p.value.toLocaleString()}}  {${d.cls}|${d.text}}`
            },
            rich: {
              v: { color: '#606266', fontSize: narrow ? 10 : 11 },
              up: { color: '#10b981', fontSize: narrow ? 10 : 11 },
              down: { color: '#ef4444', fontSize: narrow ? 10 : 11 },
              flat: { color: '#909399', fontSize: narrow ? 10 : 11 },
              new: { color: '#f59e0b', fontSize: narrow ? 10 : 11 },
            },
          },
        },
      ],
    },
    true,
  )
  if (onClick) {
    c.off('click')
    c.on('click', (p: any) => {
      if (p.componentType === 'series') onClick(p.dataIndex)
    })
  }
}

/** 活跃作者 Top10：横向条形图，主值=累计发帖，多色区分，点击下钻该作者帖子。
 *  作者没有稳定 fid，按排名取色；复用 FID_PALETTE（见 utils/fidColor），
 *  不另建一套作者色板，避免同一色值在两图里的排名含义不一致。 */
function renderAuthorChart(force = false) {
  if (!authorChartRef.value) return
  const n = topAuthors.value.length
  if (!n) return
  const colors = topAuthors.value.map((_, i) => colorByIndex(i))
  renderHBarChart(
    authorChartRef.value,
    authorChart,
    { v: lastAuthorKey },
    topAuthors.value.map((a) => ({
      name: a.author,
      value: a.value,
      extra: `累计 ${a.total} · 今日 ${a.today} · 近 7 日 ${a.week} · 近 30 日 ${a.month}`,
      delta: a.delta,
      valueLabel: RANGE_LABEL[authorRange.value],
    })),
    colors,
    (i) => goAuthor(topAuthors.value[i]?.author ?? ''),
    force,
  )
}

/** 活跃作者下钻：按作者精确过滤，并继承当前统计口径的时间范围 */
function goAuthor(author: string) {
  if (!author) return
  router.push({ path: '/posts', query: { author, ...(rankDateRange(authorRange.value) ?? {}) } })
}

/**
 * 通用下钻：跳到帖子浏览页，带 fid/sort 等过滤条件贴合原卡片场景。
 * 注意：此函数【不带时间语义】——目前仅被「点赞/回复最高帖」这类「全站历史最高」卡片使用（无时间范围）。
 * 若未来被「最新最热 / 本月最热 / 活跃榜」等有时间窗口的入口复用，必须在此并入 rankDateRange(...)，
 * 否则会出现「榜单显示 431 条，点进去却看到 4314 条全部」的口径不一致问题。
 */
function goPostsWith(query: Record<string, string>) {
  router.push({ path: '/posts', query })
}

/**
 * 带时间窗的下钻：跳到帖子浏览页并自动并入起止日期。
 * 用于「最新最热」（当日）与「本月最热」（当月）这类有明确时间语义的卡片——
 * 榜单只统计该时间窗内的帖，下钻后的列表必须限定同一时间窗，
 * 否则会出现「榜单 10 条、点进去却看到全库」的口径矛盾。
 */
function goPostsInRange(
  query: Record<string, string>,
  range: { from: string; to: string } | null,
) {
  if (!range) return
  router.push({ path: '/posts', query: { date_from: range.from, date_to: range.to, ...query } })
}

/** 最新最热的时间窗：最新数据日期当天 */
function dayRange(date?: string) {
  return date ? { from: date, to: date } : null
}

/**
 * 本月最热的时间窗：最新数据月份的月初~月末。
 * 注意用接口返回的 month（=最新数据所属月），不能用当前时间——
 * 跨月边界时（今天 9/1 但最新数据还是 8/31）「本月」指的是 8 月而非 9 月。
 */
function monthRange(month?: string) {
  if (!month) return null
  const [y, m] = month.split('-').map(Number)
  if (!y || !m) return null
  const lastDay = new Date(y, m, 0).getDate() // 下月第 0 天 = 本月最后一天
  return { from: `${month}-01`, to: `${month}-${pad2(lastDay)}` }
}

/** 相对今天的文案：0→今天，1→昨天，n→N 天前 */
function relDayText(date?: string) {
  if (!date) return ''
  const d = new Date(`${date}T00:00:00`)
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000)
  if (diff <= 0) return '今天'
  if (diff === 1) return '昨天'
  return `${diff} 天前`
}

/** 互动量（点赞+回复）：热门榜的排序依据，显式展示以免用户看不懂榜单顺序 */
function engagement(item: { likes?: number; replies?: number }) {
  // Number() 兜底：互动数字段在不同接口口径下可能是字符串，直接 + 会变成拼接
  return Number(item.likes ?? 0) + Number(item.replies ?? 0)
}

/** 活跃版块 Top10：横向条形图，主值=累计发帖，颜色按版块色板，点击跳版块列表 */
function renderFidChart(force = false) {
  if (!fidChartRef.value) return
  const n = topFids.value.length
  if (!n) return
  renderHBarChart(
    fidChartRef.value,
    fidChart,
    { v: lastFidKey },
    topFids.value.map((f) => ({
      name: f.name,
      value: f.value,
      extra: `累计 ${f.total} · 今日 ${f.today} · 近 7 日 ${f.week} · 近 30 日 ${f.month}`,
      delta: f.delta,
      valueLabel: RANGE_LABEL[fidRange.value],
    })),
    topFids.value.map((f) => colorForFid(f.fid ?? '')),
    (i) => goDist(topFids.value[i]?.fid ?? undefined),
    force,
  )
}

/**
 * 切换活跃榜口径：只重新拉取对应榜单，不整页刷新。
 * 必须清空图表数据指纹（lastXxxKey）——指纹由「名称:数值」构成，
 * 切换口径后数值可能完全不变（例如累计值），不清空会导致图表不重绘。
 */
async function switchRank(which: 'author' | 'fid', range: RankRange) {
  try {
    if (which === 'author') {
      authorRange.value = range
      topAuthors.value = await api.topAuthors(10, range)
      lastAuthorKey = ''
      await nextTick()
      renderAuthorChart()
    } else {
      fidRange.value = range
      topFids.value = await api.topFids(10, range)
      lastFidKey = ''
      await nextTick()
      renderFidChart()
    }
  } catch (e) {
    if (isAborted(e)) return
    void notifyError(`切换榜单口径失败: ${(e as Error).message}`)
  }
}

/** 口径切换入口：以 unknown 接收，避免模板箭头函数参数触发隐式 any */
function onAuthorRangeChange(v: unknown) {
  void switchRank('author', v as RankRange)
}

/** 同上：活跃版块榜口径切换 */
function onFidRangeChange(v: unknown) {
  void switchRank('fid', v as RankRange)
}

function onResize() {
  // 跨过窄屏断点时，两张活跃榜要走另一套留白配置：仅 resize 不会改变 grid/标签，
  // 必须强制重绘（renderHBarChart 平时靠数据指纹跳过重绘）
  const w = authorChartRef.value?.clientWidth || window.innerWidth
  const narrowNow = w < RANK_NARROW_W
  if (rankNarrow === null) rankNarrow = narrowNow
  else if (narrowNow !== rankNarrow) {
    rankNarrow = narrowNow
    renderAuthorChart(true)
    renderFidChart(true)
  }
  trendChart.value?.resize()
  trendEngChart.value?.resize()
  fidTrendChart.value?.resize()
  authorChart.value?.resize()
  fidChart.value?.resize()
}

/**
 * 单卡片全屏：复用 useAppStore 的元素级全屏（不支持时自动降级为伪全屏），
 * 与整页「大屏」共用同一 fullscreen 状态，退出走 Esc 或再次点击按钮。
 * 尺寸变化由 ResizeObserver + watch(app.fullscreen) 的 rebuildCharts 兜底。
 */
async function onCardFullscreen(which: 'trend' | 'fid' | 'eng'): Promise<void> {
  if (app.fullscreen) {
    fsCard.value = null
    await app.exitFullscreen()
    return
  }
  fsCard.value = which
  const target =
    which === 'trend' ? trendCardRef.value : which === 'fid' ? fidTrendCardRef.value : trendEngCardRef.value
  await app.enterFullscreen(target)
}

/**
 * 大屏模式切换时重建全部图表实例：
 * ECharts 的 Tooltip 挂载容器（appendTo / appendToBody）在实例初始化时即确定，
 * 后续 setOption 修改不生效；真全屏下挂 body 的 tooltip 不显示，故必须重建。
 */
function rebuildCharts(): void {
  trendChart.value?.dispose()
  trendChart.value = null
  trendEngChart.value?.dispose()
  trendEngChart.value = null
  fidTrendChart.value?.dispose()
  fidTrendChart.value = null
  authorChart.value?.dispose()
  authorChart.value = null
  fidChart.value?.dispose()
  fidChart.value = null
  // 「事件仅注册一次」的保护标记需复位，否则新实例不再绑定双向 Tooltip 联动 / 缩放联动
  trendTipSynced = false
  fidTipSynced = false
  trendEngBound = false
  // 清空 P1-8 数据指纹，强制重新 setOption（动画开关也在 option 中）
  lastTrendKey = ''
  lastTrendEngKey = ''
  lastFidTrendKey = ''
  lastAuthorKey = ''
  lastFidKey = ''
  // 复位窄屏标记：重建后由 onResize 按新容器宽度重新判定
  rankNarrow = null
  stopTrendCarousel()
  renderTrendChart()
  renderTrendEngChart()
  renderFidTrendChart()
  renderAuthorChart()
  renderFidChart()
  onResize()
  if (trend.value.length) startTrendCarousel(0)
}

// 容器尺寸变化驱动图表重排：侧栏折叠 / 移动抽屉 / 大屏全屏都不会触发 window.resize，
// 只能观察内容容器本身；用 rAF 合并同一帧内的多次回调，避免连续抖动
let resizeObserver: ResizeObserver | null = null
let resizeFrame = 0
function onContainerResize() {
  if (resizeFrame) return
  resizeFrame = requestAnimationFrame(() => {
    resizeFrame = 0
    onResize()
  })
}

function syncAutoRefresh() {
  if (store.autoRefresh) {
    if (!refreshTimer) refreshTimer = setInterval(() => autoRefreshTick(), REFRESH_INTERVAL)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

/** 自动刷新：仅刷新已加载区块；懒加载区块若已在视口内则一并刷新；防重入（上一轮未完成则跳过本轮） */
function autoRefreshTick() {
  if (refreshing) return
  refreshing = true
  const jobs: Promise<unknown>[] = []
  if (overview.value || loadingP0.value) jobs.push(loadP0(false))
  if (boards.value) jobs.push(loadBoards())
  // 待下载推荐如今是首屏卡（与互动量趋势同行），随轮询刷新：下载完成后相应帖子自动退出
  // 推荐位（后端 5s TTL 缓存）。loadPending 自带在途防重入，失败由下一轮自动重试。
  jobs.push(loadPending())
  Promise.allSettled(jobs).finally(() => {
    refreshing = false
  })
}

/** 页面隐藏时暂停全部轮询与轮播动画（浏览器后台会节流定时器，主动暂停更省资源） */
function stopAllTimers() {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
  if (carouselTimer) {
    clearInterval(carouselTimer)
    carouselTimer = null
  }
  stopTrendCarousel()
  if (fidTrendTipTimer) {
    clearInterval(fidTrendTipTimer)
    fidTrendTipTimer = null
  }
}

/** 页面恢复可见：立即刷新一次并重启轮询与轮播 */
function resumeAllTimers() {
  if (store.autoRefresh) autoRefreshTick()
  syncAutoRefresh()
  if (trendChart.value && trend.value.length) startTrendCarousel(0)
  if (store.carouselActive) createCarouselTimer()
}

function onVisibilityChange() {
  if (document.visibilityState === 'hidden') {
    pageVisible = false
    stopAllTimers()
  } else if (!pageVisible) {
    pageVisible = true
    resumeAllTimers()
  }
}

onMounted(() => {
  // P0：立即加载首屏
  loadP0(true)
  // 待下载推荐已移入首屏（与互动量趋势同行），必须随 P0 一起取数，
  // 否则卡片直到热门榜进入视口（P1）才加载，首屏会长时间空着
  loadPending()

  // P1：热门榜 懒加载（IntersectionObserver，rootMargin 预取）
  const hasObserver = typeof IntersectionObserver !== 'undefined'
  if (hasObserver && p1AreaRef.value) {
    p1Observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          p1Observer?.disconnect()
          p1Observer = null
          loadBoards()
        }
      },
      { rootMargin: '200px 0px' },
    )
    p1Observer.observe(p1AreaRef.value)
  } else if (!hasObserver) {
    // 兼容不支持 IntersectionObserver 的旧浏览器：直接加载
    loadBoards()
  }

  // 分版块趋势：懒加载（进入视口后再加载，避免首屏一次性拉取过多）
  // 注意：不能在这里直接 observe——mounted 时 overview 尚未返回，模板走骨架屏分支，
  // 卡片 ref 还是 null，observer 会永远创建不出来（实测：trend_by_fid 从未请求，
  // 分版块图一直空白）。已改为 watch 卡片 ref 出现后再 observe（见上方 watch）。
  if (!('IntersectionObserver' in window)) {
    // 兼容不支持 IntersectionObserver 的旧浏览器：直接加载
    fidTrendVisible.value = true
    loadFidTrend()
  }

  syncAutoRefresh()
  store.registerAutoChange(syncAutoRefresh)
  if (typeof ResizeObserver !== 'undefined' && rootRef.value) {
    resizeObserver = new ResizeObserver(onContainerResize)
    resizeObserver.observe(rootRef.value)
  } else {
    // 兜底：不支持 ResizeObserver 的旧浏览器退回窗口级监听
    window.addEventListener('resize', onResize)
  }
  document.addEventListener('visibilitychange', onVisibilityChange)
  window.addEventListener('keydown', onKeydown)
})

/**
 * 大屏模式切换：重建图表以切换 Tooltip 挂载方式（挂 body 在全屏下不显示）；
 * 进入大屏时同时补齐懒加载区块，否则视口变高后下半屏会出现空白卡片。
 */
watch(
  () => app.fullscreen,
  async (v) => {
    await nextTick()
    rebuildCharts()
    if (!v) return
    fidTrendVisible.value = true
    await Promise.allSettled([loadBoards(), loadPending(), loadFidTrend()])
  },
)

// ============================================================
// 演示轮播（投屏模式）：独立开关，仅控制视图滚动，不影响数据刷新；
// 与「全屏」解耦——可单独开，也可叠加全屏投屏。
// 复用 stopAllTimers / resumeAllTimers 的页面可见性暂停框架（后台标签不空转轮播）。
// ============================================================
// 板块锚点：key（与后端 carousel_sections 白名单对齐）→ 页面元素 id。
// 总览首屏 / 趋势 / 活跃榜 / 热门榜为四个真实垂直滚动位置，可自由组合与排序。
const SECTION_IDS: Record<string, string> = {
  overview: 'section-overview',
  trend: 'section-trend',
  ranks: 'section-ranks',
  boards: 'section-boards',
}
const CAROUSEL_DEFAULT_ORDER = ['overview', 'trend', 'ranks', 'boards']
const CAROUSEL_DEFAULT_INTERVAL_MS = 8000
let carouselTimer: ReturnType<typeof setInterval> | null = null
const carouselIdx = ref(0)
// 运行时轮播配置：来自「参数设置」页；进入轮播时拉取一次（运行中不变）
let carouselSections: string[] = CAROUSEL_DEFAULT_ORDER
let carouselIntervalMs = CAROUSEL_DEFAULT_INTERVAL_MS

/** 从「参数设置」页读取轮播间隔与板块序列；读取失败回落默认 */
async function loadCarouselConfig() {
  try {
    const cfg = await api.config()
    const intervalItem = cfg.settings.find((s) => s.key === 'carousel_interval')
    const sectionsItem = cfg.settings.find((s) => s.key === 'carousel_sections')
    const secKeys = Array.isArray(sectionsItem?.value)
      ? (sectionsItem!.value as string[])
      : CAROUSEL_DEFAULT_ORDER
    const valid = secKeys.filter((k) => SECTION_IDS[k])
    carouselSections = valid.length ? valid : CAROUSEL_DEFAULT_ORDER
    const sec = typeof intervalItem?.value === 'number' ? intervalItem.value : 8
    carouselIntervalMs = sec * 1000
  } catch {
    carouselSections = CAROUSEL_DEFAULT_ORDER
    carouselIntervalMs = CAROUSEL_DEFAULT_INTERVAL_MS
  }
}

function scrollToSection(idx: number) {
  const id = SECTION_IDS[carouselSections[idx]]
  if (!id) return
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 仅创建/重置轮播定时器（不复位滚动位置，供页面恢复可见时续播） */
function createCarouselTimer() {
  if (carouselTimer) clearInterval(carouselTimer)
  carouselTimer = setInterval(() => {
    // 悬停暂停：用户想看 tooltip / 下钻时不被强制滚走
    if (store.carouselPaused) return
    carouselIdx.value = (carouselIdx.value + 1) % carouselSections.length
    scrollToSection(carouselIdx.value)
  }, carouselIntervalMs)
}

async function startCarousel() {
  await loadCarouselConfig()
  // 进入即预加载 P1 热门榜与待下载推荐，避免轮播到 boards 时仍是骨架
  if (!boards.value) void loadBoards()
  void loadPending()
  carouselIdx.value = 0
  scrollToSection(0)
  createCarouselTimer()
}

function stopCarousel() {
  if (carouselTimer) {
    clearInterval(carouselTimer)
    carouselTimer = null
  }
  store.setCarouselPaused(false)
  // 复位到总览首屏顶部
  document.getElementById('section-overview')?.scrollIntoView({ block: 'start' })
}

/** 仅清定时器（页面隐藏时调用，不复位滚动位置，恢复时直接续播） */
function stopCarouselTimer() {
  if (carouselTimer) {
    clearInterval(carouselTimer)
    carouselTimer = null
  }
}

function nextSlide() {
  carouselIdx.value = (carouselIdx.value + 1) % carouselSections.length
  scrollToSection(carouselIdx.value)
}

function prevSlide() {
  carouselIdx.value = (carouselIdx.value - 1 + carouselSections.length) % carouselSections.length
  scrollToSection(carouselIdx.value)
}

function toggleCarouselPause() {
  store.setCarouselPaused(!store.carouselPaused)
}

// 鼠标进入大屏区域即暂停轮播，移出恢复（与趋势轮播悬停暂停同范式）
function onRootMouseEnter() {
  if (store.carouselActive) store.setCarouselPaused(true)
}
function onRootMouseLeave() {
  if (store.carouselActive) store.setCarouselPaused(false)
}

// 控制条可见性：收起后保留极简唤回入口，轮播本身继续运行
const barHidden = ref(false)
function toggleBarHidden() {
  barHidden.value = !barHidden.value
}

// 监听 store 中的演示开关，启停轮播
watch(
  () => store.carouselActive,
  (v) => {
    if (v) {
      void startCarousel()
      barHidden.value = false // 重新进入演示时复位为展开态
    } else {
      stopCarousel()
    }
  },
)

// 键盘：Esc 完全退出演示；H 仅收起/展开控制条（轮播继续）
function onKeydown(e: KeyboardEvent) {
  if (!store.carouselActive) return
  if (e.key === 'Escape') {
    store.setCarouselActive(false)
  } else if (e.key === 'h' || e.key === 'H') {
    // 输入框聚焦时不拦截，避免影响正常打字
    const tag = (e.target as HTMLElement | null)?.tagName
    if (tag !== 'INPUT' && tag !== 'TEXTAREA') toggleBarHidden()
  }
}

onBeforeUnmount(() => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
  if (trendObserver) {
    trendObserver.disconnect()
    trendObserver = null
  }
  if (p1Observer) {
    p1Observer.disconnect()
    p1Observer = null
  }
  if (fidTrendObserver) {
    fidTrendObserver.disconnect()
    fidTrendObserver = null
  }
  if (fidTrendChart.value) {
    fidTrendChart.value.dispose()
    fidTrendChart.value = null
  }
  // authorChart / fidChart 也必须 dispose——否则 appendToBody 的 tooltip DOM
  // 会随未释放的 ECharts 实例一起残留在 body 中，导致「鼠标移开后 tooltip 不消失」
  if (authorChart.value) {
    authorChart.value.dispose()
    authorChart.value = null
  }
  if (fidChart.value) {
    fidChart.value.dispose()
    fidChart.value = null
  }
  stopCarouselTimer()
  window.removeEventListener('keydown', onKeydown)
  stopTrendCarousel()
  store.registerAutoChange(null)
  resizeObserver?.disconnect()
  resizeObserver = null
  if (resizeFrame) {
    cancelAnimationFrame(resizeFrame)
    resizeFrame = 0
  }
  window.removeEventListener('resize', onResize)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  trendChart.value?.dispose()
  trendEngChart.value?.dispose()
})

function openUrl(url: string) {
  // 经同源中继打开（见 utils/postUrl）：本机镜像只监听回环，手机无法直连，必须由看板转发
  window.open(postOpenUrl(url), '_blank', 'noopener')
}

/** 创建下载任务（榜单 / 待下载推荐每行「下载」按钮）：与帖子浏览同一套共用交互（D2 判重 + 防连点），
 *  进度在下载中心查看。全屏态需先退出全屏，提示与确认框才可见。
 *  创建成功后再定向刷新「依赖下载状态」的卡（待下载推荐 + 内容资产）；被判重剔除、用户
 *  取消确认或请求失败时不刷新——数据没有变化，刷新只会产生无意义的请求与视觉抖动。 */
const { submitDownload } = useDownloadSubmit()
async function downloadUrl(url: string) {
  const created = await submitDownload([url], {
    success: notifySuccess,
    error: notifyError,
    warning: notifyWarning,
    beforeDialog: async () => {
      if (app.fullscreen && !app.pseudoFullscreen) await app.exitFullscreen()
    },
  })
  if (created) refreshDownloadDependent()
}

function rankClass(i: number): string {
  if (i === 0) return 'rank-badge rank-gold'
  if (i === 1) return 'rank-badge rank-silver'
  if (i === 2) return 'rank-badge rank-bronze'
  return 'rank-badge rank-plain'
}

function metricText(v: unknown): string {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n.toLocaleString() : '-'
}



// ============================================================
// 动态效果模块（数据大屏风格，纯前端动画，不触发数据刷新）
// ============================================================

// ---- 每日发布趋势：tooltip 自动轮播（7 → 14 → 21 → 28 天循环）----
// 模拟鼠标悬停效果，沿时间轴从右往左（最新日期 → 最早日期）依次展示每个数据点的 tooltip；
// 当前维度展示完成后自动切换下一维度，循环播放；悬停暂停、移出恢复；
// 维度切换采用「保留旧图表 → 加载新数据 → ECharts 平滑过渡动画」的无缝衔接，无闪烁无跳变。
const TREND_DAYS_SEQ = [7, 14, 21, 28]
const TREND_TIP_INTERVAL = 900 // ms，单点停留时长
const TREND_STAGE_GAP = 1500 // ms，阶段切换间隔
const trendSwitching = ref(false) // 数据切换中（轻量 loading 指示，不透明度过渡）
let trendTipTimer: ReturnType<typeof setInterval> | null = null
let trendStageTimer: ReturnType<typeof setTimeout> | null = null
let trendStartTimer: ReturnType<typeof setTimeout> | null = null
let trendTipIndex = 0
let trendTipPaused: boolean = false
let trendLoading = false
/** 全站趋势趋势图 Tooltip 轮播：悬停暂停 / 移出恢复（方法包装，规避 ts-plugin 对 let 变量模板内联赋值的类型收窄误报） */
function setTrendTipPaused(paused: boolean) {
  trendTipPaused = paused
}
/** 分版块趋势图 Tooltip 轮播：悬停暂停 / 移出恢复（同上） */
function setFidTrendTipPaused(paused: boolean) {
  fidTrendTipPaused = paused
}
// 全站趋势与分版块 Tooltip 联动：仅注册一次
let trendTipSynced = false
let fidTipSynced = false
// 防止双向联动时 showTip 事件递归派发
let tipSyncing = false
// 缩放选区 / 聚焦日联动（2026-09-13）：每日互动量趋势图的 datazoom + click 监听同样只注册一次
let trendEngBound = false
// 防止「同步派发的 dataZoom」回声：dispatchAction 会同步触发目标图自己的 datazoom 事件，
// 不挡住就会两张单指标图互相触发（与 tipSyncing 同一思路）
let zoomSyncing = false
function syncTipTo(target: any, params: any) {
  const idx = params?.dataIndex
  if (idx == null || !target) return
  if (tipSyncing) return
  tipSyncing = true
  target.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: idx })
  tipSyncing = false
}
const trendCache = new Map<number, TrendPoint[]>()
// 互动量趋势同维度缓存（切回已看过的天数时不重新请求，与发布趋势一致）
const trendEngCache = new Map<number, TrendPoint[]>()

function stopTrendCarousel() {
  if (trendTipTimer) {
    clearInterval(trendTipTimer)
    trendTipTimer = null
  }
  if (trendStageTimer) {
    clearTimeout(trendStageTimer)
    trendStageTimer = null
  }
  if (trendStartTimer) {
    clearTimeout(trendStartTimer)
    trendStartTimer = null
  }
  trendTipIndex = 0
  trendChart.value?.dispatchAction({ type: 'hideTip' })
}

/** 启动趋势 tooltip 轮播；delay>0 时延迟启动（等待描线/过渡动画完成）；
 *  prevLen 为上一维度点数，用于维度切换时让轮播起点与上一阶段终点在时间轴上衔接 */
function startTrendCarousel(delay = 0, prevLen?: number) {
  stopTrendCarousel()
  if (delay > 0) {
    trendStartTimer = setTimeout(() => {
      trendStartTimer = null
      beginTrendTipLoop(prevLen)
    }, delay)
  } else {
    beginTrendTipLoop(prevLen)
  }
}

function beginTrendTipLoop(prevLen?: number) {
  const chart = trendChart.value
  if (!chart || !trend.value.length) return
  // 先清除可能残留的 tooltip，保证每阶段第一帧干净
  chart.dispatchAction({ type: 'hideTip' })
  // 从右往左（最新日期 → 最早日期）依次展示；
  // 维度衔接时起点为「上一维度天数之前一天」：例如 7 天播到 6 天前后，30 天从 7 天前（index 22）接着往左
  trendTipIndex = prevLen != null
    ? Math.max(0, trend.value.length - 1 - prevLen)
    : trend.value.length - 1
  trendTipTimer = setInterval(() => {
    // 悬停全站趋势或分版块任一张卡片时，暂停自动轮播；离开后恢复
    if (trendTipPaused || fidTrendTipPaused) return
    const c = trendChart.value
    if (!c || !trend.value.length) return
    // 数据刷新导致长度变化时钳制索引，避免越界
    if (trendTipIndex >= trend.value.length) trendTipIndex = trend.value.length - 1
    if (trendTipIndex < 0) {
      advanceTrendStage()
      return
    }
    c.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: trendTipIndex })
    trendTipIndex--
  }, TREND_TIP_INTERVAL)
}

/** 当前维度展示完成：间隔后切换到下一数据范围（7 → 14 → 21 → 28 → 7）；
 *  切换到 14/21/28 天时轮播起点与上一阶段终点衔接（时间轴连续向左推进），
 *  循环回 7 天时重新从最新日期（最右侧）开始 */
function advanceTrendStage() {
  if (trendTipTimer) {
    clearInterval(trendTipTimer)
    trendTipTimer = null
  }
  trendChart.value?.dispatchAction({ type: 'hideTip' })
  trendStageTimer = setTimeout(() => {
    trendStageTimer = null
    const i = TREND_DAYS_SEQ.indexOf(trendDays.value)
    const nextIdx = (i + 1) % TREND_DAYS_SEQ.length
    // 自动切换天数：同步联动分版块（复用 onTrendDaysChange 的下钻逻辑）
    trendDays.value = TREND_DAYS_SEQ[nextIdx]
    onTrendDaysChange()
  }, TREND_STAGE_GAP)
}

/** 仅刷新趋势数据（轮播维度切换 / 手动切换），避免 loadP0 全量刷新引起其他卡片重绘；
 *  切换过程：保留旧图表可见 → 加载新数据 → ECharts 内置平滑动画过渡，无闪烁无跳变 */
async function loadTrendOnly(prevLen?: number) {
  if (trendLoading) return
  trendLoading = true
  stopTrendCarousel()
  const cached = trendCache.get(trendDays.value)
  const cachedEng = trendEngCache.get(trendDays.value)
  if (cached && cachedEng) {
    trend.value = cached
    trendEng.value = cachedEng
    await nextTick()
    renderTrendChart()
    renderTrendEngChart()
    startTrendCarousel(0, prevLen)
    trendLoading = false
    return
  }
  trendSwitching.value = true
  try {
    // 发布量与互动量同一窗口一起拉取：两张图横轴必须严格对齐（并排图口径不一致是踩过的坑）
    const [data, dataEng] = await Promise.all([
      cached ?? api.trend(trendDays.value),
      cachedEng ?? api.trend(trendDays.value, 'engagement'),
    ])
    trendCache.set(trendDays.value, data)
    trendEngCache.set(trendDays.value, dataEng)
    trend.value = data
    trendEng.value = dataEng
    await nextTick()
    renderTrendChart()
    renderTrendEngChart()
    startTrendCarousel(400, prevLen)
  } catch (e) {
    if (isAborted(e)) return
    void notifyError(`加载趋势数据失败: ${(e as Error).message}`)
  } finally {
    trendSwitching.value = false
    trendLoading = false
  }
}

/** 手动切换趋势天数：保留旧图表，加载新数据后以 ECharts 平滑动画过渡，轮播重置到该维度最右侧点开始 */
function onTrendDaysChange() {
  // 兼容自定义输入天数（allow-create 可能为字符串），统一为整数并校验
  let n = Number(trendDays.value)
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < 1 || n > 365) {
    trendDays.value = 7
    return
  }
  n = Math.floor(n)
  if (!trendDayOptions.value.includes(n)) {
    trendDayOptions.value = [...trendDayOptions.value, n]
  }
  trendDays.value = n
  // 天数变了，旧的缩放选区不再有意义（数据点数量与日期都已改变）：
  // 复用 clearTrendZoom —— 两张单指标图一起复位，避免只清状态留下一张图停在旧区间
  clearTrendZoom()
  loadTrendOnly()
  // 分版块由全站趋势联动下钻：已加载或可见时同步刷新
  if (fidTrendVisible.value || fidTrend.value.dates.length) {
    loadFidTrend()
  }
}

// ===== 分版块每日趋势（多系列折线）=====
const fidTrendStats = computed(() => {
  const series = fidTrend.value.series
  if (!series.length) return null
  // 累计量最高的版块
  let topIdx = 0
  let topSum = -1
  series.forEach((s, i) => {
    const sum = s.data.reduce((a, b) => a + b, 0)
    if (sum > topSum) {
      topSum = sum
      topIdx = i
    }
  })
  // 单日峰值（所有版块、所有日期的最大值）
  let peak = 0
  series.forEach((s) => s.data.forEach((v) => (peak = Math.max(peak, v))))
  return {
    fidCount: series.length,
    topName: series[topIdx].name,
    peak,
    // 最活版块的近 7 日环比（与活跃版块榜同一滚动窗口口径）
    topDelta: series[topIdx].delta ?? null,
  }
})

/** 懒加载：进入视口后首次拉取，之后由全站趋势联动下钻切换天数走缓存 */
async function loadFidTrend() {
  if (loadingFidTrend.value) return
  const cached = fidTrendCache.get(trendDays.value)
  if (cached) {
    fidTrend.value = cached
    await nextTick()
    renderFidTrendChart()
    return
  }
  loadingFidTrend.value = true
  fidTrendSwitching.value = true
  try {
    const data = await api.trendByFid(trendDays.value, 8)
    fidTrendCache.set(trendDays.value, data)
    fidTrend.value = data
    await nextTick()
    renderFidTrendChart()
  } catch (e) {
    if (isAborted(e)) return
    void notifyError(`加载分版块趋势失败: ${(e as Error).message}`)
  } finally {
    loadingFidTrend.value = false
    fidTrendSwitching.value = false
  }
}

/** 清除分版块联动聚焦 */
function clearFidLink() {
  linkedFid.value = null
  if (fidTrendChart.value) {
    fidTrendChart.value.dispatchAction({
      type: 'downplay',
      series: fidTrend.value.series.map((s) => s.name),
    })
    renderFidTrendChart()
  }
  renderTrendChart()
}

/**
 * 「点击某天」→ 聚焦该日（三张趋势图共用，唯一实现）。
 *
 * 为什么不用 series 的 click 事件（本项目两条实测教训，缺一都点不中）：
 * ① 折线的命中区就是 `lineStyle.width`（2px）。实测在固定列上扫 14 次点击只命中 1 次，
 *    用户几乎点不到，「点击某天联动」等于不可用；
 * ② 即使命中，`triggerLineEvent` 给的是「点中折线/填充区」的 series 事件，
 *    **`dataIndex` 为 undefined**（dataIndex 是「点中某个符号」的语义，本图 `showSymbol: false`
 *    不渲染符号）——旧代码 `trend.value[undefined]` 必然落空（实测 15/15 全落空）。
 *
 * 故改在 zr 层监听：点绘图区内该列的**任意位置**都聚焦那一天（宽容得多），
 * 用 `containPixel({ gridIndex: 0 })` 判定，天然排除底部缩放条与图例区，不会误触。
 */
function bindPlotClick(chart: ECharts, dates: () => string[], onBefore?: () => void): void {
  chart.getZr().on('click', (e: { offsetX?: number; offsetY?: number }) => {
    const x = e?.offsetX
    const y = e?.offsetY
    if (typeof x !== 'number' || typeof y !== 'number') return
    if (!chart.containPixel({ gridIndex: 0 }, [x, y])) return
    // convertFromPixel 在分类轴上返回索引（可能带小数），四舍五入到最近的一天
    const i = Math.round(Number(chart.convertFromPixel({ xAxisIndex: 0 }, x)))
    const ds = dates()
    if (!Number.isFinite(i) || !ds[i]) return
    onBefore?.()
    focusDay(ds[i])
  })
}

/**
 * 聚焦某一天（三图共用同一条状态 linkedDay）：
 * 三张趋势图**任一**点击某天都走这里 → 三图都画该日标线（
 * 单指标图靠 buildDayLineOption 的 markDate，分版块靠既有 markLine）。
 * 与 clearDayLink 成对，禁止在别处再写一份「设 linkedDay + 重绘」。
 */
function focusDay(date: string): void {
  linkedDay.value = date
  renderFidTrendChart()
  renderTrendChart()
  renderTrendEngChart()
}

/** 清除「聚焦某天」（点总趋势某天）：状态只有 linkedDay 一处，三图都要重绘去掉标线 */
function clearDayLink() {
  linkedDay.value = null
  renderFidTrendChart()
  renderTrendChart()
  renderTrendEngChart()
}

/** 分版块图「本次实际渲染」的日期序列（可能已被共享区间裁剪）。
 *  分版块滑动条的选区索引只能映射回这份日期——回传共享区间时才不会错位。 */
let fidRenderedDates: string[] = []

/** 参与缩放联动的三张图的 实例 + 日期序列（缩放选区「读哪张、同步到哪张」都走这里）。
 *  注意：分版块给的是**实际渲染的（已裁剪）日期**，另两张是完整窗口日期。 */
function linkedChart(key: 'trend' | 'eng' | 'fid'): { chart: ECharts | null; dates: string[] } {
  if (key === 'trend') return { chart: trendChart.value, dates: trend.value.map((t) => t.date) }
  if (key === 'eng') return { chart: trendEngChart.value, dates: trendEng.value.map((t) => t.date) }
  return { chart: fidTrendChart.value, dates: fidRenderedDates }
}

/**
 * 缩放选区联动（三图**双向**，2026-09-13）：
 * 读取 `source` 图缩放条的选区 → 换算为日期区间 → ① 同步另外两张单指标图的视窗
 * （dispatchAction('dataZoom')，不带 dataZoomIndex = 作用于它的 inside + slider 两个组件，
 * **只移视窗不改数据**，滑动条语义完整）；② 分版块图按该区间裁剪数据。
 * 任一图拖动/滚轮缩放，另外两张都会跟随；选区覆盖全部数据点 = 未筛选（清空区间）。
 *
 * 分版块作为来源的特殊处理：它的数据本身已被共享区间裁剪，其滑动条只表示
 * 「在裁剪区间内再看一段」。故当它的选区覆盖「它当前全部数据」时（= 裁剪后的满区间，
 * 例如裁剪后我们把它的滑动条复位为满宽），不视为区间变更，直接返回，避免自我触发。
 *
 * 防回声：`zoomSyncing` 挡住 dispatchAction 引发的同名事件（ECharts 同步派发），
 * 否则三张图会互相触发成环。
 */
function applyTrendZoom(source: 'trend' | 'eng' | 'fid' = 'trend'): void {
  if (zoomSyncing) return // 同步派发的回声
  const src = linkedChart(source)
  if (!src.chart || !src.dates.length) return
  const dz = (src.chart.getOption() as { dataZoom?: any[] }).dataZoom?.[0]
  if (!dz) return
  const n = src.dates.length
  // 未拖动过（百分比模式，无 startValue）时按比例换算索引；拖动后直接用索引
  const rawStart = dz.startValue ?? ((dz.start ?? 0) / 100) * (n - 1)
  const rawEnd = dz.endValue ?? ((dz.end ?? 100) / 100) * (n - 1)
  const i0 = Math.max(0, Math.min(n - 1, Math.floor(Math.min(rawStart, rawEnd))))
  const i1 = Math.max(0, Math.min(n - 1, Math.ceil(Math.max(rawStart, rawEnd))))
  const full = i0 === 0 && i1 === n - 1
  if (source === 'fid' && full) return // 分版块的「满区间」= 当前裁剪区间本身，不是新选区
  const range = full ? null : { start: src.dates[i0], end: src.dates[i1] }
  const prev = trendZoomRange.value
  const changed = !prev || !range || prev.start !== range.start || prev.end !== range.end
  trendZoomRange.value = range

  // ① 同步另外两张单指标图：按日期换算索引（不假设各图日期序列完全对齐），
  //    与「分版块按日期裁剪」同一原则——日期才是跨图通用的锚点
  const peers: ('trend' | 'eng')[] =
    source === 'fid' ? ['trend', 'eng'] : [source === 'trend' ? 'eng' : 'trend']
  for (const key of peers) {
    const peer = linkedChart(key)
    if (!peer.chart || !peer.dates.length) continue
    const a = range ? peer.dates.indexOf(range.start) : 0
    const b = range ? peer.dates.lastIndexOf(range.end) : peer.dates.length - 1
    if (a < 0 || b < a) continue
    zoomSyncing = true
    peer.chart.dispatchAction({ type: 'dataZoom', startValue: a, endValue: b })
    zoomSyncing = false
  }
  // ② 区间变化才重绘分版块（拖动过程中会连续触发本事件）
  if (changed) renderFidTrendChart()
}

/** 清除趋势选区：三张图一起复位（两张单指标图移回全区间；分版块恢复全量数据）。
 *  必须三张都复位——漏一张会留下「徽标已清、那张图仍在旧区间」的不一致。 */
function clearTrendZoom(): void {
  trendZoomRange.value = null
  zoomSyncing = true
  for (const key of ['trend', 'eng'] as const) {
    linkedChart(key).chart?.dispatchAction({ type: 'dataZoom', start: 0, end: 100 })
  }
  zoomSyncing = false
  renderFidTrendChart()
}

function renderFidTrendChart() {
  const el = fidTrendRef.value
  if (!el) return
  let dates = fidTrend.value.dates
  let series = fidTrend.value.series
  if (!dates.length || !series.length) return

  // 跟随「全站发布趋势」的缩放条选区裁剪：两图联动，必须同区间。
  // 用日期字符串比较取命中范围，不依赖两张图的日期序列完全对齐。
  const zoom = trendZoomRange.value
  if (zoom) {
    const hit = dates
      .map((d, i) => ({ d, i }))
      .filter((x) => x.d >= zoom.start && x.d <= zoom.end)
      .map((x) => x.i)
    if (hit.length > 1) {
      const a = hit[0]
      const b = hit[hit.length - 1]
      dates = dates.slice(a, b + 1)
      series = series.map((s) => ({ ...s, data: s.data.slice(a, b + 1) }))
    }
  }

  // 记录本次实际渲染的日期（可能已被共享区间裁剪）：
  // 分版块滑动条回传共享区间时，索引只能映射回这份日期
  fidRenderedDates = dates

  // P1-8：数据指纹（含联动聚焦/日期依赖），无变化跳过 setOption
  const fidKey =
    dates.join('|') +
    '|' +
    series.map((s) => `${s.name}:${s.data.join(',')}`).join('|') +
    `|${linkedDay.value ?? ''}|${linkedFid.value?.name ?? ''}`
  if (fidKey === lastFidTrendKey) return
  lastFidTrendKey = fidKey

  // 按 fid 取色，与活跃版块榜 / 帖子浏览的版块标签保持同一映射
  // （原先按排名索引取色，榜单顺序一变颜色就跟着变，跨图对不上）
  const colorByName: Record<string, string> = {}
  series.forEach((s) => (colorByName[s.name] = colorForFid(s.fid)))
  fidColorByName.value = colorByName

  if (!fidTrendChart.value) {
    fidTrendChart.value = echartsInit(el)
    fidTrendChart.value.on('mouseover', () => (fidTrendTipPaused = true))
    fidTrendChart.value.on('mouseout', () => (fidTrendTipPaused = false))
    // 缩放条联动（反向）：在分版块图上拖滑动条/滚轮缩放，同样回传为三图共享区间
    fidTrendChart.value.on('datazoom', () => applyTrendZoom('fid'))
    // 反联动：分版块 Tooltip 出现时，同步显示全站趋势对应天数的 Tooltip（双向）
    if (!fidTipSynced) {
      fidTipSynced = true
      fidTrendChart.value.on('showTip', (params: any) => syncTipTo(trendChart.value, params))
      fidTrendChart.value.on('hideTip', () => {
        if (tipSyncing) return
        tipSyncing = true
        trendChart.value?.dispatchAction({ type: 'hideTip' })
        tipSyncing = false
      })
    }
    // 点某天 → 三图同标该日（反向：由分版块侧发起，与「全站点某天」完全对称，
    // 状态只有 linkedDay 一处）
    bindPlotClick(fidTrendChart.value, () => fidRenderedDates)
    // 点图例高亮：与总趋势卡片联动
    fidTrendChart.value.on('click', (params: any) => {
      if (params.componentType !== 'legend') return
      const name = params.name as string
      const allNames = series.map((s) => s.name)
      if (linkedFid.value && linkedFid.value.name === name) {
        // 再次点击同一条：取消聚焦
        linkedFid.value = null
        fidTrendChart.value?.dispatchAction({ type: 'downplay', series: allNames })
      } else {
        linkedFid.value = { name, color: colorByName[name] }
        fidTrendChart.value?.dispatchAction({ type: 'downplay', series: allNames })
        fidTrendChart.value?.dispatchAction({ type: 'highlight', seriesName: name })
      }
      // 同步刷新总趋势配色
      renderTrendChart()
    })
  }

  // 各版块近 7 日环比（tooltip 每行尾部）：取自接口 delta，用原始全量数据构建，
  // 不受缩放条裁剪影响（环比是 7 日窗口口径，与当前展示区间无关）
  const deltaByName: Record<string, number | null> = {}
  fidTrend.value.series.forEach((s) => (deltaByName[s.name] = s.delta))

  const lineSeries: any[] = series.map((s, i) => {
    const color = colorForFid(s.fid)
    return {
      name: s.name,
      type: 'line',
      smooth: true,
      symbol: 'circle',
      symbolSize: 4,
      showSymbol: false,
      emphasis: { focus: 'series' },
      // 同上：让「点分版块某天」可点中（反向聚焦日联动依赖它）
      triggerLineEvent: true,
      lineStyle: { width: 2, color, shadowColor: color, shadowBlur: 6 },
      itemStyle: { color },
      areaStyle: {
        opacity: 0.06,
        color: new graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color },
          { offset: 1, color: 'rgba(255,255,255,0)' },
        ]),
      },
      data: s.data,
      ...(i === 0 && linkedDay.value
        ? {
            markLine: {
              silent: true,
              symbol: 'none',
              label: {
                formatter: linkedDay.value.slice(5),
                position: 'insideEndTop',
                color: '#e6ebf5',
                fontSize: 11,
                backgroundColor: 'rgba(20,28,48,0.85)',
                padding: [2, 4],
                borderRadius: 3,
              },
              lineStyle: { color: 'rgba(255,255,255,0.55)', type: 'solid', width: 1.5 },
              data: [{ xAxis: linkedDay.value }],
            },
          }
        : {}),
    }
  })

  // 聚焦态：仅高亮联动版块，其余压暗
  if (linkedFid.value) {
    lineSeries.forEach((s) => {
      if (s.name === linkedFid.value!.name) {
        s.lineStyle = { width: 3, color: s.itemStyle.color, shadowColor: s.itemStyle.color, shadowBlur: 10 }
        s.areaStyle = { opacity: 0.18, color: s.itemStyle.color }
        s.z = 5
      } else {
        s.lineStyle = { width: 1, color: s.itemStyle.color, opacity: 0.25 }
        s.areaStyle = { opacity: 0 }
        s.z = 1
      }
    })
  }

  const needZoom = dates.length > 31
  const option = {
    backgroundColor: 'transparent',
    animation: !app.fullscreen,
    // 与全站趋势趋势图保持完全一致的绘图区，使 Y 轴高度对齐
    grid: { top: 30, right: 20, bottom: needZoom ? 46 : 28, left: 44 },
    tooltip: {
      trigger: 'axis',
      ...tipMount(),
      z: 99999,
      backgroundColor: 'rgba(20,28,48,0.92)',
      borderColor: 'rgba(255,255,255,0.12)',
      borderWidth: 1,
      textStyle: { color: '#e6ebf5', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(0,0,0,0.35)' } },
      // 分版块按当日新增从大到小排序展示；每行尾部附近 7 日环比（涨绿/跌红/持平灰/新增橙）
      formatter: (params: any) => {
        if (!Array.isArray(params) || !params.length) return ''
        const rows = [...params]
          .filter((s) => s.value != null && !Number.isNaN(Number(s.value)))
          .sort((a, b) => Number(b.value) - Number(a.value))
        const date = params[0].axisValueLabel ?? params[0].axisValue ?? ''
        const lines = rows
          .map((s) => {
            const d = deltaByName[s.seriesName]
            const cmp =
              d === null || d === undefined
                ? '<span style="color:#f59e0b">7日 新增</span>'
                : d === 0
                  ? '<span style="color:#8b95a7">7日 持平</span>'
                  : `<span style="color:${d > 0 ? '#10b981' : '#ef4444'}">7日 ${d > 0 ? '↑' : '↓'}${Math.abs(d)}%</span>`
            return `${s.marker}${s.seriesName}：<b>${Number(s.value).toLocaleString()}</b> · ${cmp}`
          })
          .join('<br/>')
        return `${date}<br/>${lines}`
      },
    },
    legend: {
      show: false,
      type: 'scroll',
      top: 2,
      selectedMode: false,
      textStyle: { color: '#9aa7c2', fontSize: 11 },
      inactiveColor: '#4a5570',
      pageTextStyle: { color: '#9aa7c2' },
      itemWidth: 14,
      itemHeight: 8,
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLine: { lineStyle: { color: 'rgba(0,0,0,0.25)' } },
      axisTick: { show: false },
      // 不显示 X 轴（竖向）网格线
      splitLine: { show: false },
      axisLabel: {
        color: '#6b7280',
        fontSize: 11,
        hideOverlap: true,
        formatter: (v: string) => v.slice(5),
      },
    },
    yAxis: {
      type: 'value',
      min: 0,
      minInterval: 1,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#6b7280', fontSize: 11, formatter: (v: number) => String(Math.round(v)) },
      // 显示 Y 轴（横向）网格线
      splitLine: { show: true, lineStyle: { color: 'rgba(0,0,0,0.08)' } },
    },
    dataZoom: needZoom
      ? [
          { type: 'inside', start: 0, end: 100 },
          { type: 'slider', height: 18, bottom: 8, start: 0, end: 100 },
        ]
      : [],
    series: lineSeries,
  }
  fidTrendChart.value.setOption(option, { notMerge: false })

  // 切换天数后保持已有聚焦高亮
  if (linkedFid.value && colorByName[linkedFid.value.name]) {
    fidTrendChart.value.dispatchAction({ type: 'downplay', series: series.map((s) => s.name) })
    fidTrendChart.value.dispatchAction({ type: 'highlight', seriesName: linkedFid.value.name })
  }

  // 裁剪/重绘后把本图滑动条复位到「裁剪区间的满宽」：本图选区只表示「在共享区间内再看一段」，
  // 重绘后旧索引区间已失效（可能落到新数据之外），统一复位最稳（复位派发被 zoomSyncing 挡回声）。
  if (needZoom) {
    zoomSyncing = true
    fidTrendChart.value.dispatchAction({ type: 'dataZoom', start: 0, end: 100 })
    zoomSyncing = false
  }
}
</script>

<template>
  <div ref="rootRef" class="dashboard" :class="{ 'is-fullscreen': app.fullscreen }" @mouseenter="onRootMouseEnter" @mouseleave="onRootMouseLeave">
    <!-- 总览锚点（演示轮播用）：健康条 + KPI 卡同属首屏视图 -->
    <div id="section-overview">
      <!-- R1 采集健康条：数据源状态一眼可见；level/message 由后端统一判定，前端只上色 -->
      <div
        v-if="health"
        class="health-bar"
        :class="[`health-${health.level}`, health.run_status === 'running' ? 'health-pulse' : '']"
        role="button"
        tabindex="0"
        :title="healthDetail"
        @click="goRuns"
        @keydown.enter="goRuns"
      >
        <span class="health-dot"></span>
        <span class="health-msg">{{ health.message }}</span>
        <!-- B1：定时抓取「计划」徽标——健康条报告抓取结果，这里补齐计划本身：
             启用没、下次几点跑、今天已处理几轮。点击去设置页（stop 防触发整条跳运行记录）；窄屏隐藏 -->
        <el-tooltip v-if="sched" :content="schedTip" placement="top" :teleported="!app.fullscreen">
          <span
            class="health-sched"
            :class="{ 'sched-off': !sched.enabled || !sched.times.length }"
            role="link"
            tabindex="0"
            @click.stop="goSettings"
            @keydown.enter.stop="goSettings"
          >{{ schedLabel }}</span>
        </el-tooltip>
        <span class="health-go">运行记录 ›</span>
      </div>

    <!-- 统计卡片 -->
    <div class="stat-grid">
      <template v-if="overview">
        <!-- B5（R5 补齐）：累计收录下钻——跳帖子页全量列表（不带任何筛选，卡面 N = 列表 N） -->
        <div
          class="stat-card stat-clickable"
          role="button"
          tabindex="0"
          :title="`累计收录 ${overview.total.toLocaleString()} 帖；点击查看全部帖子明细`"
          @click="goPostsWith({})"
          @keydown.enter="goPostsWith({})"
        >
          <div class="stat-icon" style="background: linear-gradient(135deg, #4f83f1, #2f6fed)">
            <el-icon><Collection /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">
              累计收录
              <span class="stat-drill" aria-hidden="true">下钻 ›</span>
            </div>
            <div class="stat-value"><RollingNumber :value="overview.total" /></div>
            <div class="stat-sub">
              <span class="sub-neutral">近7日发布 +{{ overview.week_new.toLocaleString() }}</span>
              <el-tooltip
                v-if="weekCmp"
                content="近 7 日发布量相对前 7 日的涨跌（滚动窗口，与活跃榜 7 日环比同口径）"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span :class="weekCmp.cls">{{ weekCmp.text }}</span>
              </el-tooltip>
              <span class="sub-neutral">覆盖 {{ fidDist.length }} 个版块</span>
            </div>
          </div>
        </div>
        <!-- 今日发布 KPI 下钻：整卡可点，跳帖子页并锁定「今日」日期窗（卡片 N 帖 = 列表 N 帖，口径自洽）。
             右上角常驻弱化的「下钻 ›」是业界 KPI 卡通用的可达性提示，hover 高亮暗示可点。 -->
        <div
          class="stat-card stat-clickable"
          role="button"
          tabindex="0"
          :title="`今日发布 ${overview.today} 帖（${overview.today_str}）；点击下钻查看当日全部帖子明细`"
          @click="goTodayPosts"
          @keydown.enter="goTodayPosts"
        >
          <div class="stat-icon" style="background: linear-gradient(135deg, #34d399, #10b981)">
            <el-icon><TrendCharts /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">
              今日发布
              <span class="stat-drill" aria-hidden="true">下钻 ›</span>
            </div>
            <div class="stat-value"><RollingNumber :value="overview.today" /></div>
            <div v-if="kpiSub" class="stat-sub">
              <span :class="kpiSub.todayDiff.cls">{{ kpiSub.todayDiff.text }}</span>
              <span class="sub-neutral">昨日 {{ overview.yesterday.toLocaleString() }}</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #f87171, #ef4444)">
            <el-icon><User /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">发帖作者</div>
            <div class="stat-value"><RollingNumber :value="overview.total_users" /></div>
            <!-- 副行两行四值（用户指定格式）：第一行「今日更新 · 活跃率」讲存量，
                 第二行「昨日 · 近7日新增」讲增量。主值是累计作者数，四个副值各属不同窗口；
                 口径说明（近 N 个完整日、只判首次出现）按第 20 条进悬浮，不占常驻位。 -->
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-line">
                <span class="sub-up">今日更新 {{ overview.active_users.toLocaleString() }} 人</span>
                <span class="sub-neutral">· 活跃率 {{ kpiSub.activeShare ?? 0 }}%</span>
              </span>
              <span class="sub-line">
                <span class="sub-neutral">昨日 {{ overview.yesterday_users.toLocaleString() }} 人，近7日新增</span>
                <el-tooltip :content="authorTip" placement="top" :teleported="!app.fullscreen">
                  <span class="sub-new">{{ overview.new_authors_7d.toLocaleString() }} 人</span>
                </el-tooltip>
              </span>
            </div>
          </div>
        </div>
        <!-- B5（R5 补齐）：最近入库下钻——运行记录页就是它的明细（批次、进度、日志） -->
        <div
          class="stat-card stat-clickable"
          role="button"
          tabindex="0"
          title="最近入库时间与抓取批次明细；点击查看运行记录"
          @click="goRuns"
          @keydown.enter="goRuns"
        >
          <div class="stat-icon" style="background: linear-gradient(135deg, #fbbf24, #f59e0b)">
            <el-icon><Clock /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">
              最近入库
              <span class="stat-drill" aria-hidden="true">查看 ›</span>
            </div>
            <div class="stat-value">{{ kpiSub?.latestDate ? kpiSub.latestDate.slice(5) : '—' }}</div>
            <div v-if="kpiSub" class="stat-sub">
              <span v-if="runningBatch" class="running-badge">
                <span class="running-dot"></span>抓取中 {{ runningBatch.progress ?? 0 }}%
              </span>
              <!-- 去重：停摆/告警语义交给 R1 健康条；此处仅保留中性的「更新于」时间戳 -->
              <span v-else class="sub-neutral">更新于 {{ kpiSub.updatedAt ?? '--:--' }}</span>
            </div>
          </div>
        </div>

      </template>
      <template v-else>
        <div v-for="i in 4" :key="i" class="stat-card">
          <el-skeleton animated :rows="3" />
        </div>
      </template>
    </div>
    </div>

    <!-- R4 内容资产（精简 KPI 摘要）：完整卡片已合并到「资源管理」页（KPI 优先形态）。
         此处只保留一行「沉淀完成度」KPI：当前覆盖率 / 目标 + 缺口；点击进资源管理看漏斗 / 五态 / 分层率 / 存储。
         口径与依据见 docs/内容资产沉淀进度调研与建议.md -->
    <div class="page-card asset-kpi">
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">内容资产</span>
          <span class="chart-sub">沉淀完成度</span>
        </div>
        <div class="chart-head-right">
          <span
            class="drill-link"
            role="link"
            tabindex="0"
            title="点击下钻到资源管理，查看漏斗 / 五态 / 分层沉淀率 / 存储明细"
            @click="goResources"
            @keydown.enter="goResources"
          >下钻 ›</span>
        </div>
      </div>
      <div v-if="assets && !assetsEmpty" class="ak-body">
        <div class="ak-rate">
          <span class="ak-now">{{ assets.goal.current_rate }}%</span>
          <span class="ak-target">目标 {{ assets.goal.target_rate }}%</span>
          <span v-if="assets.goal.reached" class="ak-done">已达成</span>
          <span v-else class="ak-remain" :title="goalTip">{{ assets.goal.scope_label }} 内还差 {{ assets.goal.remain.toLocaleString() }} 帖</span>
        </div>
        <div
          class="ak-bar"
          role="progressbar"
          :aria-label="goalAria"
          :aria-valuenow="assets.goal.current_rate"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <div class="ak-fill" :class="{ 'is-reached': assets.goal.reached }" :style="{ width: goalBarWidth }"></div>
          <span class="ak-mark" :style="{ left: assets.goal.target_rate + '%' }"></span>
        </div>
        <div class="ak-foot">
          <span class="ak-sub">已沉淀 <b>{{ assets.downloaded_posts.toLocaleString() }}</b> / 收录 {{ assets.posts_total.toLocaleString() }}</span>
          <!-- B2：运维数字——下载失败（点击进下载中心处理）+ 磁盘剩余（低于阈值标红）。
               数据 /stats/assets 早已给出（state.failed / disk_free），此前只在资源页展示，大屏补一眼位 -->
          <span class="ak-ops">
            <el-tooltip content="最近一次下载尝试失败、且此后未再成功的帖数（持久记录）。这是帖 / 链接级口径，与下载中心按任务统计的「失败任务」不是同一个量纲；点击直达同口径的失败缺口清单（N 条对 N 个），可逐条重下" placement="top" :teleported="!app.fullscreen">
              <span
                class="ak-op ak-op-link"
                :class="{ 'ak-warn': assets.state.failed > 0 }"
                role="link"
                tabindex="0"
                @click="goDownloads"
                @keydown.enter="goDownloads"
              >下载失败 <b>{{ assets.state.failed.toLocaleString() }}</b> ›</span>
            </el-tooltip>
            <el-tooltip v-if="assets.disk_total > 0" content="本地媒体资产所在卷的可用容量；剩余不足总量 10% 或低于 20 GB 时标红" placement="top" :teleported="!app.fullscreen">
              <span class="ak-op" :class="{ 'ak-danger': diskLow }">磁盘剩余 {{ formatSize(assets.disk_free) }}</span>
            </el-tooltip>
          </span>
        </div>
      </div>
      <div v-else-if="assets && assetsEmpty" class="asset-empty">
        <el-icon class="ae-icon"><FolderOpened /></el-icon>
        <div class="ae-text">尚未下载任何内容</div>
        <div class="ae-sub">下载的帖子会沉淀为本地媒体资产并显示在此</div>
        <el-link type="primary" :underline="false" class="ae-link" @click="goResources">去资源管理 ›</el-link>
      </div>
      <el-skeleton v-else animated :rows="1" style="margin-top: 12px" />
    </div>

    <!-- 每日发布趋势：全站趋势 + 分版块 同行各占 1/2 -->
    <div id="section-trend" class="trend-row">
    <div
      ref="trendCardRef"
      class="page-card chart-card trend-half"
      :class="{ 'is-card-fs': app.pseudoFullscreen && fsCard === 'trend' }"
    >
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">全站发布趋势</span>
          <el-tooltip
            content="窗口跟随右侧天数选择（7/14/21/28 日，可自定义输入）；按帖子「发布日」聚合发布量。统计卡（峰值/谷值/日均）与曲线均排除今天——当日抓取未覆盖全天，否则谷值永远落在今天；「近7日环比」为滚动窗口口径（近 7 日 vs 前 7 日，与分版块/活跃榜同源同窗）。点击折线某日可联动聚焦右侧「分版块发布对比」。"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <span class="chart-sub">近 {{ trendDays }} 日 · 按发布日统计</span>
          </el-tooltip>
          <span v-if="linkedFid" class="link-badge" :style="{ '--link-color': linkedFid.color }">
            <span class="link-dot"></span>
            联动聚焦：{{ linkedFid.name }}
            <span class="link-close" @click="clearFidLink">✕</span>
          </span>
        </div>
        <div class="chart-head-right">
          <el-select
            v-model="trendDays"
            size="small"
            class="day-select"
            filterable
            allow-create
            :teleported="!app.fullscreen"
            reserve-keyword="false"
            default-first-option
            placeholder="选择/输入天数"
            @change="onTrendDaysChange"
          >
            <el-option v-for="d in trendDayOptions" :key="d" :label="`${d}天`" :value="d" />
          </el-select>
          <el-tooltip
            :content="app.fullscreen ? '退出全屏（Esc）' : '全屏查看该卡片'"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <el-button
              class="card-fs-btn"
              text
              :icon="app.fullscreen ? 'Aim' : 'FullScreen'"
              aria-label="卡片全屏"
              @click="onCardFullscreen('trend')"
            />
          </el-tooltip>
        </div>
      </div>
      <!-- 统计卡行：位于「标题 + 口径说明」之下（2026-09-13 统一卡片头部模式，
           不再挤在标题行右侧，窄屏也不必靠折行兜底） -->
      <div v-if="trendStats" class="chart-stats">
        <div class="ts-card ts-peak">
          <span class="ts-label">峰值</span>
          <span class="ts-value"><RollingNumber :value="trendStats.max" /></span>
          <span class="ts-sub">{{ trendStats.maxDate.slice(5) }}</span>
        </div>
        <div class="ts-card ts-valley">
          <span class="ts-label">谷值</span>
          <span class="ts-value"><RollingNumber :value="trendStats.min" /></span>
          <span class="ts-sub">{{ trendStats.minDate.slice(5) }}</span>
        </div>
        <div class="ts-card ts-avg">
          <span class="ts-label">日均</span>
          <span class="ts-value"><RollingNumber :value="trendStats.avg" /></span>
        </div>
        <div
          v-if="trendCmp"
          class="ts-card ts-cmp"
          title="近 7 日发布量相对前 7 日的涨跌（滚动窗口，与分版块趋势同源同窗）"
        >
          <span class="ts-label">近7日环比</span>
          <span class="ts-value" :style="{ color: trendCmp.color }">{{ trendCmp.text }}</span>
        </div>
      </div>
      <div v-if="!trend.length && loadingP0" class="chart chart-loading">
        <el-skeleton animated :rows="8" />
      </div>
      <div class="trend-chart-wrap">
        <div
          v-show="trend.length"
          ref="trendRef"
          class="chart"
          @mouseenter="setTrendTipPaused(true)"
          @mouseleave="setTrendTipPaused(false)"
        ></div>
        <div v-if="trendSwitching" class="chart-switch-overlay">
          <span class="switch-dot"></span>
          <span>数据切换中…</span>
        </div>
      </div>
    </div>

    <!-- 每日发布趋势（分版块）：同行右侧 1/2 宽，懒加载 -->
    <div
      ref="fidTrendCardRef"
      class="page-card chart-card trend-half"
      :class="{ 'is-card-fs': app.pseudoFullscreen && fsCard === 'fid' }"
    >
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">分版块发布对比</span>
          <el-tooltip
            content="窗口与左侧「全站发布趋势」严格一致（跟随同一天数选择），两图横轴对齐；每个系列是一个版块（按窗口内发帖量取前 8），可用于定位「热度集中在哪些版块」。悬浮任一点会与左侧全站图联动聚焦同一日期；>31 点时启用区间缩放，缩放区间由卡头「跟随趋势区间」标记体现。统计卡的「最活板块7日环比」为滚动窗口口径（与活跃版块榜同源同窗）。"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <span class="chart-sub">近 {{ trendDays }} 日 · 按版块发帖量</span>
          </el-tooltip>
          <span v-if="linkedDay" class="link-badge" style="--link-color: #e6ebf5">
            <span class="link-dot"></span>
            联动聚焦日：{{ linkedDay.slice(5) }}
            <span class="link-close" @click="clearDayLink">✕</span>
          </span>
          <span v-if="trendZoomRange" class="link-badge" style="--link-color: #e6ebf5">
            <span class="link-dot"></span>
            跟随趋势区间：{{ zoomRangeLabel }}
            <span class="link-close" @click="clearTrendZoom">✕</span>
          </span>
        </div>
        <div class="chart-head-right">
          <el-tooltip
            :content="app.fullscreen ? '退出全屏（Esc）' : '全屏查看该卡片'"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <el-button
              class="card-fs-btn"
              text
              :icon="app.fullscreen ? 'Aim' : 'FullScreen'"
              aria-label="卡片全屏"
              @click="onCardFullscreen('fid')"
            />
          </el-tooltip>
        </div>
      </div>
      <!-- 统计卡行：位于「标题 + 口径说明」之下（同「全站发布趋势」，2026-09-13 统一） -->
      <div v-if="fidTrendStats" class="chart-stats">
        <div class="ts-card ts-total">
          <span class="ts-label">对比版块</span>
          <span class="ts-value"><RollingNumber :value="fidTrendStats.fidCount" /></span>
        </div>
        <div class="ts-card ts-peak">
          <span class="ts-label">最活版块</span>
          <span class="ts-value ts-name">{{ fidTrendStats.topName }}</span>
        </div>
        <div class="ts-card ts-avg">
          <span class="ts-label">峰值日增</span>
          <span class="ts-value"><RollingNumber :value="fidTrendStats.peak" /></span>
        </div>
        <div
          v-if="fidTopDelta"
          class="ts-card ts-cmp"
          title="最活板块近 7 日发布量相对前 7 日的涨跌（滚动窗口，与活跃版块榜环比同口径；各版块明细见图表悬浮）"
        >
          <span class="ts-label">最活板块7日环比</span>
          <span class="ts-value" :style="{ color: fidTopDelta.color }">{{ fidTopDelta.text }}</span>
        </div>
      </div>
      <div v-if="!fidTrend.series.length && loadingFidTrend" class="chart chart-loading">
        <el-skeleton animated :rows="8" />
      </div>
      <div class="trend-chart-wrap">
        <div
          v-show="fidTrend.series.length"
          ref="fidTrendRef"
          class="chart"
          @mouseenter="setFidTrendTipPaused(true)"
          @mouseleave="setFidTrendTipPaused(false)"
        ></div>
        <div v-if="fidTrendSwitching" class="chart-switch-overlay">
          <span class="switch-dot"></span>
          <span>数据切换中…</span>
        </div>
      </div>
    </div>

    <!-- 每日互动量趋势：趋势区第三张图（全宽一行），2026-09-13 由「本月最热」卡内 sparkline 抽出。
         抽出原因：原图把「当月全站每日互动量」塞进「当月 Top10 帖」的榜单卡里——两个 population、
         两个单位（帖 vs 互动量）同卡，且 26px 高的迷你柱既不能读数、当天数据不完整时还会塌成
         一根近乎消失的柱，被误读为热度断崖。独立成图后可用完整坐标轴、可比口径与规范标注。
         窗口与「全站发布趋势」严格一致（跟随其天数选择），保证并排两图横轴对齐。 -->
    <!-- 互动量趋势与待下载推荐同行各占 1/2（2026-09-13 排版调整）：
         左=趋势图（读数型），右=待下载清单（行动型），一屏内「看到热度 → 直接下载」闭环；
         窄屏（≤1100px）由 .trend-row 的 1 列规则自动上下堆叠，卡片内部网格同步收为单列。
         trend-eng：本卡图表区按剩余高度自适应（行高由右侧 8 行清单决定，固定高度会在底部留白）。 -->
    <div
      ref="trendEngCardRef"
      class="page-card chart-card trend-half trend-eng"
      :class="{ 'is-card-fs': app.pseudoFullscreen && fsCard === 'eng' }"
    >
      <div class="chart-head">
        <div class="chart-head-left">
          <el-tooltip
            content="窗口与「全站发布趋势」完全一致（跟随其天数选择）；互动量 = 点赞 + 回复（与热门榜排序同源）。统计卡排除今天，曲线上今天那一格打了浅色底并标注「数据未完整」——当日抓取尚未覆盖全天。"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <span class="chart-title">每日互动量趋势</span>
          </el-tooltip>
          <span class="chart-sub">近 {{ trendDays }} 日 · 互动量 = 点赞 + 回复</span>
        </div>
        <div class="chart-head-right">
          <el-tooltip
            :content="app.fullscreen ? '退出全屏（Esc）' : '全屏查看该卡片'"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <el-button
              class="card-fs-btn"
              text
              :icon="app.fullscreen ? 'Aim' : 'FullScreen'"
              aria-label="卡片全屏"
              @click="onCardFullscreen('eng')"
            />
          </el-tooltip>
        </div>
      </div>
      <!-- 统计卡行：位于「标题 + 口径说明」之下（本卡是 2026-09-13 起统一的头部模式样板，
           三张趋势图共用同一种「标题行 → 统计行 → 图表」三层结构） -->
      <div v-if="trendEngStats" class="chart-stats">
        <div class="ts-card ts-peak">
          <span class="ts-label">峰值</span>
          <span class="ts-value"><RollingNumber :value="trendEngStats.max" /></span>
          <span class="ts-sub">{{ trendEngStats.maxDate.slice(5) }}</span>
        </div>
        <div class="ts-card ts-valley">
          <span class="ts-label">谷值</span>
          <span class="ts-value"><RollingNumber :value="trendEngStats.min" /></span>
          <span class="ts-sub">{{ trendEngStats.minDate.slice(5) }}</span>
        </div>
        <div class="ts-card ts-avg">
          <span class="ts-label">日均</span>
          <span class="ts-value"><RollingNumber :value="trendEngStats.avg" /></span>
        </div>
        <div class="ts-card ts-total" title="窗口内互动量合计（同样排除今天这一未完整数据点）">
          <span class="ts-label">窗口合计</span>
          <span class="ts-value"><RollingNumber :value="trendEngStats.total" /></span>
        </div>
      </div>
      <div v-if="!trendEng.length && loadingP0" class="chart chart-loading">
        <el-skeleton animated :rows="8" />
      </div>
      <div class="trend-chart-wrap">
        <div
          v-show="trendEng.length"
          ref="trendEngRef"
          class="chart"
          @mouseenter="setTrendTipPaused(true)"
          @mouseleave="setTrendTipPaused(false)"
        ></div>
        <div v-if="trendSwitching" class="chart-switch-overlay">
          <span class="switch-dot"></span>
          <span>数据切换中…</span>
        </div>
      </div>
    </div>

    <!-- R3 待下载推荐：近 30 日互动量最高且未下载的帖子（发现 → 下载一步直达）。
         行内「下载」按钮直接创建下载任务（与热门榜同一套交互）；标题打开原帖；
         卡片头「查看全部」下钻到帖子页，继承「近30日 · 未下载 · 按互动量」上下文，
         列表为该筛选的全量明细，数字自洽。
         2026-09-13 由热门榜下方全宽卡移入本行：与「每日互动量趋势」并排各占 1/2
         （趋势图看热度、右侧清单直接下载，形成「看到 → 行动」的闭环）。 -->
    <div class="page-card chart-card pending-card trend-half">
      <div class="chart-head" style="margin-bottom: 8px">
        <div class="chart-head-left">
          <span class="chart-title">待下载推荐</span>
          <el-tooltip
            content="近 30 日滚动窗口（含今天、会跨月）内互动量最高、且尚未下载到本地的帖子（已下载与下载中自动排除；下载完成后下一轮刷新自动退出推荐；曾下载但文件已被清理的帖子标记为「可重下」）。注意：「本月最热」按数据最新月份的自然月统计，两者窗口不同，即使同按互动量排序，结果也可能不重合"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <span class="chart-sub">近30日滚动窗口（跨月）· 未下载 · 按互动量</span>
          </el-tooltip>
        </div>
        <div class="chart-head-right">
          <el-link type="primary" :underline="false" class="more-link" @click="goPendingPosts">查看全部 ›</el-link>
        </div>
      </div>
      <div v-if="loadingPending && !pending" class="board-list">
        <div v-for="i in 8" :key="i" class="board-card pending-row">
          <el-skeleton animated :rows="1" />
        </div>
      </div>
      <div v-else class="board-list">
        <div v-for="(item, i) in pending?.items ?? []" :key="item.url" class="board-card pending-row">
          <span :class="rankClass(i)">{{ i + 1 }}</span>
          <el-tag size="small" type="info" class="board-tag">{{ item.name }}</el-tag>
          <a
            class="title-link board-title"
            :title="`${item.name} · ${item.title}`"
            @click.stop.prevent="openUrl(item.url)"
          >
            {{ item.title }}
          </a>
          <el-tag
            v-if="item.state === 're_download'"
            size="small"
            type="warning"
            class="board-tag re-download-tag"
          >可重下</el-tag>
          <span class="board-postdate" :title="`发布于 ${item.date}`">{{ item.date.slice(5) }}</span>
          <el-tooltip content="下载" placement="top" :teleported="!app.fullscreen">
            <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
          </el-tooltip>

          <el-tooltip
            :content="`互动量 ${item.engagement} = 点赞 ${item.likes} + 回复 ${item.replies}`"
            placement="top"
            :teleported="!app.fullscreen"
          >
            <span class="board-metric board-metric-main">
              <el-icon><Star /></el-icon>{{ metricText(item.engagement) }}
            </span>
          </el-tooltip>
        </div>
        <div v-if="!pending?.items?.length" class="pending-empty">近 30 日高互动帖子均已下载</div>
      </div>
    </div>
    </div>

    <!-- 图表（P0）：左活跃作者 + 右活跃版块，均为横向条形图 -->
    <div id="section-ranks" class="chart-row">
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 8px">
          <div class="chart-head-left">
            <span class="chart-title">活跃作者 Top10</span>
            <el-tooltip
              content="范围由右侧「累计 / 近 7 日 / 近 30 日」切换；发帖量 = 该窗口内发布的主题数（不含回复）；与「活跃版块 Top10」「近 7 日环比」同源同窗。点击任一条可下钻到帖子页并按该作者筛选。"
              placement="top"
              :teleported="!app.fullscreen"
            >
              <span class="chart-sub">按{{ RANGE_LABEL[authorRange] }}发帖量 · 点击查看该作者帖子</span>
            </el-tooltip>
          </div>
          <div class="chart-head-right">
            <el-radio-group
              :model-value="authorRange"
              size="small"
              @change="onAuthorRangeChange"
            >
              <el-radio-button value="all">累计</el-radio-button>
              <el-radio-button value="7d">近 7 日</el-radio-button>
              <el-radio-button value="30d">近 30 日</el-radio-button>
            </el-radio-group>
          </div>
        </div>
        <div class="chart-wrap">
          <div v-if="!topAuthors.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
          <div v-else-if="!topAuthors.length" class="chart chart-empty">该时间段暂无数据</div>
          <div v-else ref="authorChartRef" class="chart"></div>
        </div>
      </div>
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 8px">
          <div class="chart-head-left">
            <span class="chart-title">活跃版块 Top10</span>
            <el-tooltip
              content="范围由右侧「累计 / 近 7 日 / 近 30 日」切换；发帖量 = 该窗口内该版块发布的主题数；与「活跃作者 Top10」同源同窗，仅维度不同（版块 vs 作者）。点击任一条可下钻到帖子页并按该版块筛选。"
              placement="top"
              :teleported="!app.fullscreen"
            >
              <span class="chart-sub">按{{ RANGE_LABEL[fidRange] }}发帖量 · 点击查看该版块帖子</span>
            </el-tooltip>
          </div>
          <div class="chart-head-right">
            <el-radio-group
              :model-value="fidRange"
              size="small"
              @change="onFidRangeChange"
            >
              <el-radio-button value="all">累计</el-radio-button>
              <el-radio-button value="7d">近 7 日</el-radio-button>
              <el-radio-button value="30d">近 30 日</el-radio-button>
            </el-radio-group>
          </div>
        </div>
        <div class="chart-wrap">
          <div v-if="!topFids.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
          <div v-else-if="!topFids.length" class="chart chart-empty">该时间段暂无数据</div>
          <div v-else ref="fidChartRef" class="chart"></div>
        </div>
      </div>
    </div>

    <!-- 热门榜 + 最近抓取（P1：懒加载） -->
    <div ref="p1AreaRef">
      <!-- 热门榜 -->
      <div id="section-boards" class="board-row">
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <div class="chart-head-left">
              <span class="chart-title">点赞最高帖</span>
              <el-tooltip
                content="全站累计口径（不受下方任何时间筛选影响）；每个版块各取点赞最高的 1 帖（13 个版块 = 13 行，并列时取更新的一条），非全站 Top10。行内「下载」按钮可直接创建下载任务；点击整行可下钻到该版块帖子页并按点赞排序；「查看更多」为全站按点赞排序的 Top 榜。"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="chart-sub">全站累计 · 每版块最高 1 帖</span>
              </el-tooltip>
            </div>
            <div class="chart-head-right">
              <el-link type="primary" :underline="false" class="more-link" @click="goPostsWith({ sort: 'likes_desc' })">查看更多</el-link>
            </div>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in boards?.top_likes ?? []" :key="item.fid" class="board-card" @click="goPostsWith({ fid: item.fid, sort: 'likes_desc' })">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="info" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="`${item.name} · ${item.title}`" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <!-- 「已沉淀」状态标（fresh 默认态不渲染）：紧贴下载按钮，资产相关信号聚合一处 -->
              <el-tooltip v-if="stateBadge(item.state)" :content="stateBadge(item.state)!.tip" placement="top" :teleported="!app.fullscreen">
                <el-tag size="small" :type="stateBadge(item.state)!.type" class="board-state-tag">{{ stateBadge(item.state)!.text }}</el-tag>
              </el-tooltip>
              <el-tooltip :content="downloadTip(item.state)" placement="top" :teleported="!app.fullscreen">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>
              <span class="board-metric">
                <el-icon><Star /></el-icon>{{ metricText(item.value) }}
              </span>
            </div>
            <div v-if="!boards?.top_likes?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <div class="chart-head-left">
              <span class="chart-title">回复最高帖</span>
              <el-tooltip
                content="全站累计口径（不受下方任何时间筛选影响）；每个版块各取回复最高的 1 帖（13 个版块 = 13 行，并列时取更新的一条），非全站 Top10。行内「下载」按钮可直接创建下载任务；点击整行可下钻到该版块帖子页并按回复排序；「查看更多」为全站按回复排序的 Top 榜。"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="chart-sub">全站累计 · 每版块最高 1 帖</span>
              </el-tooltip>
            </div>
            <div class="chart-head-right">
              <el-link type="primary" :underline="false" class="more-link" @click="goPostsWith({ sort: 'replies_desc' })">查看更多</el-link>
            </div>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in boards?.top_replies ?? []" :key="item.fid" class="board-card" @click="goPostsWith({ fid: item.fid, sort: 'replies_desc' })">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="info" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="`${item.name} · ${item.title}`" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <!-- 「已沉淀」状态标（fresh 默认态不渲染）：紧贴下载按钮，资产相关信号聚合一处 -->
              <el-tooltip v-if="stateBadge(item.state)" :content="stateBadge(item.state)!.tip" placement="top" :teleported="!app.fullscreen">
                <el-tag size="small" :type="stateBadge(item.state)!.type" class="board-state-tag">{{ stateBadge(item.state)!.text }}</el-tag>
              </el-tooltip>
              <el-tooltip :content="downloadTip(item.state)" placement="top" :teleported="!app.fullscreen">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>
              <span class="board-metric">
                <el-icon><ChatDotRound /></el-icon>{{ metricText(item.value) }}
              </span>
            </div>
            <div v-if="!boards?.top_replies?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <div class="chart-head-left">
              <span class="chart-title">最新最热</span>
              <el-tooltip
                v-if="todayTop?.date"
                :content="`按数据最新日 ${todayTop.date}（${relDayText(todayTop.date)}）统计，当日共 ${todayTop.total} 帖；排序依据=${BOARD_SORT_HINT[todaySort]}。当日窗口整体更替，故不做「新入榜」标记（会全量刷成 NEW 而失去信息量）；点击整行可下钻到该版块帖子页。`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="chart-sub">{{ todayTop.date.slice(5) }} 当日 · 按{{ BOARD_SORT_HINT[todaySort] }}</span>
              </el-tooltip>
            </div>
            <span v-if="todayTop?.date" class="chart-head-right">
              <el-select v-model="todaySort" size="small" class="board-sort" @change="onTodaySortChange">
                <el-option v-for="o in BOARD_SORT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-link
                type="primary"
                :underline="false"
                class="more-link"
                @click="goPostsInRange({ sort: BOARD_SORT_TO_POSTS[todaySort] }, dayRange(todayTop?.date))"
              >查看更多</el-link>
            </span>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div
              v-for="(item, i) in todayTop?.items ?? []"
              :key="item.url"
              class="board-card"
              @click="goPostsInRange(item.fid ? { fid: item.fid, sort: BOARD_SORT_TO_POSTS[todaySort] } : { sort: BOARD_SORT_TO_POSTS[todaySort] }, dayRange(todayTop?.date))"
            >
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="info" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="`${item.name} · ${item.title}`" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip v-if="isHotTalk(item)" content="热议型：回复数不低于点赞数" placement="top" :teleported="!app.fullscreen">
                <span class="board-flag">热议</span>
              </el-tooltip>
              <!-- 「已沉淀」状态标（fresh 默认态不渲染）：紧贴下载按钮，资产相关信号聚合一处 -->
              <el-tooltip v-if="stateBadge(item.state)" :content="stateBadge(item.state)!.tip" placement="top" :teleported="!app.fullscreen">
                <el-tag size="small" :type="stateBadge(item.state)!.type" class="board-state-tag">{{ stateBadge(item.state)!.text }}</el-tag>
              </el-tooltip>
              <el-tooltip :content="downloadTip(item.state)" placement="top" :teleported="!app.fullscreen">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>

              <el-tooltip
                :content="`互动量 ${engagement(item)} = 点赞 ${item.likes} + 回复 ${item.replies} · ${rateText(item)}`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="board-metric board-metric-main">
                  <el-icon><Star /></el-icon>{{ metricText(engagement(item)) }}
                </span>
              </el-tooltip>
            </div>
            <div v-if="!todayTop?.items?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <div class="chart-head-left">
              <span class="chart-title">本月最热</span>
              <el-tooltip
                v-if="monthTop?.date"
                :content="`按数据最新月份 ${monthTop.date} 统计，当月共 ${monthTop.total} 帖 / 覆盖 ${monthTop.days} 天${monthTop.days < 7 ? '（样本较少，榜单波动大）' : ''}；排序依据=${BOARD_SORT_HINT[monthSort]}。注意：本榜是自然月窗口，与「待下载推荐」的近 30 日滚动窗口不同，即使同按互动量排序结果也可能不重合；点击整行可下钻到该版块帖子页。`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="chart-sub">{{ monthTop.date }} 当月 · 按{{ BOARD_SORT_HINT[monthSort] }}</span>
              </el-tooltip>
            </div>
            <span v-if="monthTop?.date" class="chart-head-right">
              <el-select v-model="monthSort" size="small" class="board-sort" @change="onMonthSortChange">
                <el-option v-for="o in BOARD_SORT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
              <el-link
                type="primary"
                :underline="false"
                class="more-link"
                @click="goPostsInRange({ sort: BOARD_SORT_TO_POSTS[monthSort] }, monthRange(monthTop?.date))"
              >查看更多</el-link>
            </span>
          </div>
          <!-- 本卡只做列表展示：每日互动量趋势已抽出为趋势区「每日互动量趋势」独立图（2026-09-13） -->
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div
              v-for="(item, i) in monthTop?.items ?? []"
              :key="item.url"
              class="board-card"
              @click="goPostsInRange(item.fid ? { fid: item.fid, sort: BOARD_SORT_TO_POSTS[monthSort] } : { sort: BOARD_SORT_TO_POSTS[monthSort] }, monthRange(monthTop?.date))"
            >
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="info" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="`${item.name} · ${item.title}`" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip v-if="item.is_new" content="新入榜：今天首次进入 Top10" placement="top" :teleported="!app.fullscreen">
                <span class="board-new">NEW</span>
              </el-tooltip>
              <el-tooltip v-if="isHotTalk(item)" content="热议型：回复数不低于点赞数" placement="top" :teleported="!app.fullscreen">
                <span class="board-flag">热议</span>
              </el-tooltip>
              <span class="board-postdate" :title="`发布于 ${item.date}`">{{ item.date.slice(5) }}</span>
              <!-- 「已沉淀」状态标（fresh 默认态不渲染）：紧贴下载按钮，资产相关信号聚合一处 -->
              <el-tooltip v-if="stateBadge(item.state)" :content="stateBadge(item.state)!.tip" placement="top" :teleported="!app.fullscreen">
                <el-tag size="small" :type="stateBadge(item.state)!.type" class="board-state-tag">{{ stateBadge(item.state)!.text }}</el-tag>
              </el-tooltip>
              <el-tooltip :content="downloadTip(item.state)" placement="top" :teleported="!app.fullscreen">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>

              <el-tooltip
                :content="`互动量 ${engagement(item)} = 点赞 ${item.likes} + 回复 ${item.replies}（本榜排序依据）`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="board-metric board-metric-main">
                  <el-icon><Star /></el-icon>{{ metricText(engagement(item)) }}
                </span>
              </el-tooltip>
            </div>
            <div v-if="!monthTop?.items?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
      </div>

    </div>

    <!-- 演示轮播控制条：仅演示模式显示，固定底部居中；Esc 退出 / H 收起 -->
    <div v-if="store.carouselActive && !barHidden" class="carousel-bar">
      <span class="cb-title">演示轮播</span>
      <el-button size="small" text @click="prevSlide()">‹ 上一项</el-button>
      <el-button size="small" text :icon="store.carouselPaused ? 'VideoPlay' : 'Timer'" @click="toggleCarouselPause()">
        {{ store.carouselPaused ? '播放' : '暂停' }}
      </el-button>
      <el-button size="small" text @click="nextSlide()">下一项 ›</el-button>
      <el-button size="small" text title="收起控制条（H 键）" @click="barHidden = true">收起</el-button>
      <el-button size="small" type="primary" @click="store.toggleCarousel()">退出</el-button>
    </div>
    <!-- 收起态：极简唤回入口，轮播继续；点击或 H 键展开 -->
    <transition name="cb-fade">
      <div
        v-if="store.carouselActive && barHidden"
        class="carousel-bar-mini"
        @click="barHidden = false"
      >
        <el-icon><VideoPlay /></el-icon>
        <span>演示轮播 · 点击展开</span>
      </div>
    </transition>
  </div>
</template>

<style scoped>
/* 大屏（全屏）态：图表高度按视口剩余高度三等分，KPI 与区块间距收紧，尽量一屏铺满 */
.dashboard.is-fullscreen {
  --chart-h: max(240px, calc((100vh - 220px) / 3));
}

.dashboard.is-fullscreen .stat-grid,
.dashboard.is-fullscreen .trend-row,
.dashboard.is-fullscreen .chart-row,
.dashboard.is-fullscreen .board-row {
  gap: 12px;
  margin-bottom: 12px;
}

/* 大屏（全屏）态：健康条同步收紧间距，避免挤压图表区
   （待下载推荐已移入 .trend-row 成为网格项，行距由该行的 gap 统一控制，不再单列） */
.dashboard.is-fullscreen .health-bar {
  margin-bottom: 12px;
}

.dashboard.is-fullscreen .stat-card {
  padding: 14px 18px;
}

.dashboard.is-fullscreen .stat-value {
  font-size: 32px;
}

.dashboard.is-fullscreen .board-list {
  max-height: calc(var(--chart-h) - 40px);
}

/* 移除「最新最热帖」「媒体文件」两张 KPI 后剩 4 张：桌面保持一行 4 列，窄屏（≤1024px）折 2×2、手机（≤640px）单列，各端自适应铺满（作用域内覆盖全局 .stat-grid 的 3 列，避免影响资源/下载页布局） */
.dashboard .stat-grid {
  grid-template-columns: repeat(4, 1fr);
}
@media (max-width: 1024px) {
  .dashboard .stat-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
@media (max-width: 640px) {
  .dashboard .stat-grid {
    grid-template-columns: 1fr;
  }
}

/* 每日发布趋势 + 版块分布：左右 1:1 等宽，与热门榜保持一致间距 */
.chart-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 1100px) {
  .chart-row {
    grid-template-columns: 1fr;
  }
}

/* 每日发布趋势：全站趋势 + 分版块 同行各占 1/2 */
.trend-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.trend-half {
  min-width: 0;
}
/* 互动量趋势卡：与右侧待下载清单同行等高，图表区吃掉卡片剩余高度
   （右卡 8 行清单决定整行高度；若图表区固定 300px，卡片底部会留 46px 空白，
   大屏态因 --chart-h 变矮会留更多。min-height 只作下限，不改变半宽卡的既有观感） */
.trend-eng {
  display: flex;
  flex-direction: column;
}
.trend-eng .trend-chart-wrap {
  flex: 1 1 auto;
  height: auto;
  min-height: var(--chart-h, 300px);
}
@media (max-width: 1100px) {
  .trend-row {
    grid-template-columns: 1fr;
  }
}

/* 热门榜：4 栏等宽；中屏 2x2，窄屏单列 */
.board-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 1400px) and (min-width: 1101px) {
  .board-row {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 1100px) {
  .board-row {
    grid-template-columns: 1fr;
  }
}

.chart-card {
  min-width: 0;
}

/* 单卡片全屏：真全屏由 :fullscreen 兜底（浏览器自带铺满），
   不支持元素级 Fullscreen API 时降级为 fixed 覆盖层（.is-card-fs）。
   图表高度由 --chart-h 驱动，全屏态只需放大它即可。 */
.chart-card:fullscreen,
.chart-card.is-card-fs {
  --chart-h: calc(100vh - 150px);

  border-radius: 0;
  margin: 0;
  padding: 16px 20px;
  background: #fff;
}

.chart-card.is-card-fs {
  position: fixed;
  inset: 0;
  z-index: 3000;
  width: 100vw;
  height: 100vh;
  overflow: auto;
}

.card-fs-btn {
  flex-shrink: 0;
}

/* 演示轮播控制条：固定底部居中，覆盖在大屏内容之上（z-index 高于卡片全屏） */
.carousel-bar {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  z-index: 4000;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: rgba(20, 28, 48, 0.92);
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 999px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.35);
  color: #e6ebf5;
}
.carousel-bar .cb-title {
  font-size: 13px;
  font-weight: 600;
  color: #a8c5ff;
  margin-right: 4px;
}
.carousel-bar .el-button {
  color: #e6ebf5;
}
.carousel-bar .el-button:hover {
  color: #fff;
}

/* 收起态：极简唤回入口，固定在底部居中，不遮挡内容 */
.carousel-bar-mini {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  z-index: 4000;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  background: rgba(20, 28, 48, 0.7);
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 999px;
  color: #e6ebf5;
  font-size: 12px;
  cursor: pointer;
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.3);
  transition: background 0.2s, color 0.2s;
}
.carousel-bar-mini:hover {
  background: rgba(20, 28, 48, 0.92);
  color: #fff;
}
.cb-fade-enter-active,
.cb-fade-leave-active {
  transition: opacity 0.25s ease;
}
.cb-fade-enter-from,
.cb-fade-leave-to {
  opacity: 0;
}

.chart-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
  gap: 12px;
  /* 窄屏下允许换行，避免趋势 stats + 天数切换超出卡片宽度产生横向滚动条 */
  flex-wrap: wrap;
  min-width: 0;
}

.chart-title {
  font-weight: 600;
  color: #1f2d3d;
}
.chart-head-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.chart-head-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  justify-content: flex-end;
  position: relative; /* 全屏时天数下拉就地挂载，以本容器为定位基准 */
}
.link-badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 10px;
  padding: 2px 8px 2px 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--link-color, #2f6fed);
  background: color-mix(in srgb, var(--link-color, #2f6fed) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--link-color, #2f6fed) 40%, transparent);
  border-radius: 999px;
}
.link-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--link-color, #2f6fed);
  box-shadow: 0 0 6px var(--link-color, #2f6fed);
}
.link-close {
  cursor: pointer;
  margin-left: 2px;
  opacity: 0.7;
  font-size: 11px;
}
.link-close:hover {
  opacity: 1;
}

/* 统计卡行（2026-09-13 统一卡片头部模式）：位于「标题 + 口径说明」之下，
   与图表/列表同属内容行 → 左对齐（此前挂在 .chart-head-right 里右对齐，
   与标题挤同一行，窄屏还得靠折行兜底）。三张趋势图共用本结构。 */
.chart-stats {
  display: flex;
  align-items: center;
  gap: 8px;
  /* 允许整组折行：统计卡自身 white-space: nowrap（防数值被拆），
     但「互动量」卡的数值是 6 位数（如窗口合计 109,191），390px 视口下 4 张卡
     一行放不下会横向溢出（实测 7px）。折行是不牺牲信息量的兜底。 */
  flex-wrap: wrap;
  justify-content: flex-start;
  margin-bottom: 8px;
}
.ts-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 4px 12px;
  border-radius: 6px;
  background: #f7f8fa;
  border-left: 3px solid #2f6fed;
  min-width: 52px;
  /* 数字/版块名不折行：挤压时由 chart-head-right 的 wrap 整条换行兜底 */
  white-space: nowrap;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.ts-card:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}
.ts-label {
  font-size: 11px;
  color: #909399;
  line-height: 1;
  margin-bottom: 2px;
}
.ts-value {
  font-size: 16px;
  font-weight: 700;
  color: #1f2d3d;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}
.ts-sub {
  font-size: 10px;
  color: #b0b3b8;
  margin-top: 1px;
}
.ts-peak { border-left-color: #ef4444; }
.ts-peak .ts-value { color: #ef4444; }
.ts-valley { border-left-color: #10b981; }
.ts-valley .ts-value { color: #10b981; }
.ts-avg { border-left-color: #2f6fed; }
.ts-total { border-left-color: #8b5cf6; }
.ts-total .ts-value { color: #8b5cf6; }

.more-link {
  font-size: 12px;
}

/* 内容资产 KPI 卡下钻入口：与「今日发布」的「下钻 ›」统一视觉（弱化灰字，hover 变蓝暗示可点）。
   此处仅链接可点（非整卡点击），故保留 pointer-events 与可访问性角色，区别于 .stat-drill 的穿透态。 */
.asset-kpi .drill-link {
  font-size: 12px;
  color: #c0c4cc;
  cursor: pointer;
  transition: color 0.15s ease;
  user-select: none;
}
.asset-kpi .drill-link:hover,
.asset-kpi .drill-link:focus-visible {
  color: #2f6fed;
}

.chart {
  width: 100%;
  height: 100%;
}
.trend-chart-wrap {
  position: relative;
  height: var(--chart-h, 320px);
  width: 100%;
  overflow: hidden; /* 遏制 ECharts canvas 初始化瞬间的横向溢出 */
  min-width: 0;
}
/* 半宽卡片：图表略矮，左右等高对齐（大屏态跟随 --chart-h 统一高度） */
.trend-half .trend-chart-wrap {
  height: var(--chart-h, 300px);
}
/* 动态天数切换下拉框 */
.day-select {
  width: 92px;
}
/* E2：折线流光动画（CSS 方案，轻量科技感） */
.trend-chart-wrap::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: linear-gradient(90deg,
    transparent 0%,
    rgba(255, 255, 255, 0.6) 50%,
    transparent 100%
  );
  animation: shine 3s infinite linear;
  pointer-events: none;
  z-index: 10;
}
@keyframes shine {
  from { transform: translateX(-100%); }
  to   { transform: translateX(100%); }
}
.chart-switch-overlay {
  position: absolute;
  right: 16px;
  top: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid rgba(47, 111, 237, 0.25);
  border-radius: 14px;
  font-size: 12px;
  color: #2f6fed;
  backdrop-filter: blur(4px);
  box-shadow: 0 2px 8px rgba(47, 111, 237, 0.08);
  animation: fade-in 0.2s ease;
}
.chart-switch-overlay .switch-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #2f6fed;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.7); }
}
@keyframes fade-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.chart-loading {
  display: flex;
  align-items: center;
}

.chart-wrap {
  position: relative;
  height: var(--chart-h, 320px); /* 与排行榜 8 行可视区等高 */
  overflow: hidden; /* 遏制 ECharts / 排行榜容器瞬时横向溢出 */
  min-width: 0;
}

/* B1 抓取中徽标：绿色脉冲点 + 实时进度 */
.running-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: #10b981;
  font-weight: 600;
}

.running-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #10b981;
  animation: running-pulse 1.2s ease-in-out infinite;
}

@keyframes running-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.4;
    transform: scale(1.6);
  }
}

@media (prefers-reduced-motion: reduce) {
  .running-dot {
    animation: none;
  }
}

/* 卡片头部说明文字（活跃作者榜） */
.chart-sub {
  font-size: 12px;
  color: #909399;
}

/* 榜单空态：某口径下无数据时的占位，避免图表区一片空白 */
.chart-empty {
  display: flex;
  align-items: center;
  justify-content: center;
  color: #909399;
  font-size: 12px;
}

.board-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 360px;
  overflow-y: auto;
  /* 细窄滚动条常显（2026-09-13）：榜单四卡与「待下载推荐」共用本类——内容超出时
     滑块可见，避免列表被截断却看不出「下面还有」。默认覆盖式滚动条在部分环境下
     完全不可见（实测 offsetWidth-clientWidth=0）。 */
  scrollbar-width: thin; /* Firefox */
  scrollbar-color: #c8ced8 transparent;
}

.board-list::-webkit-scrollbar {
  width: 6px;
}

.board-list::-webkit-scrollbar-track {
  background: transparent;
}

.board-list::-webkit-scrollbar-thumb {
  background: #c8ced8;
  border-radius: 3px;
}

.board-list::-webkit-scrollbar-thumb:hover {
  background: #a8b0bd;
}

.board-card {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 10px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  position: relative; /* 全屏时「下载」tooltip 就地挂载，以本卡片为定位基准 */
  background: #fafbfc;
  font-size: 13px;
  min-width: 0;
  cursor: pointer;
  transition: background 0.15s ease, box-shadow 0.15s ease;
}

.board-card:hover {
  background: #f0f4ff;
  box-shadow: 0 2px 8px rgba(47, 111, 237, 0.08);
}

.rank-badge {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
}

.rank-gold {
  background: linear-gradient(135deg, #fbbf24, #f59e0b);
}

.rank-silver {
  background: linear-gradient(135deg, #cbd5e1, #94a3b8);
}

.rank-bronze {
  background: linear-gradient(135deg, #e8a17c, #cd7f5a);
}

.rank-plain {
  background: #e4e7f0;
  color: #606266;
}

.board-tag {
  flex-shrink: 0;
}

/* 窄屏（单列，≤1100px）隐藏行内「板块」中文名：行内元素（名次/NEW/热议/发布日/下载/互动量）
   较多，板块 tag 会挤压标题可读性。板块名已并入标题 tooltip（长按可看），且卡片空白区点击
   下钻该版块，信息不丢失。业界（微博/知乎热榜、GitHub Trending 移动端）亦倾向紧凑分类小标
   而非纯 tooltip，此处窄屏直接隐藏并在 tooltip 兜底，平衡可读性与信息完整性。 */
@media (max-width: 1100px) {
  .board-tag {
    display: none;
  }
}

.board-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.board-metric {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: #606266;
  font-weight: 600;
}

/* 互动量主指标：本榜的排序依据，用主色强调（点赞/回复明细放 tooltip） */
.board-metric-main {
  color: #2f6fed;
}

/* 本月最热的发布日小字；最新最热各行都是同一天，展示无信息量故不渲染 */
.board-postdate {
  flex-shrink: 0;
  font-size: 12px;
  color: #909399;
  font-variant-numeric: tabular-nums;
}

/* 排序维度切换（综合/点赞/回复/热度）：窄一些，避免挤占卡头的日期与「查看更多」 */
.board-sort {
  width: 74px;
  flex-shrink: 0;
}

/* 热议型标记：仅「回复数 ≥ 点赞数」时出现，否则每行都挂标签等于没标签 */
.board-flag {
  flex-shrink: 0;
  font-size: 11px;
  line-height: 16px;
  padding: 0 4px;
  border-radius: 3px;
  color: #d46b08;
  background: #fff7e6;
  border: 1px solid #ffd591;
}

/* 新入榜标记（对比快照，今天首次进入 Top10） */
.board-new {
  flex-shrink: 0;
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  padding: 0 4px;
  border-radius: 3px;
  color: #fff;
  background: #f5222d;
}

/* ================= R1 采集健康条 =================
   三态横幅：ok 绿（弱化不抢视觉）/ warn 橙 / danger 红；
   判定口径在后端 _health_verdict 一处，前端只按 level 上色。
   点击跳运行记录页；抓取中时圆点脉冲提示。 */
.health-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  padding: 8px 14px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: box-shadow 0.15s ease;
  min-width: 0;
}

.health-bar:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.health-bar:focus-visible {
  outline: 2px solid #2f6fed;
  outline-offset: 2px;
}

.health-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.health-msg {
  flex: 1;
  min-width: 0;
  font-weight: 500;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.health-go {
  flex-shrink: 0;
  font-size: 12px;
  opacity: 0.75;
}

/* B1：定时抓取计划徽标（健康条右侧，点击去设置页）。竖线与主条分隔；
   未启用时降透明度弱化；窄屏隐藏保住健康条单行（配置入口在设置页不受影响） */
.health-sched {
  flex-shrink: 0;
  font-size: 12px;
  opacity: 0.85;
  padding-left: 10px;
  border-left: 1px solid currentColor;
  white-space: nowrap;
  cursor: pointer;
}
.health-sched:hover {
  opacity: 1;
  text-decoration: underline;
}
.health-sched.sched-off {
  opacity: 0.55;
}
@media (max-width: 768px) {
  .health-sched {
    display: none;
  }
}

.health-ok {
  background: #f0faf4;
  border-color: #d4f0e0;
  color: #0f7a52;
}
.health-ok .health-dot {
  background: #10b981;
  box-shadow: 0 0 6px rgba(16, 185, 129, 0.6);
}

.health-warn {
  background: #fffaf0;
  border-color: #ffe7ba;
  color: #b26b00;
}
.health-warn .health-dot {
  background: #f59e0b;
  box-shadow: 0 0 6px rgba(245, 158, 11, 0.6);
}

.health-danger {
  background: #fef1f2;
  border-color: #ffdce0;
  color: #be2028;
}
.health-danger .health-dot {
  background: #ef4444;
  box-shadow: 0 0 6px rgba(239, 68, 68, 0.6);
}

/* 抓取中：状态点脉冲（与 B1 running-dot 同节奏） */
.health-pulse .health-dot {
  animation: health-pulse 1.2s ease-in-out infinite;
}

@keyframes health-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.45;
    transform: scale(1.5);
  }
}

@media (prefers-reduced-motion: reduce) {
  .health-pulse .health-dot {
    animation: none;
  }
}

/* 窄屏：健康条信息收窄为单行省略，「运行记录」入口保留可达 */
@media (max-width: 640px) {
  .health-bar {
    font-size: 12px;
    padding: 7px 10px;
  }
}

/* ================= R2 周期环比色（累计收录卡副指标） ================= */
.sub-new {
  color: #f59e0b;
}

/* 近 30 日环比卡（趋势卡头）：沿用 ts-card 结构，色值随涨跌方向 */
.ts-cmp {
  border-left-color: #2f6fed;
}

/* ================= R4 内容资产（精简 KPI 摘要） =================
   完整卡片（漏斗 / 五态 / 分层沉淀率 / 存储行 / 对账）已合并到「资源管理」页（KPI 优先形态），
   此处只保留「沉淀完成度」一行 KPI：当前覆盖率 / 目标 + 缺口，点击进资源管理看详情。
   口径与依据见 docs/内容资产沉淀进度调研与建议.md */
.asset-kpi {
  margin-bottom: 16px;
}
.asset-kpi .ak-body {
  margin-top: 10px;
}
.asset-kpi .ak-rate {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}
.asset-kpi .ak-now {
  font-size: 22px;
  font-weight: 700;
  color: #1f2d3d;
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.asset-kpi .ak-target {
  font-size: 12px;
  color: #909399;
}
.asset-kpi .ak-done {
  font-size: 12px;
  font-weight: 600;
  color: #10b981;
}
.asset-kpi .ak-remain {
  margin-left: auto;
  font-size: 12px;
  color: #606266;
  cursor: help;
}
.asset-kpi .ak-bar {
  position: relative;
  height: 8px;
  border-radius: 4px;
  background: #f0f2f5;
  margin-top: 8px;
}
.asset-kpi .ak-fill {
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, #2f6fed, #10b981);
  transition: width 0.3s;
}
.asset-kpi .ak-fill.is-reached {
  background: linear-gradient(90deg, #34d399, #10b981);
}
.asset-kpi .ak-mark {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 14px;
  background: #f59e0b;
  border-radius: 1px;
}
.asset-kpi .ak-foot {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  /* B2：左侧沉淀/收录比、右侧运维数字（下载失败 / 磁盘剩余），两端对齐 */
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.asset-kpi .ak-foot b {
  color: #1f2d3d;
  font-variant-numeric: tabular-nums;
}
/* B2：运维数字组 */
.ak-ops {
  display: flex;
  align-items: baseline;
  gap: 14px;
  flex-shrink: 0;
}
.ak-op-link {
  cursor: pointer;
}
.ak-op-link:hover {
  text-decoration: underline;
}
.ak-op.ak-warn,
.ak-op.ak-warn b {
  color: #e6a23c;
}
.ak-op.ak-danger {
  color: #f56c6c;
  font-weight: 600;
}
/* 热门榜四卡：卡头统一为两行（第一行 标题+口径，第二行 右对齐操作区），
   保证四列列表起始线对齐——此前「本月最热」控件多被挤到折行（57px）、
   其余卡单行（21px），四列列表起点错位。
   min-height 对齐第二行：链接行高 16px 与 el-select 小尺寸 24px 混排时
   仍差 8px（实测 49 vs 57），统一按操作控件高度撑齐 */
.board-row .chart-head-right {
  width: 100%;
  min-height: 24px;
}

/* ================= R3 待下载推荐 =================
   2026-09-13 起与「每日互动量趋势」同行各占 1/2 宽（原为热门榜下方全宽卡）：
   列表直接复用热门榜的 .board-list（单列 + max-height: 360px + 溢出滚动），
   与「本月最热」同款展示；取 10 条、超出部分滚动查看。
   行不整体下钻（帖子页无「未下载」过滤条件），故指针恢复默认。 */

.pending-row {
  cursor: default;
}

.pending-row:hover {
  background: #f5f7fa;
  box-shadow: none;
}

/* 「可重下」标记：曾下载但文件已清理的帖子，区别于全新待下载 */
.re-download-tag {
  flex: 0 0 auto;
  margin: 0 4px;
}

/* 「已沉淀」状态标（榜单四卡，2026-09-19）：已沉淀=绿 / 下载中=蓝 / 可重下=橙，
   配色由 el-tag 的 type 承载；fresh 默认态不渲染，避免未下载行挂满标签的视觉噪音。
   与待下载推荐的 .re-download-tag 同为 el-tag，视觉语言一致；flex 收缩由 board-title 承担 */
.board-state-tag {
  flex: 0 0 auto;
}

.pending-empty {
  color: #909399;
  font-size: 12px;
  padding: 10px 0;
  text-align: center;
}

</style>
