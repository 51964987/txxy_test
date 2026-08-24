<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { graphic, init as echartsInit, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart, EffectScatterChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkPointComponent,
  TooltipComponent,
} from 'echarts/components'
import type { ECharts } from 'echarts/core'
import { ElMessage } from 'element-plus'
import { api, type FidDistItem, type Overview, type Post, type TrendPoint } from '../api'
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
  MarkPointComponent,
])

const router = useRouter()

const store = useDashboardStore()

// ===== P0：首屏区块 =====
const overview = ref<Overview | null>(null)
const trend = ref<TrendPoint[]>([])
const fidDist = ref<FidDistItem[]>([])
const loadingP0 = ref(false)

// ===== P1：懒加载区块（热门榜）=====
const boards = ref<{ top_likes: Post[]; top_replies: Post[] } | null>(null)
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
const REFRESH_INTERVAL = 30000
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
        backgroundColor: 'rgba(31, 45, 61, 0.92)',
        borderColor: 'rgba(47, 111, 237, 0.3)',
        borderWidth: 1,
        padding: [12, 16],
        textStyle: { color: '#e5e9f0', fontSize: 13 },
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
      xAxis: { type: 'category', data: trend.value.map((t) => t.date.slice(5)), boundaryGap: false },
      yAxis: {
        type: 'value',
        minInterval: 1,
        axisLabel: { formatter: (v: number) => String(Math.round(v)) },
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
              { offset: 0, color: '#2f6fed' },
              { offset: 1, color: '#6366f1' },
            ]),
            shadowColor: 'rgba(47, 111, 237, 0.35)',
            shadowBlur: 6,
            shadowOffsetY: 2,
          },
          itemStyle: { color: '#2f6fed' },
          areaStyle: {
            color: new graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(47, 111, 237, 0.35)' },
              { offset: 0.3, color: 'rgba(47, 111, 237, 0.18)' },
              { offset: 0.65, color: 'rgba(99, 102, 241, 0.10)' },
              { offset: 1, color: 'rgba(99, 102, 241, 0.01)' },
            ]),
          },
          markArea: {
            silent: true,
            itemStyle: { color: 'rgba(47, 111, 237, 0.06)' },
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
        router.push({ path: '/posts', query: { date_from: point.date, date_to: point.date } })
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

// 名次徽章按真实排名计算；末尾补位 (N-1) 行复用前几行的名次（1~N-1），实现无缝循环
const barLoopItems = computed(() => {
  const items = fidDist.value.map((item, i) => ({ item, rank: i + 1 }))
  const padCount = Math.max(0, BAR_VISIBLE_ROWS - 1)
  const pad = fidDist.value
    .slice(0, Math.min(padCount, fidDist.value.length))
    .map((item, i) => ({ item, rank: i + 1 }))
  return [...items, ...pad]
})

// 固定 8 行可视区高度，与 CSS 中 .bar-list 保持一致
const barListStyle = computed(() => ({ height: `${BAR_VISIBLE_ROWS * BAR_ROW_HEIGHT}px` }))

function startBarScroll() {
  stopBarScroll()
  const el = barListRef.value
  if (!el || fidDist.value.length <= BAR_VISIBLE_ROWS) return
  barIndex = 0
  el.scrollTop = 0
  barTimer = setInterval(() => {
    const maxIndex = Math.max(0, barLoopItems.value.length - BAR_VISIBLE_ROWS)
    barIndex = barIndex >= maxIndex ? 0 : barIndex + 1
    el.scrollTop = barIndex * BAR_ROW_HEIGHT
  }, BAR_WAIT_TIME)
}

function stopBarScroll() {
  if (barTimer) {
    clearInterval(barTimer)
    barTimer = null
  }
  barIndex = 0
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
const TREND_DAYS_SEQ = [7, 30, 60, 90]
const TREND_TIP_INTERVAL = 900 // ms，单点停留时长
const TREND_STAGE_GAP = 1500 // ms，阶段切换间隔
const trendSwitching = ref(false) // 数据切换中（轻量 loading 指示，不透明度过渡）
let trendTipTimer: ReturnType<typeof setInterval> | null = null
let trendStageTimer: ReturnType<typeof setTimeout> | null = null
let trendStartTimer: ReturnType<typeof setTimeout> | null = null
let trendTipIndex = 0
let trendTipPaused = false
let trendLoading = false
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
    if (trendTipPaused) return
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
    const prevLen = trend.value.length
    const i = TREND_DAYS_SEQ.indexOf(trendDays.value)
    const nextIdx = (i + 1) % TREND_DAYS_SEQ.length
    trendDays.value = TREND_DAYS_SEQ[nextIdx]
    loadTrendOnly(nextIdx === 0 ? undefined : prevLen)
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
  loadTrendOnly()
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
            <div class="stat-value">{{ totalText }}</div>
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

    <!-- 每日新增趋势（整行全宽，位于 6 个指标卡下方） -->
    <div class="page-card chart-card" style="margin-bottom: 16px">
      <div class="chart-head">
        <span class="chart-title">每日新增趋势（近 {{ trendDays }} 天）</span>
        <div v-if="trendStats" class="trend-stats">
          <div class="ts-card ts-peak">
            <span class="ts-label">峰值</span>
            <span class="ts-value">{{ trendStats.max.toLocaleString() }}</span>
            <span class="ts-sub">{{ trendStats.maxDate.slice(5) }}</span>
          </div>
          <div class="ts-card ts-valley">
            <span class="ts-label">谷值</span>
            <span class="ts-value">{{ trendStats.min.toLocaleString() }}</span>
            <span class="ts-sub">{{ trendStats.minDate.slice(5) }}</span>
          </div>
          <div class="ts-card ts-avg">
            <span class="ts-label">日均</span>
            <span class="ts-value">{{ trendStats.avg.toLocaleString() }}</span>
          </div>
          <div class="ts-card ts-total">
            <span class="ts-label">总计</span>
            <span class="ts-value">{{ trendStats.total.toLocaleString() }}</span>
          </div>
        </div>
        <el-radio-group v-model="trendDays" size="small" @change="onTrendDaysChange">
          <el-radio-button :value="7">7天</el-radio-button>
          <el-radio-button :value="30">30天</el-radio-button>
          <el-radio-button :value="60">60天</el-radio-button>
          <el-radio-button :value="90">90天</el-radio-button>
        </el-radio-group>
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

    <!-- 图表（P0）：左排行榜 + 右环形图，同时展示 -->
    <div class="chart-row">
      <div class="page-card chart-card">
        <div class="chart-head">
          <span class="chart-title">版块分布 · 排行榜</span>
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
              :title="`最近抓取：${row.item.latest_date ?? '-'}，今日新增：${row.item.today_count ?? 0}，昨日新增：${row.item.yesterday_count ?? 0}`"
              @click="goDist(row.item.fid)"
            >
              <span :class="rankClass(row.rank - 1)" class="bar-rank">{{ row.rank }}</span>
              <span class="bar-name">{{ row.item.name }}({{ row.item.fid }})</span>
              <span class="bar-sub">
                <template v-if="row.item.today_count != null">今日新增 {{ row.item.today_count.toLocaleString() }}</template>
                <template v-else>最新 {{ row.item.latest_date ?? '-' }}</template>
              </span>
              <div class="bar-metric">
                <span class="bar-value">{{ row.item.count.toLocaleString() }} 条</span>
                <span class="bar-pct">({{ fidDistTotal ? ((row.item.count / fidDistTotal) * 100).toFixed(1) : 0 }}%)</span>
              </div>
            </div>
          </div>
        </div>
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
}

.chart-title {
  font-weight: 600;
  color: #1f2d3d;
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

@media (max-width: 1100px) {
  .bar-row {
    grid-template-columns: 28px minmax(80px, 1fr) auto;
  }
  .bar-sub {
    display: none;
  }
}
</style>
