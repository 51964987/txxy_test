<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatDuration, type RunDetail, type RunSummary } from '../api'

const dates = ref<RunSummary[]>([])
const loading = ref(false)
const detailLoading = ref(false)
const current = ref<RunDetail | null>(null)
const activeDir = ref('')

/** 分页：每页 5 条 */
const PAGE_SIZE = 5
const currentPage = ref(1)

/** 当前页展示的运行记录（按日期倒序全量切片） */
const pagedDates = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return dates.value.slice(start, start + PAGE_SIZE)
})

/** 数据更新后校正页码，避免当前页超出总页数 */
function clampPage() {
  const maxPage = Math.max(1, Math.ceil(dates.value.length / PAGE_SIZE))
  if (currentPage.value > maxPage) currentPage.value = maxPage
}

function onPageChange(page: number) {
  currentPage.value = page
}

/** 轮询句柄：页面存活期间始终每 4 秒静默刷新，确保新启动的运行记录（running）能实时出现并更新进度 */
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL = 4000

/** 静默刷新：更新列表 + 当前选中明细，不打断用户查看 */
async function refreshQuiet() {
  try {
    const r = await api.runs()
    dates.value = r.dates
    clampPage()
    if (activeDir.value) {
      const row = r.dates.find((d) => d.dir === activeDir.value)
      if (row && current.value && current.value.dir === row.dir) {
        current.value = row.id ? await api.runDetailById(row.id) : await api.runDetail(row.dir)
      }
    }
  } catch {
    /* 单次轮询失败忽略，下轮自动重试 */
  }
}

async function loadList() {
  loading.value = true
  try {
    const r = await api.runs()
    dates.value = r.dates
    clampPage()
    if (r.dates.length && !activeDir.value) {
      await showDetail(r.dates[0])
    } else if (activeDir.value) {
      await showDetail(r.dates.find((d) => d.dir === activeDir.value) ?? r.dates[0])
    }
  } catch (e) {
    ElMessage.error(`加载运行记录失败: ${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

async function showDetail(row: RunSummary) {
  activeDir.value = row.dir
  detailLoading.value = true
  try {
    // 数据库记录按运行 ID 查明细（每次运行一条）；日志回退项按日期查
    current.value = row.id ? await api.runDetailById(row.id) : await api.runDetail(row.dir)
  } catch (e) {
    ElMessage.error(`加载明细失败: ${(e as Error).message}`)
  } finally {
    detailLoading.value = false
  }
}

function onCurrentChange(row: RunSummary | null) {
  if (row) showDetail(row)
}

function statusType(s: string): 'success' | 'danger' | 'warning' | 'primary' | 'info' {
  if (s === 'ok') return 'success'
  if (s === 'error') return 'danger'
  if (s === 'cancelled') return 'warning'
  if (s === 'running') return 'primary'
  return 'info'
}

function statusText(s: string): string {
  if (s === 'ok') return '正常完成'
  if (s === 'error') return '异常中断'
  if (s === 'cancelled') return '手动中断'
  if (s === 'running') return '进行中'
  return s
}

function sectionType(s: string): 'success' | 'danger' | 'primary' | 'info' {
  if (s === 'ok') return 'success'
  if (s === 'fail') return 'danger'
  if (s === 'running') return 'primary'
  return 'info'
}

function sectionText(s: string): string {
  if (s === 'ok') return '成功'
  if (s === 'fail') return '失败'
  if (s === 'running') return '进行中'
  return '未执行'
}

onMounted(() => {
  loadList()
  // 页面存活期间始终轮询，新启动的运行记录（running）能实时出现并更新状态/进度
  pollTimer = setInterval(refreshQuiet, POLL_INTERVAL)
})
onBeforeUnmount(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-card" style="margin-bottom: 16px">
      <div class="note">
        <el-icon style="margin-right: 6px"><InfoFilled /></el-icon>
        <span>
          运行记录由 <code>run_batch</code> / <code>scraper</code> 运行开始时创建、逐页上报进度并实时落库
          （<code>db/posts.db</code> 的 <code>run_days</code> / <code>run_sections</code> 表）。
          本页每 4 秒自动刷新，运行中记录实时更新状态与进度；
          每次运行一条、历史保留，
          同一天多次运行会分别展示，不受日志清理策略影响；
          改动前仅留日志的历史记录会回退解析 <code>outputs/&lt;日期&gt;/</code> 的日志展示。
          记录超过 5 条时自动分页，每页 5 条。
        </span>
      </div>

      <el-table :data="pagedDates" highlight-current-row style="width: 100%" @current-change="onCurrentChange">
        <el-table-column label="日期时间" :min-width="130">
          <template #default="{ row }">
            <span class="date-cell">{{ row.date }}</span>
            <span class="time-cell">{{ row.time || (row.id ? '—' : '日志') }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="source" label="来源" :min-width="65">
          <template #default="{ row }">
            <el-tag size="small" :type="row.source === 'run_batch' ? 'primary' : 'warning'">
              {{ row.source === 'run_batch' ? 'run_batch' : 'scraper 单跑' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" :min-width="65">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" :min-width="75">
          <template #default="{ row }">
            <el-progress
              v-if="row.status === 'running' && row.progress != null"
              :percentage="row.progress"
              :stroke-width="8"
              :status="row.progress >= 100 ? 'success' : undefined"
            />
            <span v-else-if="row.status === 'running'" class="text-muted">准备中</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="ok" label="成功" :min-width="45" />
        <el-table-column prop="fail" label="失败" :min-width="45" />
        <el-table-column prop="skip" label="未执行" :min-width="50" show-overflow-tooltip />
        <el-table-column prop="csv" label="CSV 条数" :min-width="75" show-overflow-tooltip />
        <el-table-column prop="sqlite" label="SQLite 条数" :min-width="80" show-overflow-tooltip />
        <el-table-column label="耗时" :min-width="65">
          <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
        </el-table-column>
      </el-table>

      <div class="runs-pager" v-if="dates.length > PAGE_SIZE">
        <el-pagination
          background
          layout="total, prev, pager, next, jumper"
          :current-page="currentPage"
          :page-size="PAGE_SIZE"
          :total="dates.length"
          @current-change="onPageChange"
        />
      </div>
    </div>

    <div class="page-card" v-loading="detailLoading">
      <template v-if="current">
        <div class="detail-head">
          <span class="chart-title">
            {{ current.date }}<template v-if="current.time">&nbsp;{{ current.time }}</template>
            运行明细（{{ current.source === 'run_batch' ? 'run_batch' : 'scraper 单跑' }}）
          </span>
          <span class="text-muted">
            状态：<el-tag size="small" :type="statusType(current.status)">{{ statusText(current.status) }}</el-tag>
            <template v-if="current.status === 'running'">
              <template v-if="current.progress != null">
                &nbsp;整体进度
                <el-progress
                  :percentage="current.progress"
                  :stroke-width="8"
                  style="display: inline-flex; width: 160px; margin-left: 4px; vertical-align: middle"
                />
              </template>
              <span v-else>&nbsp;整体进度：准备中</span>
            </template>
            <template v-if="current.source === 'run_batch' && current.overall">
              &nbsp;成功 {{ current.overall.ok }} · 失败 {{ current.overall.fail }} · 未执行
              {{ current.overall.skip }}
            </template>
            &nbsp;| CSV {{ current.total.csv }} 条 / SQLite 入库 {{ current.total.sqlite }} 条
          </span>
        </div>

        <el-table :data="current.sections" size="small" style="width: 100%; margin-top: 12px" empty-text="无版块明细">
          <el-table-column prop="fid" label="版块" :min-width="65">
            <template #default="{ row }">
              <el-tag size="small" type="info">{{ row.fid }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" :min-width="120" show-overflow-tooltip />
          <el-table-column label="状态" :min-width="70">
            <template #default="{ row }">
              <el-tag size="small" :type="sectionType(row.status)">{{ sectionText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="进度" :min-width="96">
            <template #default="{ row }">
              <el-progress
                v-if="row.status === 'running' && row.progress != null && row.total_pages"
                :percentage="row.progress"
                :stroke-width="8"
              />
              <span v-else-if="row.status === 'running'" class="text-muted">准备中</span>
              <span v-else-if="row.status === 'skip'" class="text-muted">—</span>
              <span v-else-if="row.progress != null" class="text-muted">{{ row.progress }}%</span>
              <span v-else class="text-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="csv" label="CSV 条数" :min-width="75" show-overflow-tooltip />
          <el-table-column prop="sqlite" label="SQLite 条数" :min-width="85" show-overflow-tooltip />
          <el-table-column label="耗时" :min-width="80">
            <template #default="{ row }">{{ formatDuration(row.duration) }}</template>
          </el-table-column>
        </el-table>
      </template>
      <el-empty v-else description="暂无运行记录（outputs/ 下没有日志）" />
    </div>
  </div>
</template>

<style scoped>
.note {
  display: flex;
  align-items: flex-start;
  color: #909399;
  font-size: 13px;
  margin-bottom: 14px;
  line-height: 1.6;
}

.note code {
  background: #f2f4f8;
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 12px;
}

.date-cell {
  font-weight: 600;
}

.time-cell {
  margin-left: 6px;
  color: #909399;
}

.detail-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}

.chart-title {
  font-weight: 600;
  color: #1f2d3d;
}

.runs-pager {
  display: flex;
  justify-content: center;
  margin-top: 14px;
}

/* 窄屏下减小单元格内边距，给数据留出更多空间 */
@media (max-width: 1200px) {
  :deep(.el-table .cell) {
    padding-left: 6px;
    padding-right: 6px;
  }
}
</style>
