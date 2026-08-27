<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, nextTick, type ShallowRef } from 'vue'
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
import { api, isAborted, type Boards, type FidDistItem, type Overview, type TodayTop, type TopAuthor, type TopFid, type TrendByFid, type TrendPoint } from '../api'
import { useDashboardStore } from '../stores/dashboard'
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
const p1AreaRef = ref<HTMLDivElement | null>(null)

let trendObserver: IntersectionObserver | null = null
let p1Observer: IntersectionObserver | null = null

const trendRef = shallowRef<HTMLDivElement | null>(null)
const trendChart = shallowRef<ECharts | null>(null)

// ===== 活跃作者 / 活跃版块 榜（随首屏加载，横向条形图）=====
const topAuthors = ref<TopAuthor[]>([])
const topFids = ref<TopFid[]>([])
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

// ===== 各版块每日趋势（多系列折线，懒加载）=====
// 各版块天数由总版块联动下钻控制，不再单独切换
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
// 联动：各版块图例点击高亮的版块（同步到总趋势卡片配色）
const linkedFid = ref<{ name: string; color: string } | null>(null)
// 各版块名称 -> 颜色 映射（渲染时填充）
const fidColorByName = ref<Record<string, string>>({})
// 反向联动：点总趋势某天 -> 各版块同天高亮（垂直标线）
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
  // 数据新鲜度：最新数据日期距今天数
  let gapText = '暂无数据'
  let gapCls = 'sub-neutral'
  if (o.latest_date) {
    const gap = daysBetween(o.latest_date)
    if (gap <= 0) gapText = '今天'
    else if (gap === 1) gapText = '昨天'
    else gapText = `${gap} 天前`
    gapCls = gap <= 1 ? 'sub-up' : gap <= 3 ? 'sub-neutral' : 'sub-down'
  }
  return {
    todayDiff,
    activeShare,
    gap: { cls: gapCls, text: gapText },
    latestDate: o.latest_date ?? '',
    updatedAt: o.latest_created_at ? String(o.latest_created_at).replace('T', ' ').slice(11, 16) : null,
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
      api.topAuthors(),
      api.topFids(),
    ])
    overview.value = o
    trend.value = t
    fidDist.value = f
    topAuthors.value = authors
    topFids.value = fids
    trendCache.set(trendDays.value, t)
    store.setUpdatedAt(o.latest_created_at ?? null)
    await nextTick()
    renderTrendChart()
    renderAuthorChart()
    renderFidChart()
    // 首屏：等折线逐点描线动画完成后再启动趋势 tooltip 轮播（非首屏自动刷新不中断当前轮播）
    if (initial) startTrendCarousel(trend.value.length * 24 + 900)
  } catch (e) {
    if (isAborted(e)) return
    if (!initial) return // 轮询失败静默，下轮自动重试
    ElMessage.error(`加载总览数据失败: ${(e as Error).message}`)
  } finally {
    loadingP0.value = false
  }
}

// ===== P1：懒加载热门榜（点赞/回复/今日最热/本月最热）=====
async function loadBoards() {
  if (boards.value || loadingBoards.value) return
  loadingBoards.value = true
  try {
    const [b, tt, mt] = await Promise.all([
      api.boards(),
      api.todayTop(),
      api.monthTop(),
    ])
    boards.value = b
    todayTop.value = tt
    monthTop.value = mt
  } catch (e) {
    if (isAborted(e)) return
    if (boards.value) return // 轮询刷新（榜单已存在）失败静默，下轮重试
    ElMessage.error(`加载热门榜失败: ${(e as Error).message}`)
  } finally {
    loadingBoards.value = false
  }
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
    // 双向 Tooltip 联动：总版块 ⟷ 各版块（仅注册一次）
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
          // 反向联动：点总趋势某天 -> 各版块同天高亮（取消下钻，仅保留联动）
          linkedDay.value = point.date
          renderFidTrendChart()
        }
      })
    }
    // 联动配色：当各版块图例聚焦某版块时，总趋势同步换为该版块色
    const base = linkedFid.value?.color ?? '#2f6fed'
    const data = trend.value.map((t) => t.count)
    const needZoom = trend.value.length > 31
    const avg = data.length ? Math.round(data.reduce((s, v) => s + v, 0) / data.length) : 0
    const lastIdx = data.length - 1
    const mean = avg
    const variance = data.length ? data.reduce((s, v) => s + (v - mean) ** 2, 0) / data.length : 0
    const std = Math.sqrt(variance)
    const stdUpper = mean + std
    const stdLower = Math.max(0, mean - std)
    const maxVal = data.length ? Math.max(...data) : 0
    const minVal = data.length ? Math.min(...data) : 0
    const maxIdx = data.indexOf(maxVal)
    const minIdx = data.indexOf(minVal)
    trendChart.value.setOption({
      tooltip: {
        trigger: 'axis',
        appendToBody: true,
        z: 99999,
        backgroundColor: 'rgba(31, 45, 61, 0.92)',
        borderColor: 'rgba(47, 111, 237, 0.3)',
        borderWidth: 1,
        padding: [12, 16],
        textStyle: { color: '#e5e9f0', fontSize: 13 },
        axisPointer: { type: 'line', lineStyle: { color: 'rgba(0,0,0,0.35)' } },
        extraCssText: 'border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.25); backdrop-filter: blur(4px);',
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
          html += `<span style="color:#8b95a7;font-size:12px">新增</span>`
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
        axisLabel: { color: '#6b7280', fontSize: 11 },
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
          name: '新增帖子',
          type: 'line',
          smooth: true,
          showSymbol: false,
          animation: true,
          animationDuration: 900,
          animationDurationUpdate: 600,
          animationEasing: 'cubicOut',
          animationDelay: (idx: number) => idx * 24,
          data,
          lineStyle: {
            width: 3,
            color: new graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: base },
              { offset: 1, color: '#6366f1' },
            ]),
            shadowColor: `${base}59`,
            shadowBlur: 6,
            shadowOffsetY: 2,
          },
          itemStyle: { color: base },
          areaStyle: {
            color: new graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: `${base}59` },
              { offset: 0.3, color: `${base}2e` },
              { offset: 0.65, color: `${base}1a` },
              { offset: 1, color: `${base}03` },
            ]),
          },
          markArea: {
            silent: true,
            itemStyle: { color: `${base}0f` },
            data: data.length > 1
              ? [[
                  { xAxis: 0, yAxis: stdLower },
                  { xAxis: data.length - 1, yAxis: stdUpper },
                ]]
              : [],
            label: { show: false },
          },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: 'rgba(239, 68, 68, 0.5)', type: 'dashed', width: 1 },
            label: {
              formatter: `日均 ${avg}`,
              position: 'end',
              fontSize: 10,
              color: '#ef4444',
            },
            data: [{ yAxis: avg }],
          },
        },
        ...(data.length > 1
          ? [
              {
                name: '峰值',
                type: 'effectScatter',
                coordinateSystem: 'cartesian2d',
                zlevel: 3,
                rippleEffect: { period: 3, scale: 5, brushType: 'stroke' },
                symbolSize: 14,
                itemStyle: { color: '#f59e0b' },
                label: {
                  show: true,
                  position: 'top',
                  formatter: '峰值',
                  fontSize: 10,
                  color: '#f59e0b',
                  fontWeight: 600,
                },
                data: [{ value: [maxIdx, maxVal] }],
                tooltip: { show: false },
              },
              {
                name: '谷值',
                type: 'effectScatter',
                coordinateSystem: 'cartesian2d',
                zlevel: 3,
                rippleEffect: { period: 4, scale: 3, brushType: 'stroke' },
                symbolSize: 10,
                itemStyle: { color: '#ef4444' },
                label: {
                  show: true,
                  position: 'bottom',
                  formatter: '谷值',
                  fontSize: 10,
                  color: '#ef4444',
                  fontWeight: 600,
                },
                data: [{ value: [minIdx, minVal] }],
                tooltip: { show: false },
              },
            ]
          : []),
        ...(lastIdx >= 0
          ? [
              {
                name: '最新',
                type: 'effectScatter',
                coordinateSystem: 'cartesian2d',
                zlevel: 3,
                rippleEffect: { period: 2.5, scale: 4, brushType: 'stroke' },
                symbolSize: 12,
                itemStyle: { color: '#2f6fed' },
                label: {
                  show: true,
                  position: 'top',
                  formatter: '最新',
                  fontSize: 10,
                  color: '#2f6fed',
                  fontWeight: 600,
                },
                data: [{ value: [lastIdx, data[lastIdx]] }],
                tooltip: { show: false },
              },
            ]
          : []),
      ],
    })
  }
}

function goDist(fid?: string) {
  if (fid) router.push({ path: '/posts', query: { fid } })
}

function initChart(el: HTMLDivElement): ECharts {
  return echartsInit(el)
}

/** 排名榜横向条形图通用渲染：Top-N 主数值 + 副指标 tooltip，可指定点击回调 */
function renderHBarChart(
  el: HTMLDivElement,
  chart: ShallowRef<ECharts | null>,
  lastKeyRef: { v: string },
  items: { name: string; value: number; extra: string }[],
  colors: string[],
  onClick?: (i: number) => void,
) {
  const key = items.map((d) => `${d.name}:${d.value}`).join('|')
  if (key === lastKeyRef.v) return // P1-8：数据指纹无变化跳过重绘
  lastKeyRef.v = key
  const c = chart.value ??= initChart(el)
  c.setOption(
    {
      tooltip: {
        trigger: 'item',
        appendToBody: true, // 项目规范：Tooltip 顶层
        // 鼠标不可进入 tooltip，移除后立即隐藏——避免 appendToBody 下 tooltip DOM
        // 残留在 body 内导致「鼠标移开后 tooltip 不消失」的观感
        enterable: false,
        hideDelay: 0,
        transitionDuration: 0,
        z: 99999,
        formatter: (p: any) => `${p.name}<br/>累计 ${p.value.toLocaleString()} 条<br/>${p.data.extra ?? ''}`,
      },
      grid: { left: 8, right: 44, top: 6, bottom: 6, containLabel: true },
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
            itemStyle: { color: colors[i] ?? '#6366f1', borderRadius: [0, 6, 6, 0] },
          })),
          label: { show: true, position: 'right', color: '#606266', fontSize: 11, formatter: '{c}' },
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
      value: a.total,
      extra: `今日 ${a.today} · 近 7 日 ${a.week}`,
    })),
    colors,
    (i) => goAuthor(topAuthors.value[i]?.author ?? ''),
  )
}

/** 活跃作者下钻：跳到帖子浏览页，按作者精确过滤 */
function goAuthor(author: string) {
  if (!author) return
  router.push({ path: '/posts', query: { author } })
}

/** 通用下钻：跳到帖子浏览页，带 fid/sort 等过滤条件贴合原卡片场景 */
function goPostsWith(query: Record<string, string>) {
  router.push({ path: '/posts', query })
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
      value: f.total,
      extra: `今日 ${f.today} · 近 7 日 ${f.week}`,
    })),
    topFids.value.map((f) => colorForFid(f.fid ?? '')),
    (i) => goDist(topFids.value[i]?.fid ?? undefined),
  )
}

function onResize() {
  trendChart.value?.resize()
  fidTrendChart.value?.resize()
  authorChart.value?.resize()
  fidChart.value?.resize()
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

  // 各版块趋势：懒加载（进入视口后再加载，避免首屏一次性拉取过多）
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
  window.addEventListener('resize', onResize)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

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
    ElMessage.success(`已创建下载任务（${r.count} 个链接），可在下载中心查看进度`)
  } catch (e) {
    ElMessage.error(`创建下载任务失败: ${(e as Error).message}`)
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

// ---- 每日新增趋势：tooltip 自动轮播（7 → 14 → 21 → 28 天循环）----
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
/** 总版块趋势图 Tooltip 轮播：悬停暂停 / 移出恢复（方法包装，规避 ts-plugin 对 let 变量模板内联赋值的类型收窄误报） */
function setTrendTipPaused(paused: boolean) {
  trendTipPaused = paused
}
/** 各版块趋势图 Tooltip 轮播：悬停暂停 / 移出恢复（同上） */
function setFidTrendTipPaused(paused: boolean) {
  fidTrendTipPaused = paused
}
// 总版块与各版块 Tooltip 联动：仅注册一次
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
    // 悬停总版块或各版块任一张卡片时，暂停自动轮播；离开后恢复
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
    // 自动切换天数：同步联动各版块（复用 onTrendDaysChange 的下钻逻辑）
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
    ElMessage.error(`加载趋势数据失败: ${(e as Error).message}`)
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
  // 各版块由总版块联动下钻：已加载或可见时同步刷新
  if (fidTrendVisible.value || fidTrend.value.dates.length) {
    loadFidTrend()
  }
}

// ===== 各版块每日趋势（多系列折线）=====
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

/** 懒加载：进入视口后首次拉取，之后由总版块联动下钻切换天数走缓存 */
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
    ElMessage.error(`加载各版块趋势失败: ${(e as Error).message}`)
  } finally {
    loadingFidTrend.value = false
    fidTrendSwitching.value = false
  }
}

/** 清除各版块联动聚焦 */
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
    // 反联动：各版块 Tooltip 出现时，同步显示总版块对应天数的 Tooltip（双向）
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
    // 与总版块趋势图保持完全一致的绘图区，使 Y 轴高度对齐
    grid: { top: 30, right: 20, bottom: needZoom ? 46 : 28, left: 44 },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      z: 99999,
      backgroundColor: 'rgba(20,28,48,0.92)',
      borderColor: 'rgba(255,255,255,0.12)',
      borderWidth: 1,
      textStyle: { color: '#e6ebf5', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: 'rgba(0,0,0,0.35)' } },
      // 各版块按当日新增从大到小排序展示
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
  <div>
    <!-- 统计卡片 -->
    <div class="stat-grid">
      <template v-if="overview">
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #4f83f1, #2f6fed)">
            <el-icon><Collection /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">累计帖子</div>
            <div class="stat-value"><RollingNumber :value="overview.total" /></div>
            <div class="stat-sub">
              <span class="sub-up">近7日 +{{ overview.week_new.toLocaleString() }}</span>
              <span class="sub-neutral">覆盖 {{ fidDist.length }} 个版块</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #34d399, #10b981)">
            <el-icon><TrendCharts /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">今日新增</div>
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
            <div class="stat-label">累计作者</div>
            <div class="stat-value"><RollingNumber :value="overview.total_users" /></div>
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-up">今日发帖 {{ overview.active_users.toLocaleString() }}</span>
              <span class="sub-neutral">活跃率 {{ kpiSub.activeShare ?? 0 }}%</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #fbbf24, #f59e0b)">
            <el-icon><Clock /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">数据新鲜度</div>
            <div class="stat-value">{{ kpiSub?.latestDate ? kpiSub.latestDate.slice(5) : '—' }}</div>
            <div v-if="kpiSub" class="stat-sub">
              <span :class="kpiSub.gap.cls">{{ kpiSub.gap.text }}</span>
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

    <!-- 每日新增趋势：总版块 + 各版块 同行各占 1/2 -->
    <div class="trend-row">
    <div class="page-card chart-card trend-half">
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">总版块</span>
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

    <!-- 每日新增趋势（各版块）：同行右侧 1/2 宽，懒加载 -->
    <div ref="fidTrendBlockRef" class="page-card chart-card trend-half">
      <div class="chart-head">
        <div class="chart-head-left">
          <span class="chart-title">各版块</span>
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
        <div class="chart-head" style="margin-bottom: 8px">
          <span class="chart-title">活跃作者 Top10</span>
          <span class="chart-sub">按累计发帖量</span>
        </div>
        <div class="chart-wrap">
          <div v-if="!topAuthors.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
          <div v-else ref="authorChartRef" class="chart"></div>
        </div>
      </div>
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 8px">
          <span class="chart-title">活跃版块 Top10</span>
          <span class="chart-sub">按累计发帖量 · 点击查看版块</span>
        </div>
        <div class="chart-wrap">
          <div v-if="!topFids.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
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
              <el-tag size="small" type="danger" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top">
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
              <el-tag size="small" type="warning" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.stop.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top">
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
            <span class="chart-title">今日最热</span>
            <span v-if="todayTop?.date" class="board-date">{{ todayTop.date.slice(5) }}</span>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in todayTop?.items ?? []" :key="item.url" class="board-card" @click="openUrl(item.url)">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="danger" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>
              <span class="board-metric">
                <el-icon><Star /></el-icon>{{ metricText(item.likes) }}
              </span>
              <span class="board-metric">
                <el-icon><ChatDotRound /></el-icon>{{ metricText(item.replies) }}
              </span>
            </div>
            <div v-if="!todayTop?.items?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <span class="chart-title">本月最热</span>
            <span v-if="monthTop?.date" class="board-date">{{ monthTop.date }}</span>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in monthTop?.items ?? []" :key="item.url" class="board-card" @click="openUrl(item.url)">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="success" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <el-tooltip content="下载" placement="top">
                <el-button link size="small" type="success" :icon="Download" class="board-download" @click.stop.prevent="downloadUrl(item.url)" />
              </el-tooltip>
              <span class="board-metric">
                <el-icon><Star /></el-icon>{{ metricText(item.likes) }}
              </span>
              <span class="board-metric">
                <el-icon><ChatDotRound /></el-icon>{{ metricText(item.replies) }}
              </span>
            </div>
            <div v-if="!monthTop?.items?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
      </div>

    </div>
  </div>
</template>

<style scoped>
/* 每日新增趋势 + 版块分布：左右 1:1 等宽，与热门榜保持一致间距 */
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

/* 每日新增趋势：总版块 + 各版块 同行各占 1/2 */
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
  height: 320px;
  width: 100%;
  overflow: hidden; /* 遏制 ECharts canvas 初始化瞬间的横向溢出 */
  min-width: 0;
}
/* 半宽卡片：图表略矮，左右等高对齐 */
.trend-half .trend-chart-wrap {
  height: 300px;
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
  height: 320px; /* 与排行榜 8 行可视区等高 */
  overflow: hidden; /* 遏制 ECharts / 排行榜容器瞬时横向溢出 */
  min-width: 0;
}

/* 卡片头部右侧日期角标（今日最热 / 本月最热） */
.board-date {
  flex-shrink: 0;
  font-size: 12px;
  color: #909399;
}

/* 卡片头部说明文字（活跃作者榜） */
.chart-sub {
  font-size: 12px;
  color: #909399;
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

</style>
