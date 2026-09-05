<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatDuration, isAborted, type RunDetail, type RunSummary } from '../api'

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
/** 页面可见性：后台隐藏时暂停轮询，恢复可见时立即刷新并重启 */
let pageVisible = true
let polling = false

/** 静默刷新：更新列表 + 当前选中明细，不打断用户查看；防重入（上一轮未完成则跳过本轮） */
async function refreshQuiet() {
  if (polling) return
  polling = true
  try {
    const r = await api.runs()
    dates.value = r.dates
    localProxyDefault.value = r.local_proxy_default
    activePid.value = r.active_pid
    clampPage()
    if (activeDir.value) {
      const row = r.dates.find((d) => d.dir === activeDir.value)
      if (row && current.value && current.value.dir === row.dir) {
        current.value = row.id ? await api.runDetailById(row.id) : await api.runDetail(row.dir)
      }
    }
  } catch {
    /* 单次轮询失败忽略，下轮自动重试 */
  } finally {
    polling = false
  }
}

/** 启动轮询（页面可见且未启动时） */
function syncPoll() {
  if (!pageVisible || pollTimer) return
  pollTimer = setInterval(refreshQuiet, POLL_INTERVAL)
}

/** 页面隐藏暂停轮询；恢复可见立即刷新一次并重启轮询 */
function onVisibilityChange() {
  if (document.visibilityState === 'hidden') {
    pageVisible = false
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  } else if (!pageVisible) {
    pageVisible = true
    refreshQuiet()
    syncPoll()
  }
}

async function loadList() {
  loading.value = true
  try {
    const r = await api.runs()
    dates.value = r.dates
    localProxyDefault.value = r.local_proxy_default
    activePid.value = r.active_pid
    clampPage()
    if (r.dates.length && !activeDir.value) {
      await showDetail(r.dates[0])
    } else if (activeDir.value) {
      await showDetail(r.dates.find((d) => d.dir === activeDir.value) ?? r.dates[0])
    }
  } catch (e) {
    if (isAborted(e)) return
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
    if (isAborted(e)) return
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

// ---- 启动抓取（全局按钮 + 参数弹窗，参考业界 Run with parameters） ----

/** 是否有运行中的批次：全项目同时只跑一个批次，运行中时按钮切为「强制终止」 */
const hasActive = computed(() => dates.value.some((d) => d.status === 'running'))

/** Web 端启动且仍存活的抓取进程 pid；null 表示批次进程已消亡（孤儿）或非 Web 启动 */
const activePid = ref<number | null>(null)

/** 本地镜像默认开关：来自后端（txxy_env.use_local_proxy，与 run_batch 配置区同源） */
const localProxyDefault = ref(true)

const startVisible = ref(false)
const starting = ref(false)
const startForm = reactive({ use_local_proxy: true, restart: false })

/** 将要执行的命令行预览（对应 run_batch.py 入参说明） */
const startCmd = computed(
  () =>
    `python run_batch.py ${startForm.use_local_proxy ? 'true' : 'false'}` +
    (startForm.restart ? ' --restart' : ''),
)

function openStartDialog() {
  startForm.use_local_proxy = localProxyDefault.value
  startForm.restart = false
  startVisible.value = true
}

async function confirmStart() {
  starting.value = true
  try {
    await api.startRun({
      use_local_proxy: startForm.use_local_proxy,
      restart: startForm.restart,
    })
    ElMessage.success('抓取批次已启动，稍后列表会出现新的运行记录')
    startVisible.value = false
    void refreshQuiet()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`启动失败: ${(e as Error).message}`)
  } finally {
    starting.value = false
  }
}

/** 强制终止当前批次（业界 Abort）：进程消亡的孤儿同样适用（仅清理记录），
 *  终止后防重解除，可立即重新「开始抓取」 */
async function confirmStop() {
  const orphan = activePid.value == null
  try {
    await ElMessageBox.confirm(
      orphan
        ? '抓取进程已消亡（可能是启动失败），将把运行记录标记为「手动中断」以解除防重。是否继续？'
        : '将强制终止当前抓取进程（含全部版块子进程），运行记录标记为「手动中断」。是否继续？',
      '强制终止',
      { confirmButtonText: '终止', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  try {
    const r = await api.stopRun()
    ElMessage.success(
      r.killed ? '已终止抓取进程，现在可以重新发起抓取' : '批次进程已消亡，已清理运行记录',
    )
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`终止失败: ${(e as Error).message}`)
  }
  void refreshQuiet()
}

// ---- 运行日志抽屉（参考下载中心详情抽屉；运行中每 2s 轮询准实时） ----
const LOG_POLL_INTERVAL = 2000

const logVisible = ref(false)
const logLines = ref<string[]>([])
const logTruncated = ref(false)
const logSource = ref('batch')
const logSources = ref<{ label: string; value: string }[]>([])
const logRowStatus = ref('')
const logBoxRef = ref()
let logTimer: number | null = null
let logTarget: { runId?: number; date: string } | null = null

async function openLogDrawer(row: RunSummary) {
  logTarget = { runId: row.id, date: row.dir }
  logRowStatus.value = row.status
  logVisible.value = true
  logSource.value = 'batch'
  logLines.value = []
  // 日志源：批次总日志 + 各版块日志（sections 来自明细接口；日志回退记录也能取到）
  try {
    const d = row.id ? await api.runDetailById(row.id) : await api.runDetail(row.dir)
    logSources.value = [
      { label: '批次总日志', value: 'batch' },
      ...d.sections.map((s) => ({ label: `${s.fid} ${s.name}`, value: s.fid })),
    ]
  } catch {
    logSources.value = [{ label: '批次总日志', value: 'batch' }]
  }
  stopLogPoll()
  void fetchLog()
  // 仅运行中的批次轮询；已结束的日志不会再变，拉一次即可
  if (row.status === 'running') logTimer = window.setInterval(fetchLog, LOG_POLL_INTERVAL)
}

async function fetchLog() {
  if (!logTarget) return
  try {
    const r = await api.runLog({
      run_id: logTarget.runId,
      date: logTarget.runId == null ? logTarget.date : undefined,
      log: logSource.value,
    })
    logLines.value = r.lines
    logTruncated.value = r.truncated
    await nextTick()
    const box = (logBoxRef.value as { textarea?: HTMLTextAreaElement } | undefined)?.textarea
    if (box) box.scrollTop = box.scrollHeight
  } catch {
    /* 单次拉取失败忽略（如日志文件尚未生成），下轮轮询重试 */
  }
}

function stopLogPoll() {
  if (logTimer !== null) {
    window.clearInterval(logTimer)
    logTimer = null
  }
}

watch(logVisible, (v) => {
  if (!v) stopLogPoll()
})
watch(logSource, () => void fetchLog())

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
  syncPoll()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  stopLogPoll()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-card" style="margin-bottom: 16px">
      <div class="head-row">
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
        <el-tooltip
          v-if="!hasActive"
          content="启动 run_batch 全量抓取（遍历所有版块）"
          placement="top"
        >
          <el-button type="primary" class="start-btn" @click="openStartDialog">
            <el-icon style="margin-right: 4px"><VideoPlay /></el-icon>
            开始抓取
          </el-button>
        </el-tooltip>
        <el-tooltip
          v-else
          :content="activePid == null
            ? '抓取进程已消亡（可能启动失败），点击清理运行记录以解除防重'
            : '强制终止当前抓取进程（含全部子进程），之后可重新发起'"
          placement="top"
        >
          <el-button type="danger" plain class="start-btn" @click="confirmStop">
            <el-icon style="margin-right: 4px"><CircleClose /></el-icon>
            强制终止
          </el-button>
        </el-tooltip>
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
        <el-table-column label="操作" :min-width="70" fixed="right">
          <template #default="{ row }">
            <el-tooltip content="查看批次 / 版块运行日志（运行中每 2s 准实时刷新）" placement="top">
              <el-button link type="primary" @click="openLogDrawer(row)">
                <el-icon style="margin-right: 3px"><Document /></el-icon>日志
              </el-button>
            </el-tooltip>
          </template>
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

      <!-- 启动抓取：参数与 run_batch.py 命令行一一对应（业界 Run with parameters） -->
      <el-dialog v-model="startVisible" title="启动抓取批次" width="520px">
        <div class="start-cmd">{{ startCmd }}</div>
        <el-checkbox v-model="startForm.use_local_proxy">使用本地镜像代理（true）</el-checkbox>
        <div class="param-desc">
          走本机 1024 镜像访问；取消勾选则直连业务域名（对应入参 USE_LOCAL_PROXY，
          默认勾选状态来自 .env 配置）
        </div>
        <el-checkbox v-model="startForm.restart">强制重跑（--restart）</el-checkbox>
        <div class="param-desc warn">
          忽略断点进度：当天已生成的 CSV / 进度文件会被删除并重新生成，请确认后再勾选
        </div>
        <template #footer>
          <el-button @click="startVisible = false">取消</el-button>
          <el-button type="primary" :loading="starting" @click="confirmStart">启动</el-button>
        </template>
      </el-dialog>

      <!-- 运行日志抽屉：批次总日志 / 各版块日志切换，运行中每 2s 轮询准实时 -->
      <el-drawer v-model="logVisible" title="运行日志" size="62%">
        <div class="log-toolbar">
          <el-select v-model="logSource" size="small" style="width: 280px">
            <el-option v-for="s in logSources" :key="s.value" :label="s.label" :value="s.value" />
          </el-select>
          <span v-if="logRowStatus === 'running'" class="log-live">
            ● 实时更新（{{ LOG_POLL_INTERVAL / 1000 }}s）
          </span>
          <span v-if="logTruncated" class="log-truncated">日志过长，仅显示尾部 {{ logLines.length }} 行</span>
        </div>
        <el-input
          ref="logBoxRef"
          :model-value="logLines.join('\n')"
          type="textarea"
          :rows="30"
          readonly
          class="log-box"
        />
      </el-drawer>
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

/* 顶部行：说明文案 + 全局「开始抓取」按钮（业界布局：主操作放右上） */
.head-row {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}

.head-row .note {
  flex: 1;
}

.start-btn {
  flex-shrink: 0;
}

/* 启动弹窗：命令行预览 + 参数说明 */
.start-cmd {
  font-family: Consolas, monospace;
  font-size: 12px;
  background: #f2f4f8;
  padding: 8px 10px;
  border-radius: 6px;
  margin-bottom: 14px;
  color: #1f2d3d;
}

.param-desc {
  color: #909399;
  font-size: 12px;
  line-height: 1.6;
  margin: 2px 0 14px 24px;
}

.param-desc.warn {
  color: #e6a23c;
}

/* 运行日志抽屉 */
.log-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.log-live {
  color: #67c23a;
  font-size: 12px;
}

.log-truncated {
  color: #909399;
  font-size: 12px;
}

.log-box :deep(textarea) {
  font-family: Consolas, monospace;
  font-size: 12px;
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
