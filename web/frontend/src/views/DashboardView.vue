<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRouter } from 'vue-router'
import { graphic, init as echartsInit, use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, PieChart } from 'echarts/charts'
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

use([
  CanvasRenderer,
  LineChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
])

const router = useRouter()

const store = useDashboardStore()

const overview = ref<Overview | null>(null)
const trend = ref<TrendPoint[]>([])
const fidDist = ref<FidDistItem[]>([])
const recent = ref<Post[]>([])
const loading = ref(false)

const trendRef = shallowRef<HTMLDivElement | null>(null)
const pieRef = shallowRef<HTMLDivElement | null>(null)
const trendChart = shallowRef<ECharts | null>(null)
const pieChart = shallowRef<ECharts | null>(null)

const trendDays = ref(30)

// 版块分布：环形图 / 排行榜 双视图
const distView = ref<'pie' | 'bar'>('pie')
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
const REFRESH_INTERVAL = 30000
let refreshTimer: ReturnType<typeof setInterval> | null = null

const fidDistTotal = computed(() => fidDist.value.reduce((s, f) => s + f.count, 0))

const totalText = computed(() => (overview.value?.total ?? 0).toLocaleString())
const todayText = computed(() => (overview.value?.today ?? 0).toLocaleString())
const yesterdayText = computed(() => (overview.value?.yesterday ?? 0).toLocaleString())
const weekText = computed(() => (overview.value?.week_new ?? 0).toLocaleString())
const totalUsersText = computed(() => (overview.value?.total_users ?? 0).toLocaleString())
const activeUsersText = computed(() => (overview.value?.active_users ?? 0).toLocaleString())

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

async function load(initial = false) {
  if (initial) loading.value = true
  store.refreshing = true
  try {
    const [o, t, f, r] = await Promise.all([
      api.overview(),
      api.trend(trendDays.value),
      api.fidDist(),
      api.recent(10),
    ])
    overview.value = o
    trend.value = t
    fidDist.value = f
    recent.value = r
    store.setUpdatedAt(o.latest_created_at ?? null)
    renderTrendChart()
    renderDistChart()
  } catch (e) {
    ElMessage.error(`加载总览数据失败: ${(e as Error).message}`)
  } finally {
    loading.value = false
    store.refreshing = false
  }
}

function renderTrendChart() {
  if (trendRef.value) {
    trendChart.value ??= initChart(trendRef.value)
    const data = trend.value.map((t) => t.count)
    const needZoom = trend.value.length > 31
    trendChart.value.setOption({
      tooltip: {
        trigger: 'axis',
        formatter: (params: any[]) => {
          const p = params[0]
          if (!p) return ''
          const idx = p.dataIndex
          const cur = data[idx]
          if (idx === 0) return `${p.axisValue}<br/>新增帖子：<b>${cur}</b> 条`
          const prev = data[idx - 1]
          const diff = cur - prev
          return `${p.axisValue}<br/>新增帖子：<b>${cur}</b> 条（较上日 ${diff >= 0 ? '+' : ''}${diff}）`
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
          data,
          lineStyle: { width: 3, color: '#2f6fed' },
          itemStyle: { color: '#2f6fed' },
          areaStyle: {
            color: new graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(47, 111, 237, 0.22)' },
              { offset: 1, color: 'rgba(47, 111, 237, 0.02)' },
            ]),
          },
          markPoint: {
            symbolSize: 46,
            label: { fontSize: 10 },
            data: [
              { type: 'max', name: '最高' },
              { type: 'min', name: '最低' },
            ],
          },
        },
      ],
    })
  }
}

/** 版块分布渲染：环形图，支持点击跳转与悬停联动 */
function renderDistChart() {
  if (!pieRef.value || distView.value !== 'pie') return
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
    if (p.componentType === 'series' && p.data) {
      const f = p.data
      const pct = total ? ((f.value / total) * 100).toFixed(1) : '0'
      centerInfo.value = {
        name: f.labelText ?? f.name,
        count: Number(f.value).toLocaleString(),
        pct,
        color: colorForFid(String(f.fid ?? '')),
      }
    }
  })
  chart.on('mouseout', () => {
    centerInfo.value = null
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
    if (!refreshTimer) refreshTimer = setInterval(() => load(false), REFRESH_INTERVAL)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

// Header 刷新按钮 / 开关通过 store 触发，保持原有逻辑
function onRefresh() {
  load(false)
}

onMounted(() => {
  load(true)
  syncAutoRefresh()
  store.registerRefresh(onRefresh)
  store.registerAutoChange(syncAutoRefresh)
  window.addEventListener('resize', onResize)
})

onBeforeUnmount(() => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
  store.registerRefresh(null)
  store.registerAutoChange(null)
  window.removeEventListener('resize', onResize)
  trendChart.value?.dispose()
  pieChart.value?.dispose()
})

function openPost(p: Post) {
  window.open(p.url, '_blank', 'noopener')
}

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

// 相对时间：x 分钟前 / x 小时前 / x 天前
function formatRelativeTime(raw?: string): string {
  if (!raw) return '-'
  const s = String(raw).replace('T', ' ')
  const [datePart = '', timePart = '00:00:00'] = s.split(' ')
  const t = new Date(`${datePart}T${timePart}`)
  if (Number.isNaN(t.getTime())) return s.slice(0, 19)
  const diffMs = Date.now() - t.getTime()
  const min = Math.floor(diffMs / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d} 天前`
  return s.slice(0, 10)
}
</script>

<template>
  <div v-loading="loading">
    <!-- 统计卡片 -->

    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background: linear-gradient(135deg, #4f83f1, #2f6fed)">
          <el-icon><Collection /></el-icon>
        </div>
        <div class="stat-body">
          <div class="stat-label">累计帖子</div>
          <div class="stat-value">{{ totalText }}</div>
          <div v-if="overview" class="stat-sub">
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
          <div class="stat-value">{{ todayText }}</div>
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
          <div class="stat-value">{{ yesterdayText }}</div>
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
          <div class="stat-value">{{ weekText }}</div>
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
          <div class="stat-value">{{ totalUsersText }}</div>
          <div v-if="overview" class="stat-sub">
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
          <div class="stat-value">{{ activeUsersText }}</div>
          <div v-if="kpiSub" class="stat-sub">
            <span class="sub-neutral">占累计 {{ kpiSub.activeShare ?? 0 }}%</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 图表 -->
    <div class="chart-row">
      <div class="page-card chart-card">
        <div class="chart-head">
          <span class="chart-title">每日新增趋势（近 {{ trendDays }} 天）</span>
          <el-radio-group v-model="trendDays" size="small" @change="() => load(false)">
            <el-radio-button :value="7">7天</el-radio-button>
            <el-radio-button :value="30">30天</el-radio-button>
            <el-radio-button :value="90">90天</el-radio-button>
          </el-radio-group>
        </div>
        <div ref="trendRef" class="chart"></div>
      </div>
      <div class="page-card chart-card">
        <div class="chart-head">
          <span class="chart-title">版块分布</span>
          <el-radio-group v-model="distView" size="small" @change="renderDistChart">
            <el-radio-button value="pie">环形图</el-radio-button>
            <el-radio-button value="bar">排行榜</el-radio-button>
          </el-radio-group>
        </div>
        <div class="chart-wrap">
          <div v-show="distView === 'pie'" ref="pieRef" class="chart"></div>
          <div v-if="overview && distView === 'pie'" class="pie-center" :style="pieCenterStyle">
            <template v-if="centerInfo">
              <div class="pie-center-value" :style="{ color: centerInfo.color }">{{ centerInfo.count }}</div>
              <div class="pie-center-label">{{ centerInfo.name }} · {{ centerInfo.pct }}%</div>
            </template>
            <template v-else>
              <div class="pie-center-value">{{ totalText }}</div>
              <div class="pie-center-label">累计帖子</div>
            </template>
          </div>
          <!-- 排行榜：纯 HTML 列表，避免窄卡片 ECharts bar 渲染问题，并支持展示更多字段 -->
          <div v-if="distView === 'bar'" class="bar-list">
            <div
              v-for="f in fidDist"
              :key="f.fid"
              class="bar-row"
              :title="`最近抓取：${f.latest_date ?? '-'}，今日新增：${f.today_count ?? 0}，昨日新增：${f.yesterday_count ?? 0}`"
              @click="goDist(f.fid)"
            >
              <span class="bar-name">{{ f.name }}({{ f.fid }})</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: `${fidDistTotal ? (f.count / fidDistTotal) * 100 : 0}%`, backgroundColor: colorForFid(f.fid) }"
                ></div>
              </div>
              <span class="bar-value">{{ f.count.toLocaleString() }} 条</span>
              <span class="bar-pct">({{ fidDistTotal ? ((f.count / fidDistTotal) * 100).toFixed(1) : 0 }}%)</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 热门榜 -->
    <div class="board-row">
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 8px">
          <span class="chart-title">点赞最高帖（各版块）</span>
          <el-link type="primary" :underline="false" class="more-link" @click="goPosts">查看更多</el-link>
        </div>
        <div class="board-list">
          <div v-for="(item, i) in overview?.top_likes ?? []" :key="item.fid" class="board-card" @click="openUrl(item.url)">
            <span :class="rankClass(i)">{{ i + 1 }}</span>
            <el-tag size="small" type="danger" class="board-tag">{{ item.name }}</el-tag>
            <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
              {{ item.title }}
            </a>
            <span class="board-metric">
              <el-icon><Star /></el-icon>{{ metricText(item.value) }}
            </span>
          </div>
          <div v-if="!(overview?.top_likes?.length)" class="text-muted">暂无数据</div>
        </div>
      </div>
      <div class="page-card chart-card">
        <div class="chart-head" style="margin-bottom: 8px">
          <span class="chart-title">回复最高帖（各版块）</span>
          <el-link type="primary" :underline="false" class="more-link" @click="goPosts">查看更多</el-link>
        </div>
        <div class="board-list">
          <div v-for="(item, i) in overview?.top_replies ?? []" :key="item.fid" class="board-card" @click="openUrl(item.url)">
            <span :class="rankClass(i)">{{ i + 1 }}</span>
            <el-tag size="small" type="warning" class="board-tag">{{ item.name }}</el-tag>
            <a class="title-link board-title" :title="item.title" @click.prevent="openUrl(item.url)">
              {{ item.title }}
            </a>
            <span class="board-metric">
              <el-icon><ChatDotRound /></el-icon>{{ metricText(item.value) }}
            </span>
          </div>
          <div v-if="!(overview?.top_replies?.length)" class="text-muted">暂无数据</div>
        </div>
      </div>
    </div>

    <!-- 最近抓取 -->
    <div class="page-card">
      <div class="chart-head" style="margin-bottom: 12px">
        <span class="chart-title">最近抓取 Top 10</span>
      </div>
      <el-table :data="recent" size="small" empty-text="暂无数据" style="width: 100%">
        <el-table-column type="index" label="#" width="50" align="center" />
        <el-table-column prop="title" label="标题" min-width="380" show-overflow-tooltip>
          <template #default="{ row }">
            <a class="title-link" @click.prevent="openPost(row)">{{ row.title }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="fid" label="版块" width="90">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ row.fid }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="likes" label="点赞" width="80" align="center">
          <template #default="{ row }">{{ row.likes || '-' }}</template>
        </el-table-column>
        <el-table-column prop="author" label="作者" width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.author || '-' }}</template>
        </el-table-column>
        <el-table-column prop="replies" label="回复" width="80" align="center">
          <template #default="{ row }">{{ row.replies || '-' }}</template>
        </el-table-column>
        <el-table-column label="抓取时间" width="130">
          <template #default="{ row }">
            <el-tooltip :content="row.created_at || '-'" placement="top">
              <span class="text-muted">{{ formatRelativeTime(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
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

.more-link {
  font-size: 12px;
}

.chart {
  height: 320px;
  width: 100%;
}

.chart-wrap {
  position: relative;
  height: 320px;
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

/* 版块分布 - 排行榜模式 */
.bar-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 4px;
  height: 320px;
  overflow-y: auto;
}

.bar-row {
  display: grid;
  grid-template-columns: minmax(80px, 1fr) 120px auto auto;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease;
  font-size: 13px;
}

.bar-row:hover {
  background: #f0f4ff;
}

.bar-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  color: #1f2d3d;
  font-weight: 500;
}

.bar-track {
  position: relative;
  height: 10px;
  background: #ebeef5;
  border-radius: 5px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.4s ease;
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
    grid-template-columns: minmax(80px, 1fr) 1fr auto auto;
  }
}
</style>
