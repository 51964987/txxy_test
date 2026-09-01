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
  TooltipComponent,
} from 'echarts/components'
import type { ECharts } from 'echarts/core'
import { ElMessage } from 'element-plus'
import { Download } from '@element-plus/icons-vue'
import { api, isAborted, type BoardDaily, type Boards, type BoardSort, type FidDistItem, type Overview, type RunSummary, type TodayTop, type TodayTopItem, type TopAuthor, type TopFid, type TrendByFid, type TrendPoint } from '../api'
import { useDashboardStore } from '../stores/dashboard'
import { useAppStore } from '../stores/app'
import { formatShortTime } from '../utils/time'
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
])

const router = useRouter()

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

let trendObserver: IntersectionObserver | null = null
let p1Observer: IntersectionObserver | null = null

const trendRef = shallowRef<HTMLDivElement | null>(null)
const trendChart = shallowRef<ECharts | null>(null)

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
  const fmt = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
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
const fidTrendBlockRef = ref<HTMLDivElement | null>(null)
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



// 确定性色板：同一版块在环形图 / 排行榜中使用一致颜色
const FID_PALETTE = [
  '#2f6fed', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444',
  '#06b6d4', '#ec4899', '#f97316', '#84cc16', '#14b8a6',
  '#6366f1', '#eab308', '#0ea5e9',
]
function colorForFid(fid: string): string {
  const n = parseInt(fid, 10) || 0
  return FID_PALETTE[n % FID_PALETTE.length]
}

// 自动刷新：开关状态存于 dashboard store（header 控件共享），每 30 秒静默刷新一次
// 仅刷新已加载的区块，未进入视口的懒加载区块保持不动
const REFRESH_INTERVAL = 5000
let refreshTimer: ReturnType<typeof setInterval> | null = null
// 页面可见性：后台隐藏时暂停全部轮询与轮播动画，恢复可见时立即刷新并重启
let pageVisible = true
let refreshing = false

const trendStats = computed(() => {
  if (!trend.value.length) return null
  const counts = trend.value.map((t) => t.count)
  const max = Math.max(...counts)
  const min = Math.min(...counts)
  const maxDate = trend.value[counts.indexOf(max)]?.date ?? ''
  const minDate = trend.value[counts.indexOf(min)]?.date ?? ''
  const total = counts.reduce((s, v) => s + v, 0)
  const avg = Math.round(total / counts.length)
  return { max, min, maxDate, minDate, total, avg }
})

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

/** 最新数据日期与今天相差的天数（大于 0 表示滞后）。 */
function daysBetween(dateStr: string): number {
  const d = new Date(`${dateStr}T00:00:00`)
  if (Number.isNaN(d.getTime())) return 0
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  return Math.round((today.getTime() - d.getTime()) / 86400000)
}

// ===== P0：首屏加载（KPI + 趋势 + 分布）=====
async function loadP0(initial = false) {
  if (initial) loadingP0.value = true
  try {
    const [o, t, f, authors, fids] = await Promise.all([
      api.overview(),
      api.trend(trendDays.value),
      api.fidDist(),
      api.topAuthors(10, authorRange.value),
      api.topFids(10, fidRange.value),
    ])
    overview.value = o
    trend.value = t
    fidDist.value = f
    topAuthors.value = authors
    topFids.value = fids
    trendCache.set(trendDays.value, t)
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

// ===== P1：懒加载热门榜（点赞/回复/最新最热/本月最热）=====
async function loadBoards() {
  if (boards.value || loadingBoards.value) return
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
  } catch (e) {
    if (isAborted(e)) return
    if (boards.value) return // 轮询刷新（榜单已存在）失败静默，下轮重试
    void notifyError(`加载热门榜失败: ${(e as Error).message}`)
  } finally {
    loadingBoards.value = false
  }
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

/** sparkline 柱高：按当月峰值归一化，最小 8% 保证矮柱可见 */
function sparkHeight(v: number) {
  const max = Math.max(...(monthTop.value?.daily ?? []).map((d) => d.value), 1)
  return `${Math.max(8, Math.round((v / max) * 100))}%`
}

/** sparkline 单点 tooltip */
function sparkText(d: BoardDaily) {
  return `${d.date} 互动量 ${d.value}`
}

/** P1-8：各图表数据指纹缓存，数据未变化时跳过重复 setOption，避免轮询期间空重绘 */
let lastTrendKey = ''
let lastFidTrendKey = ''

function renderTrendChart() {
  if (trendRef.value) {
    // P1-8：数据指纹（含联动配色依赖），无变化跳过 setOption
    const trendKey =
      trend.value.map((t) => `${t.date}:${t.count}`).join('|') +
      `|${linkedFid.value?.color ?? ''}`
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
      // 反向联动 click 同样仅注册一次，避免数据刷新后重复绑定累积
      trendChart.value.on('click', (params: any) => {
        trendTipPaused = true
        const idx = params.dataIndex
        const point = trend.value[idx]
        if (point) {
          // 反向联动：点总趋势某天 -> 分版块同天高亮（取消下钻，仅保留联动）
          linkedDay.value = point.date
          renderFidTrendChart()
        }
      })
    }
    // 联动配色：当分版块图例聚焦某版块时，全站趋势同步换为该版块色
    const base = linkedFid.value?.color ?? '#2f6fed'
    const data = trend.value.map((t) => t.count)
    const needZoom = trend.value.length > 31
    trendChart.value.setOption({
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
          html += `<span style="color:#8b95a7;font-size:12px">发布</span>`
          html += `<span style="font-size:20px;font-weight:700;color:#fff;font-variant-numeric:tabular-nums">${cur.toLocaleString()}</span>`
          html += `<span style="color:#8b95a7;font-size:12px">条</span>`
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
            html += `<span>7日均</span> <span style="color:#a8c5ff;font-weight:600">${weekAvg.toLocaleString()}</span> <span>条</span>`
            html += `</div>`
          }
          return html
        },
      },
      grid: { left: 44, right: 20, top: 30, bottom: needZoom ? 46 : 28 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: trend.value.map((t) => t.date.slice(5)),
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
          name: '发布帖子',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 4,
          showSymbol: false,
          emphasis: { focus: 'series' },
          data,
          lineStyle: { width: 2, color: base, shadowColor: base, shadowBlur: 6 },
          itemStyle: { color: base },
          areaStyle: {
            opacity: 0.06,
            color: new graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: base },
              { offset: 1, color: 'rgba(255,255,255,0)' },
            ]),
          },
        },
      ],
    })
  }
}

function goDist(fid?: string) {
  if (fid) {
    router.push({ path: '/posts', query: { fid, ...(rankDateRange(fidRange.value) ?? {}) } })
  }
}

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
) {
  // 指纹纳入口径：切换口径后即使数值相同也要重绘
  const key = items.map((d) => `${d.name}:${d.value}:${d.valueLabel ?? ''}`).join('|')
  if (key === lastKeyRef.v) return // P1-8：数据指纹无变化跳过重绘
  lastKeyRef.v = key
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
      // 右侧留出条尾「数值 + 环比」的空间，避免长标签被裁切
      grid: { left: 8, right: 92, top: 6, bottom: 6, containLabel: true },
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
        axisLabel: { color: '#1f2d3d', fontSize: 12, width: 74, overflow: 'truncate' },
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
            itemStyle: { color: colors[i] ?? '#6366f1', borderRadius: [0, 6, 6, 0] },
          })),
          label: {
            show: true,
            position: 'right',
            // 条尾同时给出主值与环比：涨跌用颜色区分，一眼看出谁在上升
            formatter: (p: any) => {
              const d = deltaText(p.data.delta)
              return `{v|${p.value.toLocaleString()}}  {${d.cls}|${d.text}}`
            },
            rich: {
              v: { color: '#606266', fontSize: 11 },
              up: { color: '#10b981', fontSize: 11 },
              down: { color: '#ef4444', fontSize: 11 },
              flat: { color: '#909399', fontSize: 11 },
              new: { color: '#f59e0b', fontSize: 11 },
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

/** 活跃作者 Top10：横向条形图，主值=累计发帖，多色区分，点击下钻该作者帖子 */
const AUTHOR_PALETTE = [
  '#6366f1', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#06b6d4', '#ec4899', '#14b8a6', '#f97316', '#84cc16',
]
function renderAuthorChart() {
  if (!authorChartRef.value) return
  const n = topAuthors.value.length
  if (!n) return
  const colors = AUTHOR_PALETTE.slice(0, n)
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
  return { from: `${month}-01`, to: `${month}-${String(lastDay).padStart(2, '0')}` }
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
function renderFidChart() {
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
  trendChart.value?.resize()
  fidTrendChart.value?.resize()
  authorChart.value?.resize()
  fidChart.value?.resize()
}

/**
 * 大屏模式切换时重建全部图表实例：
 * ECharts 的 Tooltip 挂载容器（appendTo / appendToBody）在实例初始化时即确定，
 * 后续 setOption 修改不生效；真全屏下挂 body 的 tooltip 不显示，故必须重建。
 */
function rebuildCharts(): void {
  trendChart.value?.dispose()
  trendChart.value = null
  fidTrendChart.value?.dispose()
  fidTrendChart.value = null
  authorChart.value?.dispose()
  authorChart.value = null
  fidChart.value?.dispose()
  fidChart.value = null
  // 「事件仅注册一次」的保护标记需复位，否则新实例不再绑定双向 Tooltip 联动
  trendTipSynced = false
  fidTipSynced = false
  // 清空 P1-8 数据指纹，强制重新 setOption（动画开关也在 option 中）
  lastTrendKey = ''
  lastFidTrendKey = ''
  lastAuthorKey = ''
  lastFidKey = ''
  stopTrendCarousel()
  renderTrendChart()
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

  // P1：热门榜 + 最近抓取 懒加载（IntersectionObserver，rootMargin 预取）
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
  if (hasObserver && fidTrendBlockRef.value) {
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
    fidTrendObserver.observe(fidTrendBlockRef.value)
  } else if (!hasObserver) {
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
    await Promise.allSettled([loadBoards(), loadFidTrend()])
  },
)

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
})

function openUrl(url: string) {
  window.open(url, '_blank', 'noopener')
}

/** 创建下载任务（热门榜每行「下载」按钮），进度在下载中心查看 */
async function downloadUrl(url: string) {
  try {
    const r = await api.submitDownload([url])
    await notifySuccess(`已创建下载任务（${r.count} 个链接），可在下载中心查看进度`)
  } catch (e) {
    await notifyError(`创建下载任务失败: ${(e as Error).message}`)
  }
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
function syncTipTo(target: any, params: any) {
  const idx = params?.dataIndex
  if (idx == null || !target) return
  if (tipSyncing) return
  tipSyncing = true
  target.dispatchAction({ type: 'showTip', seriesIndex: 0, dataIndex: idx })
  tipSyncing = false
}
const trendCache = new Map<number, TrendPoint[]>()

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
  if (cached) {
    trend.value = cached
    await nextTick()
    renderTrendChart()
    startTrendCarousel(0, prevLen)
    trendLoading = false
    return
  }
  trendSwitching.value = true
  try {
    const data = await api.trend(trendDays.value)
    trendCache.set(trendDays.value, data)
    trend.value = data
    await nextTick()
    renderTrendChart()
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

/** 清除反向联动（点总趋势某天） */
function clearDayLink() {
  linkedDay.value = null
  renderFidTrendChart()
}

function renderFidTrendChart() {
  const el = fidTrendRef.value
  if (!el) return
  const dates = fidTrend.value.dates
  const series = fidTrend.value.series
  if (!dates.length || !series.length) return

  // P1-8：数据指纹（含联动聚焦/日期依赖），无变化跳过 setOption
  const fidKey =
    dates.join('|') +
    '|' +
    series.map((s) => `${s.name}:${s.data.join(',')}`).join('|') +
    `|${linkedDay.value ?? ''}|${linkedFid.value?.name ?? ''}`
  if (fidKey === lastFidTrendKey) return
  lastFidTrendKey = fidKey

  const palette = FID_PALETTE
  const colorByName: Record<string, string> = {}
  series.forEach((s, i) => (colorByName[s.name] = palette[i % palette.length]))
  fidColorByName.value = colorByName

  if (!fidTrendChart.value) {
    fidTrendChart.value = echartsInit(el)
    fidTrendChart.value.on('mouseover', () => (fidTrendTipPaused = true))
    fidTrendChart.value.on('mouseout', () => (fidTrendTipPaused = false))
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

  const lineSeries: any[] = series.map((s, i) => {
    const color = palette[i % palette.length]
    return {
      name: s.name,
      type: 'line',
      smooth: true,
      symbol: 'circle',
      symbolSize: 4,
      showSymbol: false,
      emphasis: { focus: 'series' },
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
      // 分版块按当日新增从大到小排序展示
      formatter: (params: any) => {
        if (!Array.isArray(params) || !params.length) return ''
        const rows = [...params]
          .filter((s) => s.value != null && !Number.isNaN(Number(s.value)))
          .sort((a, b) => Number(b.value) - Number(a.value))
        const date = params[0].axisValueLabel ?? params[0].axisValue ?? ''
        const lines = rows
          .map(
            (s) =>
              `${s.marker}${s.seriesName}：<b>${Number(s.value).toLocaleString()}</b>`,
          )
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
}
</script>

<template>
  <div ref="rootRef" class="dashboard" :class="{ 'is-fullscreen': app.fullscreen }">
    <!-- 统计卡片 -->
    <div class="stat-grid">
      <template v-if="overview">
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #4f83f1, #2f6fed)">
            <el-icon><Collection /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">累计收录</div>
            <div class="stat-value"><RollingNumber :value="overview.total" /></div>
            <div class="stat-sub">
              <span class="sub-up">近7日发布 +{{ overview.week_new.toLocaleString() }}</span>
              <span class="sub-neutral">覆盖 {{ fidDist.length }} 个版块</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #34d399, #10b981)">
            <el-icon><TrendCharts /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">今日发布</div>
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
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-up">今日更新 {{ overview.active_users.toLocaleString() }} 人</span>
              <span class="sub-neutral">活跃率 {{ kpiSub.activeShare ?? 0 }}%</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #fbbf24, #f59e0b)">
            <el-icon><Clock /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">最近入库</div>
            <div class="stat-value">{{ kpiSub?.latestDate ? kpiSub.latestDate.slice(5) : '—' }}</div>
            <div v-if="kpiSub" class="stat-sub">
              <span v-if="runningBatch" class="running-badge">
                <span class="running-dot"></span>抓取中 {{ runningBatch.progress ?? 0 }}%
              </span>
              <span v-else :class="kpiSub.gap.cls">{{ kpiSub.gap.text }}</span>
              <span class="sub-neutral">更新于 {{ kpiSub.updatedAt ?? '--:--' }}</span>
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

    <!-- 每日发布趋势：全站趋势 + 分版块 同行各占 1/2 -->
    <div class="trend-row">
    <div class="page-card chart-card trend-half">
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">全站发布趋势</span>
          <span v-if="linkedFid" class="link-badge" :style="{ '--link-color': linkedFid.color }">
            <span class="link-dot"></span>
            联动聚焦：{{ linkedFid.name }}
            <span class="link-close" @click="clearFidLink">✕</span>
          </span>
        </div>
        <div class="chart-head-right">
          <div v-if="trendStats" class="trend-stats">
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
            </div>
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
    <div ref="fidTrendBlockRef" class="page-card chart-card trend-half">
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">分版块发布对比</span>
          <span v-if="linkedDay" class="link-badge" style="--link-color: #e6ebf5">
            <span class="link-dot"></span>
            联动聚焦日：{{ linkedDay.slice(5) }}
            <span class="link-close" @click="clearDayLink">✕</span>
          </span>
        </div>
        <div class="chart-head-right">
          <div v-if="fidTrendStats" class="trend-stats">
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
          </div>
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
    </div>

    <!-- 图表（P0）：左活跃作者 + 右活跃版块，均为横向条形图 -->
    <div class="chart-row">
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 6px">
          <div class="chart-head-left">
            <span class="chart-title">活跃作者 Top10</span>
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
        <div class="chart-sub" style="margin-bottom: 8px">
          按{{ RANGE_LABEL[authorRange] }}发帖量 · 点击查看该作者帖子
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
        <div class="chart-head" style="margin-bottom: 6px">
          <div class="chart-head-left">
            <span class="chart-title">活跃版块 Top10</span>
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
        <div class="chart-sub" style="margin-bottom: 8px">
          按{{ RANGE_LABEL[fidRange] }}发帖量 · 点击查看该版块帖子
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
      <div class="board-row">
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <span class="chart-title">点赞最高帖</span>
            <el-link type="primary" :underline="false" class="more-link" @click="goPostsWith({ sort: 'likes_desc' })">查看更多</el-link>
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
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top" :teleported="!app.fullscreen">
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
            <span class="chart-title">回复最高帖</span>
            <el-link type="primary" :underline="false" class="more-link" @click="goPostsWith({ sort: 'replies_desc' })">查看更多</el-link>
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
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top" :teleported="!app.fullscreen">
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
            <span class="chart-title">最新最热</span>
            <span v-if="todayTop?.date" class="chart-head-right">
              <el-tooltip
                :content="`按数据最新日 ${todayTop.date}（${relDayText(todayTop.date)}）统计，当日共 ${todayTop.total} 帖；排序依据=${BOARD_SORT_HINT[todaySort]}`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="board-date">{{ todayTop.date.slice(5) }}</span>
              </el-tooltip>
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
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip v-if="isHotTalk(item)" content="热议型：回复数不低于点赞数" placement="top" :teleported="!app.fullscreen">
                <span class="board-flag">热议</span>
              </el-tooltip>
              <el-tooltip content="下载" placement="top" :teleported="!app.fullscreen">
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
            <span class="chart-title">本月最热</span>
            <span v-if="monthTop?.date" class="chart-head-right">
              <el-tooltip
                :content="`按数据最新月份 ${monthTop.date} 统计，当月共 ${monthTop.total} 帖 / 覆盖 ${monthTop.days} 天${monthTop.days < 7 ? '（样本较少，榜单波动大）' : ''}；排序依据=${BOARD_SORT_HINT[monthSort]}`"
                placement="top"
                :teleported="!app.fullscreen"
              >
                <span class="board-date">{{ monthTop.date }}</span>
              </el-tooltip>
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
          <!-- 本月每日互动量 sparkline：一眼看出热度集中在哪几天 -->
          <div v-if="monthTop?.daily?.length" class="board-spark">
            <el-tooltip
              v-for="d in monthTop.daily"
              :key="d.date"
              :content="sparkText(d)"
              placement="top"
              :teleported="!app.fullscreen"
            >
              <span class="spark-bar" :style="{ height: sparkHeight(d.value) }" />
            </el-tooltip>
          </div>
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
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip v-if="item.is_new" content="新入榜：今天首次进入 Top10" placement="top" :teleported="!app.fullscreen">
                <span class="board-new">NEW</span>
              </el-tooltip>
              <el-tooltip v-if="isHotTalk(item)" content="热议型：回复数不低于点赞数" placement="top" :teleported="!app.fullscreen">
                <span class="board-flag">热议</span>
              </el-tooltip>
              <span class="board-postdate" :title="`发布于 ${item.date}`">{{ item.date.slice(5) }}</span>
              <el-tooltip content="下载" placement="top" :teleported="!app.fullscreen">
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

.dashboard.is-fullscreen .stat-card {
  padding: 14px 18px;
}

.dashboard.is-fullscreen .stat-value {
  font-size: 32px;
}

.dashboard.is-fullscreen .board-list {
  max-height: calc(var(--chart-h) - 40px);
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

.trend-stats {
  display: flex;
  align-items: center;
  gap: 8px;
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

/* 卡片头部右侧日期角标（最新最热 / 本月最热） */
.board-date {
  flex-shrink: 0;
  font-size: 12px;
  color: #909399;
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

/* 本月每日互动量 sparkline：纯 CSS 柱状，不额外起图表实例 */
.board-spark {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 26px;
  margin: 0 0 8px;
  padding: 0 2px;
}

.spark-bar {
  flex: 1;
  min-width: 2px;
  background: #c9d7f5;
  border-radius: 1px 1px 0 0;
}

.spark-bar:hover {
  background: #2f6fed;
}

</style>
