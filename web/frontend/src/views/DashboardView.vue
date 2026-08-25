<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { graphic, init as echartsInit, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart, EffectScatterChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from 'echarts/components'
import type { ECharts } from 'echarts/core'
import { ElMessage } from 'element-plus'
import { api, type Boards, type FidDistItem, type Overview, type TrendByFid, type TrendPoint } from '../api'
import { useDashboardStore } from '../stores/dashboard'
import RollingNumber from '../components/RollingNumber.vue'

use([
  CanvasRenderer,
  LineChart,
  PieChart,
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
const loadingBoards = ref(false)
const p1AreaRef = ref<HTMLDivElement | null>(null)

let trendObserver: IntersectionObserver | null = null
let p1Observer: IntersectionObserver | null = null

const trendRef = shallowRef<HTMLDivElement | null>(null)
const pieRef = shallowRef<HTMLDivElement | null>(null)
const trendChart = shallowRef<ECharts | null>(null)
const pieChart = shallowRef<ECharts | null>(null)

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

// 环形图中心动态信息（悬停版块时切换）
const centerInfo = ref<{ name: string; count: string; pct: string; color: string } | null>(null)
// 环形图中心位置随图例方向变化
const pieCenterStyle = ref({ left: '38%', top: '50%' })

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

const fidDistTotal = computed(() => fidDist.value.reduce((s, f) => s + f.count, 0))

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

const totalText = computed(() => (overview.value?.total ?? 0).toLocaleString())

// 指标卡副指标：环比 / 占比 / 日均
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
  const yesterdayShare = o.week_new > 0 ? ((o.yesterday / o.week_new) * 100).toFixed(0) : null
  const weekAvg = Math.round(o.week_new / 7)
  const activeShare = o.total_users > 0 ? ((o.active_users / o.total_users) * 100).toFixed(1) : null
  return { todayDiff, yesterdayShare, weekAvg, activeShare }
})

// ===== P0：首屏加载（KPI + 趋势 + 分布）=====
async function loadP0(initial = false) {
  if (initial) loadingP0.value = true
  try {
    const [o, t, f] = await Promise.all([
      api.overview(),
      api.trend(trendDays.value),
      api.fidDist(),
    ])
    overview.value = o
    trend.value = t
    fidDist.value = f
    trendCache.set(trendDays.value, t)
    store.setUpdatedAt(o.latest_created_at ?? null)
    await nextTick()
    renderTrendChart()
    renderDistChart()
    // 数据更新后同时重启排行榜轮播与环形图轮播
    startPieCarousel()
    startBarScroll()
    // 首屏：等折线逐点描线动画完成后再启动趋势 tooltip 轮播（非首屏自动刷新不中断当前轮播）
    if (initial) startTrendCarousel(trend.value.length * 24 + 900)
  } catch (e) {
    ElMessage.error(`加载总览数据失败: ${(e as Error).message}`)
  } finally {
    loadingP0.value = false
  }
}

// ===== P1：懒加载热门榜 =====
async function loadBoards() {
  if (boards.value || loadingBoards.value) return
  loadingBoards.value = true
  try {
    const b = await api.boards()
    boards.value = b
  } catch (e) {
    ElMessage.error(`加载热门榜失败: ${(e as Error).message}`)
  } finally {
    loadingBoards.value = false
  }
}

function renderTrendChart() {
  if (trendRef.value) {
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
}

/** 版块分布渲染：环形图，支持点击跳转与悬停联动 */
function renderDistChart() {
  if (!pieRef.value) return
  const chart = pieChart.value ??= initChart(pieRef.value)
  const total = fidDist.value.reduce((s, f) => s + f.count, 0)
  const wide = window.innerWidth >= 1440

  // 图例方向随视口自适应：大屏右侧纵向，中/小屏底部横向
  // selectedMode: false 避免点击图例隐藏扇区，改为触发行跳转
  const legend: Record<string, unknown> = {
    type: 'scroll',
    selectedMode: false,
    orient: wide ? 'vertical' : 'horizontal',
    top: wide ? 'middle' : undefined,
    right: wide ? 8 : undefined,
    left: wide ? undefined : 'center',
    bottom: wide ? undefined : 0,
    textStyle: { fontSize: 12 },
  }
  pieCenterStyle.value = wide ? { left: '38%', top: '50%' } : { left: '50%', top: '42%' }

  chart.setOption(
    {
      tooltip: {
        trigger: 'item',
        appendToBody: true,
        z: 99999,
        formatter: (p: any) => `${p.name}<br/>${p.value.toLocaleString()} 条（${p.percent}%）`,
      },
      legend,
      series: [
        {
          name: '版块分布',
          type: 'pie',
          radius: ['42%', '68%'],
          center: wide ? ['38%', '50%'] : ['50%', '44%'],
          avoidLabelOverlap: true,
          itemStyle: { borderRadius: 6, borderColor: '#fff', borderWidth: 2 },
          // 仅对占比 >=4% 的版块显示外部标签 + 引导线，避免 13 项重叠
          label: {
            show: true,
            fontSize: 11,
            color: '#606266',
            formatter: (p: any) => (p.percent >= 4 ? `${p.data.labelText}\n${p.percent}%` : ''),
          },
          labelLine: { length: 12, length2: 8, smooth: true },
          emphasis: { label: { show: true, fontSize: 14, fontWeight: 600 }, scale: true, scaleSize: 10 },
          data: fidDist.value.map((f) => ({
            name: `${f.name}(${f.fid})`,
            labelText: f.name,
            value: f.count,
            fid: f.fid,
            itemStyle: { color: colorForFid(f.fid) },
          })),
        },
      ],
    },
    true,
  )

  chart.off('mouseover')
  chart.off('mouseout')
  chart.off('click')
  chart.off('legendselectchanged')
  chart.on('legendselectchanged', (p: any) => {
    const name: string = p.name ?? ''
    const match = fidDist.value.find((f) => name.includes(`(${f.fid})`))
    if (match) goDist(match.fid)
  })
  chart.on('mouseover', (p: any) => {
    // 用户悬停时暂停自动轮播，避免抢占
    pieHover = true
    if (p.componentType === 'series' && p.data) {
      updatePieCenter(p.data, total)
    }
  })
  chart.on('mouseout', () => {
    pieHover = false
    // 离开后恢复轮播展示，中心信息同步到当前轮播项
    if (pieTimer) {
      const f = fidDist.value[pieIndex]
      if (f) updatePieCenter(
        { labelText: f.name, value: f.count, fid: f.fid },
        fidDistTotal.value,
      )
    }
  })
  chart.on('click', (p: any) => goDist(p.data?.fid))
}

function goDist(fid?: string) {
  if (fid) router.push({ path: '/posts', query: { fid } })
}

function initChart(el: HTMLDivElement): ECharts {
  return echartsInit(el)
}

function onResize() {
  trendChart.value?.resize()
  pieChart.value?.resize()
  fidTrendChart.value?.resize()
  renderDistChart()
}

function syncAutoRefresh() {
  if (store.autoRefresh) {
    if (!refreshTimer) refreshTimer = setInterval(() => autoRefreshTick(), REFRESH_INTERVAL)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

/** 自动刷新：仅刷新已加载区块；懒加载区块若已在视口内则一并刷新 */
function autoRefreshTick() {
  if (overview.value || loadingP0.value) loadP0(false)
  if (boards.value) loadBoards()
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
  stopBarScroll()
  stopPieCarousel()
  stopTrendCarousel()
  store.registerAutoChange(null)
  window.removeEventListener('resize', onResize)
  trendChart.value?.dispose()
  pieChart.value?.dispose()
})

function openUrl(url: string) {
  window.open(url, '_blank', 'noopener')
}

function goPosts() {
  router.push('/posts')
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

// ---- 版块分布排行榜：DataV 风格无缝循环单条轮播 ----
// 容器固定显示 N 行，每隔 waitTime 平滑向上滚动一行；
// 排行榜：固定 8 行可视区；数据超过 8 行时轮播，末尾补位 (N-1) 行实现无缝循环、无空白。
const barListRef = ref<HTMLDivElement | null>(null)
const BAR_ROW_HEIGHT = 40 // px，与 CSS 中 .bar-row 高度保持一致
const BAR_VISIBLE_ROWS = 8 // 可视区固定展示行数
const BAR_WAIT_TIME = 2000 // ms
let barTimer: ReturnType<typeof setInterval> | null = null
let barIndex = 0

// C2：排序维度切换（总量 / 今日 / 昨日），切换后基于新字段重排并重置轮播
type BarSortField = 'count' | 'today' | 'yesterday'
const barSortField = ref<BarSortField>('count')
const barSortOptions = [
  { value: 'count', label: '总量' },
  { value: 'today', label: '今日' },
  { value: 'yesterday', label: '昨日' },
] as const

const barSortValue = (f: BarSortField, item: FidDistItem): number => {
  if (f === 'today') return item.today_count ?? 0
  if (f === 'yesterday') return item.yesterday_count ?? 0
  return item.count ?? 0
}

// 按当前排序维度生成榜单数据（A2：携带 rank 用于高亮前三名）
const barRankedItems = computed(() => {
  const f = barSortField.value
  return [...fidDist.value]
    .sort((a, b) => barSortValue(f, b) - barSortValue(f, a))
    .map((item, i) => ({ item, rank: i + 1 }))
})

// 名次徽章按真实排名计算；末尾补位 (N-1) 行复用前几行的名次（1~N-1），实现无缝循环
const barLoopItems = computed(() => {
  const items = barRankedItems.value
  const padCount = Math.max(0, BAR_VISIBLE_ROWS - 1)
  const pad = items
    .slice(0, Math.min(padCount, items.length))
    .map((item) => ({ item: item.item, rank: item.rank }))
  return [...items, ...pad]
})

// 行内增量（B1：今日较昨日）——返回 { delta, cls }
function barDeltaOf(item: FidDistItem) {
  const today = item.today_count ?? 0
  const yesterday = item.yesterday_count ?? 0
  const delta = today - yesterday
  const cls = delta > 0 ? 'delta-up' : delta < 0 ? 'delta-down' : 'delta-flat'
  return { delta, cls }
}

// A1：占比进度条宽度（%），基于当前排序维度数值相对列表最大值
function barPctOf(item: FidDistItem): number {
  const f = barSortField.value
  const max = barRankedItems.value[0]
  if (!max) return 0
  const denom = barSortValue(f, max.item)
  if (!denom) return 0
  return Math.max(2, (barSortValue(f, item) / denom) * 100)
}

function onBarSortChange() {
  // 重置轮播到顶部，保证切换后首屏展示新排序的前 8 名
  const el = barListRef.value
  if (el) el.scrollTop = 0
  barIndex = 0
  startBarScroll()
}

// C1：悬停排行榜某行时，环形图同步高亮对应扇区（联动）；移出时恢复轮播展示
function barHoverFid(fid: string) {
  const idx = fidDist.value.findIndex((f) => f.fid === fid)
  if (idx < 0) return
  const chart = pieChart.value
  if (!chart) return
  // 悬停期间暂停环形图自动轮播，避免抢占
  pieHover = true
  if (pieTimer) {
    clearInterval(pieTimer)
    pieTimer = null
  }
  chart.dispatchAction({ type: 'downplay', seriesIndex: 0 })
  chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
  const f = fidDist.value[idx]
  if (f) updatePieCenter({ labelText: f.name, value: f.count, fid: f.fid }, fidDistTotal.value)
}

function barUnhover() {
  pieHover = false
  // 离开后恢复环形图自动轮播（若未暂停）
  if (!pieTimer && pieChart.value && fidDist.value.length > 1) startPieCarousel()
}

// 固定 8 行可视区高度，与 CSS 中 .bar-list 保持一致
const barListStyle = computed(() => ({ height: `${BAR_VISIBLE_ROWS * BAR_ROW_HEIGHT}px` }))

// D1：轮播当前行高亮跟随（仅在有轮播时更新）
const barActiveIndex = ref(-1)

function startBarScroll() {
  stopBarScroll()
  const el = barListRef.value
  if (!el || fidDist.value.length <= BAR_VISIBLE_ROWS) return
  barIndex = 0
  el.scrollTop = 0
  barActiveIndex.value = 0
  barTimer = setInterval(() => {
    const maxIndex = Math.max(0, barLoopItems.value.length - BAR_VISIBLE_ROWS)
    barIndex = barIndex >= maxIndex ? 0 : barIndex + 1
    el.scrollTop = barIndex * BAR_ROW_HEIGHT
    barActiveIndex.value = barIndex
  }, BAR_WAIT_TIME)
}

function stopBarScroll() {
  if (barTimer) {
    clearInterval(barTimer)
    barTimer = null
  }
  barIndex = 0
  barActiveIndex.value = -1
  const el = barListRef.value
  if (el) el.scrollTop = 0
}

function pauseBarScroll() {
  if (barTimer) {
    clearInterval(barTimer)
    barTimer = null
  }
}

function resumeBarScroll() {
  if (!barTimer) startBarScroll()
}

// C3：行悬停自定义 Tooltip（替代原生 title）
const barTip = ref<{ x: number; y: number; item: FidDistItem; rank: number } | null>(null)
function barShowTip(e: MouseEvent, item: FidDistItem, rank: number) {
  barTip.value = { x: e.clientX, y: e.clientY, item, rank }
}
function barHideTip() {
  barTip.value = null
}

// ---- 版块分布环形图：自动轮播高亮 ----
const PIE_CAROUSEL_INTERVAL = 2200
let pieTimer: ReturnType<typeof setInterval> | null = null
let pieIndex = 0
let pieHover = false

function updatePieCenter(data: any, total: number) {
  const f = data ?? {}
  const pct = total ? ((Number(f.value) || 0) / total) * 100 : 0
  centerInfo.value = {
    name: f.labelText ?? f.name ?? '',
    count: Number(f.value || 0).toLocaleString(),
    pct: pct.toFixed(1),
    color: colorForFid(String(f.fid ?? '')),
  }
}

function startPieCarousel() {
  stopPieCarousel()
  const chart = pieChart.value
  if (!chart || !fidDist.value.length) return
  const n = fidDist.value.length
  if (n < 2) return
  pieIndex = 0
  pieHover = false
  chart.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: 0 })
  updatePieCenter(
    { labelText: fidDist.value[0].name, value: fidDist.value[0].count, fid: fidDist.value[0].fid },
    fidDistTotal.value,
  )
  pieTimer = setInterval(() => {
    if (pieHover) return
    const c = pieChart.value
    if (!c || !fidDist.value.length) return
    const n2 = fidDist.value.length
    c.dispatchAction({ type: 'downplay', seriesIndex: 0 })
    pieIndex = (pieIndex + 1) % n2
    c.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: pieIndex })
    const f = fidDist.value[pieIndex]
    if (f) updatePieCenter({ labelText: f.name, value: f.count, fid: f.fid }, fidDistTotal.value)
  }, PIE_CAROUSEL_INTERVAL)
}

function stopPieCarousel() {
  if (pieTimer) {
    clearInterval(pieTimer)
    pieTimer = null
  }
  pieIndex = 0
  pieHover = false
  pieChart.value?.dispatchAction({ type: 'downplay', seriesIndex: 0 })
}

// ---- 每日新增趋势：tooltip 自动轮播（7 → 30 → 60 → 90 天循环）----
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

/** 当前维度展示完成：间隔后切换到下一数据范围（7 → 30 → 60 → 90 → 7）；
 *  切换到 30/60/90 天时轮播起点与上一阶段终点衔接（时间轴连续向左推进），
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

// ===== 联动扩展：linkedFid 同步高亮 排行榜行 + 环形图扇区 =====
/** 将排行榜滚动定位到指定版块行（暂停自动轮播，保证高亮行可见） */
function scrollBarToFid(fid: string) {
  const el = barListRef.value
  if (!el) return
  const rank = barRankedItems.value.findIndex((r) => r.item.fid === fid)
  if (rank < 0) return
  pauseBarScroll()
  const top = Math.min(rank, Math.max(0, barLoopItems.value.length - BAR_VISIBLE_ROWS))
  el.scrollTop = top * BAR_ROW_HEIGHT
  barIndex = top
  barActiveIndex.value = top
}

/** 环形图高亮指定版块扇区（复用悬停联动逻辑） */
function pieHighlightFid(fid: string) {
  const idx = fidDist.value.findIndex((f) => f.fid === fid)
  if (idx < 0 || !pieChart.value) return
  pieHover = true
  if (pieTimer) {
    clearInterval(pieTimer)
    pieTimer = null
  }
  pieChart.value.dispatchAction({ type: 'downplay', seriesIndex: 0 })
  pieChart.value.dispatchAction({ type: 'highlight', seriesIndex: 0, dataIndex: idx })
  const f = fidDist.value[idx]
  if (f) updatePieCenter({ labelText: f.name, value: f.count, fid: f.fid }, fidDistTotal.value)
}

/** 监听 linkedFid：聚焦时联动排行榜 + 环形图，取消时恢复 */
watch(linkedFid, (val) => {
  if (val) {
    const fid = fidColorToFid(val.name)
    if (fid != null) {
      scrollBarToFid(fid)
      pieHighlightFid(fid)
    }
  } else {
    // 恢复排行榜轮播（数据超 8 行才需要）
    if (fidDist.value.length > BAR_VISIBLE_ROWS) resumeBarScroll()
    // 恢复环形图轮播
    pieHover = false
    if (!pieTimer && pieChart.value && fidDist.value.length > 1) startPieCarousel()
  }
})

/** 由版块名反查 fid（联动时版块名称在各数据源一致） */
function fidColorToFid(name: string): string | null {
  const item = fidDist.value.find((f) => f.name === name)
  return item ? item.fid : null
}

function renderFidTrendChart() {
  const el = fidTrendRef.value
  if (!el) return
  const dates = fidTrend.value.dates
  const series = fidTrend.value.series
  if (!dates.length || !series.length) return

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
              <span class="sub-up">今日新增 +{{ overview.today.toLocaleString() }}</span>
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
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #fbbf24, #f59e0b)">
            <el-icon><Calendar /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">昨日新增</div>
            <div class="stat-value"><RollingNumber :value="overview.yesterday" /></div>
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-neutral">占近 7 日 {{ kpiSub.yesterdayShare ?? 0 }}%</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #a78bfa, #8b5cf6)">
            <el-icon><DataLine /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">近 7 日新增</div>
            <div class="stat-value"><RollingNumber :value="overview.week_new" /></div>
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-neutral">日均 {{ kpiSub.weekAvg }} 条</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #f87171, #ef4444)">
            <el-icon><User /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">累计用户</div>
            <div class="stat-value"><RollingNumber :value="overview.total_users" /></div>
            <div class="stat-sub">
              <span class="sub-neutral">今日活跃 {{ overview.active_users.toLocaleString() }}</span>
            </div>
          </div>
        </div>
        <div class="stat-card">
          <div class="stat-icon" style="background: linear-gradient(135deg, #22d3ee, #06b6d4)">
            <el-icon><UserFilled /></el-icon>
          </div>
          <div class="stat-body">
            <div class="stat-label">活跃用户</div>
            <div class="stat-value"><RollingNumber :value="overview.active_users" /></div>
            <div v-if="kpiSub" class="stat-sub">
              <span class="sub-neutral">占累计 {{ kpiSub.activeShare ?? 0 }}%</span>
            </div>
          </div>
        </div>
      </template>
      <template v-else>
        <div v-for="i in 6" :key="i" class="stat-card">
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
          @mouseenter="trendTipPaused = true"
          @mouseleave="trendTipPaused = false"
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
          @mouseenter="fidTrendTipPaused = true"
          @mouseleave="fidTrendTipPaused = false"
        ></div>
        <div v-if="fidTrendSwitching" class="chart-switch-overlay">
          <span class="switch-dot"></span>
          <span>数据切换中…</span>
        </div>
      </div>
    </div>
    </div>

    <!-- 图表（P0）：左排行榜 + 右环形图，同时展示 -->
    <div class="chart-row">
      <div class="page-card chart-card">
        <div class="chart-head bar-head">
          <span class="chart-title">版块分布 · 排行榜</span>
          <!-- C2：排序维度切换 -->
          <el-radio-group v-model="barSortField" size="small" @change="onBarSortChange">
            <el-radio-button v-for="opt in barSortOptions" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </el-radio-button>
          </el-radio-group>
        </div>
        <div class="chart-wrap">
          <div v-if="!fidDist.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
          <!-- 排行榜：DataV 风格排名列表；固定 8 行可视区轮播，末尾补位实现无缝循环、无空白 -->
          <div
            v-else
            ref="barListRef"
            class="bar-list"
            :style="barListStyle"
            @mouseenter="pauseBarScroll"
            @mouseleave="resumeBarScroll"
          >
            <div
              v-for="(row, i) in barLoopItems"
              :key="`${row.item.fid}-${i}`"
              class="bar-row"
              :class="[
                `rank-${row.rank}`,
                row.rank === 1 ? 'bar-top' : '',
                i === barActiveIndex ? 'is-active' : '',
                linkedFid && row.item.name === linkedFid.name ? 'is-linked' : '',
              ]"
              :style="{
                '--bar-width': `${barPctOf(row.item)}%`,
                '--bar-progress': colorForFid(row.item.fid),
              }"
              @click="goDist(row.item.fid)"
              @mouseenter="barHoverFid(row.item.fid); barShowTip($event, row.item, row.rank)"
              @mouseleave="barUnhover; barHideTip()"
            >
              <span :class="rankClass(row.rank - 1)" class="bar-rank">{{ row.rank }}</span>
              <span class="bar-name">{{ row.item.name }}({{ row.item.fid }})</span>
              <span class="bar-sub">
                <template v-if="row.item.today_count != null">今日新增 {{ row.item.today_count.toLocaleString() }}</template>
                <template v-else>最新 {{ row.item.latest_date ?? '-' }}</template>
              </span>
              <div class="bar-metric">
                <!-- B1：今日较昨日增量趋势箭头 -->
                <span
                  v-if="row.item.today_count != null"
                  class="bar-delta"
                  :class="barDeltaOf(row.item).cls"
                  :title="`较昨日 ${barDeltaOf(row.item).delta >= 0 ? '+' : ''}${barDeltaOf(row.item).delta}`"
                >
                  <el-icon>
                    <CaretTop v-if="barDeltaOf(row.item).delta > 0" />
                    <CaretBottom v-else-if="barDeltaOf(row.item).delta < 0" />
                    <Minus v-else />
                  </el-icon>
                  <span>{{ Math.abs(barDeltaOf(row.item).delta).toLocaleString() }}</span>
                </span>
                <span class="bar-value">{{ row.item.count.toLocaleString() }} 条</span>
                <span class="bar-pct">({{ fidDistTotal ? ((row.item.count / fidDistTotal) * 100).toFixed(1) : 0 }}%)</span>
              </div>
            </div>
          </div>
          <!-- B3：底部汇总条（覆盖固定在底部，不参与轮播）+ 上方渐变遮罩 -->
          <div v-if="fidDist.length" class="bar-total-mask"></div>
          <div v-if="fidDist.length" class="bar-total-bar">
            <span>共 {{ fidDist.length }} 个版块</span>
            <span class="bar-total-divider"></span>
            <span>累计 <b>{{ fidDistTotal.toLocaleString() }}</b> 条</span>
          </div>
        </div>

        <!-- C3：行悬停自定义 Tooltip -->
        <teleport to="body">
          <div
            v-if="barTip"
            class="bar-tip"
            :style="{ left: `${barTip.x + 12}px`, top: `${barTip.y + 12}px` }"
          >
            <div class="bar-tip-title">
              <span :class="rankClass(barTip.rank - 1)" class="bar-tip-rank">{{ barTip.rank }}</span>
              <span class="bar-tip-name">{{ barTip.item.name }}({{ barTip.item.fid }})</span>
            </div>
            <div class="bar-tip-row">
              <span>累计总量</span>
              <span class="bar-tip-strong">{{ barTip.item.count.toLocaleString() }} 条</span>
            </div>
            <div class="bar-tip-row">
              <span>占总量</span>
              <span>{{ fidDistTotal ? ((barTip.item.count / fidDistTotal) * 100).toFixed(1) : 0 }}%</span>
            </div>
            <div class="bar-tip-row">
              <span>今日新增</span>
              <span>{{ barTip.item.today_count?.toLocaleString() ?? '-' }}</span>
            </div>
            <div class="bar-tip-row">
              <span>昨日新增</span>
              <span>{{ barTip.item.yesterday_count?.toLocaleString() ?? '-' }}</span>
            </div>
            <div class="bar-tip-row">
              <span>最近抓取</span>
              <span>{{ barTip.item.latest_date ?? '-' }}</span>
            </div>
          </div>
        </teleport>
      </div>
      <div class="page-card chart-card">
        <div class="chart-head">
          <span class="chart-title">版块分布 · 环形图</span>
        </div>
        <div class="chart-wrap">
          <div v-if="!fidDist.length && loadingP0" class="chart chart-loading">
            <el-skeleton animated :rows="8" />
          </div>
          <template v-else>
            <div ref="pieRef" class="chart"></div>
            <div v-if="overview" class="pie-center" :style="pieCenterStyle">
              <template v-if="centerInfo">
                <div class="pie-center-value" :style="{ color: centerInfo.color }">{{ centerInfo.count }}</div>
                <div class="pie-center-label">{{ centerInfo.name }} · {{ centerInfo.pct }}%</div>
              </template>
              <template v-else>
                <div class="pie-center-value">{{ totalText }}</div>
                <div class="pie-center-label">累计帖子</div>
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 热门榜 + 最近抓取（P1：懒加载） -->
    <div ref="p1AreaRef">
      <!-- 热门榜 -->
      <div class="board-row">
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <span class="chart-title">点赞最高帖（各版块）</span>
            <el-link type="primary" :underline="false" class="more-link" @click="goPosts">查看更多</el-link>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in boards?.top_likes ?? []" :key="item.fid" class="board-card" @click="openUrl(item.url)">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="danger" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <span class="board-metric">
                <el-icon><Star /></el-icon>{{ metricText(item.value) }}
              </span>
            </div>
            <div v-if="!boards?.top_likes?.length" class="text-muted">暂无数据</div>
          </div>
        </div>
        <div class="page-card chart-card">
          <div class="chart-head" style="margin-bottom: 8px">
            <span class="chart-title">回复最高帖（各版块）</span>
            <el-link type="primary" :underline="false" class="more-link" @click="goPosts">查看更多</el-link>
          </div>
          <div v-if="loadingBoards" class="board-list">
            <div v-for="i in 4" :key="i" class="board-card">
              <el-skeleton animated :rows="1" />
            </div>
          </div>
          <div v-else class="board-list">
            <div v-for="(item, i) in boards?.top_replies ?? []" :key="item.fid" class="board-card" @click="openUrl(item.url)">
              <span :class="rankClass(i)">{{ i + 1 }}</span>
              <el-tag size="small" type="warning" class="board-tag">{{ item.name }}</el-tag>
              <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
                {{ item.title }}
              </a>
              <span class="board-metric">
                <el-icon><ChatDotRound /></el-icon>{{ metricText(item.value) }}
              </span>
            </div>
            <div v-if="!boards?.top_replies?.length" class="text-muted">暂无数据</div>
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

/* 热门榜：左右 1:1 等宽 */
.board-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
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

.pie-center {
  position: absolute;
  transform: translate(-50%, -50%);
  text-align: center;
  pointer-events: none;
}

.pie-center-value {
  font-size: 24px;
  font-weight: 700;
  color: #1f2d3d;
  line-height: 1.2;
}

.pie-center-label {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
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

.bar-list {
  display: flex;
  flex-direction: column;
  padding: 0 4px;
  height: 320px; /* 8 行 * 40px，与 JS 常量 BAR_VISIBLE_ROWS * BAR_ROW_HEIGHT 保持一致 */
  overflow: hidden;
  scroll-behavior: smooth;
}

.bar-row {
  display: grid;
  grid-template-columns: 28px minmax(80px, 1fr) auto auto;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  height: 40px;
  box-sizing: border-box;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease;
  font-size: 13px;
  flex-shrink: 0;
  /* A1：占比进度条锚点 */
  position: relative;
  overflow: hidden;
}
/* A1：行底部占比进度条（静态主条 + 淡色底槽） */
.bar-row::before {
  content: '';
  position: absolute;
  left: 38px;
  right: 10px;
  bottom: 4px;
  height: 3px;
  border-radius: 2px;
  background: rgba(127, 141, 163, 0.16);
}
.bar-row::after {
  content: '';
  position: absolute;
  left: 38px;
  bottom: 4px;
  height: 3px;
  width: var(--bar-width, 0%);
  border-radius: 2px;
  background: linear-gradient(90deg, var(--bar-progress, #2f6fed), #6366f1);
  transform-origin: left center;
  transition: width 0.5s cubic-bezier(0.22, 1, 0.36, 1);
}

/* A2：前三名行高亮（左侧色条 + 淡渐变背景 + 冠军发光） */
.bar-row.rank-1 {
  background: linear-gradient(90deg, rgba(251, 191, 36, 0.10), rgba(251, 191, 36, 0.02));
  box-shadow: inset 3px 0 0 #fbbf24;
}
.bar-row.rank-1.bar-top {
  box-shadow: inset 3px 0 0 #fbbf24, 0 1px 8px rgba(251, 191, 36, 0.28);
}
.bar-row.rank-2 {
  background: linear-gradient(90deg, rgba(203, 213, 225, 0.14), rgba(203, 213, 225, 0.02));
  box-shadow: inset 3px 0 0 #cbd5e1;
}
.bar-row.rank-3 {
  background: linear-gradient(90deg, rgba(232, 161, 124, 0.14), rgba(232, 161, 124, 0.02));
  box-shadow: inset 3px 0 0 #e8a17c;
}

.bar-row:hover {
  background: #f0f4ff;
}

.bar-rank {
  width: 22px;
  height: 22px;
  border-radius: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.bar-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #1f2d3d;
  font-weight: 500;
}

.bar-sub {
  color: #909399;
  font-size: 12px;
  white-space: nowrap;
}

.bar-metric {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 6px;
  white-space: nowrap;
}

.bar-value {
  color: #303133;
  font-weight: 600;
  white-space: nowrap;
}

.bar-pct {
  color: #909399;
  font-size: 12px;
  white-space: nowrap;
}

/* B1：今日较昨日增量趋势箭头 */
.bar-delta {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.bar-delta .el-icon {
  font-size: 13px;
}
.bar-delta.delta-up { color: #10b981; }
.bar-delta.delta-down { color: #ef4444; }
.bar-delta.delta-flat { color: #909399; }

/* C2：排序切换头部与标题间距 */
.bar-head {
  gap: 8px;
}

/* A3：前三名名次徽章圆形化、略放大 */
.bar-row.rank-1 .bar-rank,
.bar-row.rank-2 .bar-rank,
.bar-row.rank-3 .bar-rank {
  width: 24px;
  height: 24px;
  border-radius: 50%;
}

/* D1：轮播当前行高亮跟随（仅轮播时生效） */
.bar-row.is-active {
  background: #eef3ff;
  box-shadow: inset 3px 0 0 #2f6fed;
}

/* 联动聚焦：与总趋势/各版块一致的版块高亮行 */
.bar-row.is-linked {
  background: color-mix(in srgb, var(--bar-progress, #2f6fed) 14%, #fff);
  box-shadow: inset 3px 0 0 var(--bar-progress, #2f6fed);
  border: 1px solid color-mix(in srgb, var(--bar-progress, #2f6fed) 45%, transparent);
}
.bar-row.is-linked .bar-name {
  color: var(--bar-progress, #2f6fed);
  font-weight: 600;
}

/* D2：入场渐显动画（仅首屏一次性，轮播补位行复用数据不重复触发） */
.bar-list .bar-row {
  animation: barRowIn 0.4s ease both;
}
.bar-list .bar-row:nth-child(1) { animation-delay: 0.05s; }
.bar-list .bar-row:nth-child(2) { animation-delay: 0.11s; }
.bar-list .bar-row:nth-child(3) { animation-delay: 0.17s; }
.bar-list .bar-row:nth-child(4) { animation-delay: 0.23s; }
.bar-list .bar-row:nth-child(5) { animation-delay: 0.29s; }
.bar-list .bar-row:nth-child(6) { animation-delay: 0.35s; }
.bar-list .bar-row:nth-child(7) { animation-delay: 0.41s; }
.bar-list .bar-row:nth-child(8) { animation-delay: 0.47s; }
@keyframes barRowIn {
  from { opacity: 0; transform: translateX(-12px); }
  to   { opacity: 1; transform: translateX(0); }
}
@media (prefers-reduced-motion: reduce) {
  .bar-list .bar-row { animation: none !important; }
}

/* B3：底部汇总条（覆盖固定在底部） */
.bar-total-mask {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 30px;
  height: 22px;
  pointer-events: none;
  background: linear-gradient(to top, rgba(255, 255, 255, 1), rgba(255, 255, 255, 0));
  z-index: 2;
}
.bar-total-bar {
  position: absolute;
  left: 4px;
  right: 4px;
  bottom: 0;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 0 12px;
  border-radius: 6px;
  background: #f7f8fa;
  border: 1px solid #ebeef5;
  font-size: 12px;
  color: #606266;
  z-index: 3;
}
.bar-total-bar b {
  color: #2f6fed;
  font-variant-numeric: tabular-nums;
}
.bar-total-divider {
  flex: 1;
}

/* C3：行悬停自定义 Tooltip */
.bar-tip {
  position: fixed;
  z-index: 9999;
  min-width: 180px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(31, 45, 61, 0.94);
  border: 1px solid rgba(47, 111, 237, 0.3);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
  backdrop-filter: blur(4px);
  font-size: 12px;
  color: #e5e9f0;
  pointer-events: none;
}
.bar-tip-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  padding-bottom: 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}
.bar-tip-rank {
  width: 18px;
  height: 18px;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  flex-shrink: 0;
}
.bar-tip-name {
  font-weight: 600;
  color: #fff;
  font-size: 13px;
}
.bar-tip-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  line-height: 1.8;
}
.bar-tip-row > span:first-child {
  color: #8b95a7;
}
.bar-tip-strong {
  color: #a8c5ff;
  font-weight: 600;
}

@media (max-width: 1100px) {
  .bar-row {
    grid-template-columns: 28px minmax(80px, 1fr) auto;
  }
  .bar-sub {
    display: none;
  }
  /* 窄屏隐藏增量箭头，避免与总量/占比挤占空间 */
  .bar-delta {
    display: none;
  }
}
</style>
