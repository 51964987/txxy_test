<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  ElMessage,
  ElMessageBox,
  ElNotification,
} from 'element-plus'
import {
  api,
  isAborted,
  type DownloadTask,
} from '../api'

const REFRESH_INTERVAL = 3000 // 任务页固定 3s 轮询（页面可见时）

const tasks = ref<DownloadTask[]>([])
const loading = ref(false)
const error = ref('') // 轮询失败提示信息

let timer: number | null = null

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
const filterStatus = ref<'all' | 'active' | 'done' | 'failed' | 'cancelled'>('all')
const activeCount = computed(
  () => tasks.value.filter((t) => t.status === 'running' || t.status === 'pending').length,
)
const doneCount = computed(() => tasks.value.filter((t) => t.status === 'done').length)
const failedCount = computed(() => tasks.value.filter((t) => t.status === 'failed').length)
const cancelledCount = computed(
  () => tasks.value.filter((t) => t.status === 'cancelled').length,
)
const filterOptions = computed(() => [
  { label: `全部（${tasks.value.length}）`, value: 'all' },
  { label: `进行中（${activeCount.value}）`, value: 'active' },
  { label: `已完成（${doneCount.value}）`, value: 'done' },
  { label: `失败（${failedCount.value}）`, value: 'failed' },
  { label: `已取消（${cancelledCount.value}）`, value: 'cancelled' },
])
const filteredTasks = computed<DownloadTask[]>(() => {
  if (filterStatus.value === 'all') return tasks.value
  if (filterStatus.value === 'active')
    return tasks.value.filter((t) => t.status === 'running' || t.status === 'pending')
  return tasks.value.filter((t) => t.status === filterStatus.value)
})

// ---- D2 重复提交提醒：与历史已成功/跳过的 URL 比对 ----
async function submitUrls() {
  const urls = parsedUrls.value
  if (!urls.length) {
    ElMessage.warning('请输入至少一个有效 URL（http/https 开头）')
    return
  }
  const doneSet = new Set<string>()
  for (const t of tasks.value) {
    for (const it of t.items) {
      if (it.status === 'ok' || it.status === 'skip') doneSet.add(it.url)
    }
  }
  const dup = urls.filter((u) => doneSet.has(u))
  if (dup.length > 0) {
    const go = await ElMessageBox.confirm(
      `有 ${dup.length} 个链接此前已成功下载过，重复提交会自动跳过已存在文件。仍要提交吗？`,
      '重复提交提醒',
      { type: 'warning', confirmButtonText: '仍要提交', cancelButtonText: '取消' },
    )
      .then(() => true)
      .catch(() => false)
    if (!go) return
  }
  try {
    const r = await api.submitDownload(urls)
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
function diffAndNotify(list: DownloadTask[]) {
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
          message: '任务执行中断，可在列表中点击「重试」重跑未成功链接',
        })
      } else if (t.status === 'cancelled') {
        ElNotification.info({ title: '下载任务已取消', message: `已完成 ${t.done}/${t.total}` })
      }
    }
    knownStatus.set(t.id, t.status)
  }
}

// ---- D1/D5/D9 任务操作 ----
async function retryTask(row: DownloadTask) {
  try {
    const r = await api.retryDownload(row.id)
    ElMessage.success(`已创建重试任务（${r.retried} 个链接）`)
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`重试失败: ${(e as Error).message}`)
  }
}

async function prioritizeTask(row: DownloadTask) {
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
  if (!tasks.value.some((t) => TERMINAL.includes(t.status))) {
    ElMessage.info('当前没有已结束的任务')
    return
  }
  const go = await ElMessageBox.confirm(
    '将删除全部已完成/失败/已取消的任务记录（不影响已下载文件），确定吗？',
    '清空历史任务',
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

async function cancelTask(row: DownloadTask) {
  try {
    await api.cancelDownload(row.id)
    ElMessage.success('已请求取消')
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`取消失败: ${(e as Error).message}`)
  }
}

async function deleteTask(row: DownloadTask) {
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

// ---- 详情抽屉 ----
const detailVisible = ref(false)
const detailTask = ref<DownloadTask | null>(null)

function showDetail(row: DownloadTask) {
  detailTask.value = row
  detailVisible.value = true
}

function statusTagType(status: string): string {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'running') return 'primary'
  if (status === 'pending') return 'info'
  return 'warning' // cancelled
}

function statusText(status: string): string {
  const map: Record<string, string> = {
    pending: '排队中',
    running: '下载中',
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

// ---- 轮询 ----
async function loadTasks() {
  try {
    const r = await api.downloadTasks()
    error.value = ''
    tasks.value = r.tasks
    diffAndNotify(r.tasks)
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

onMounted(() => {
  void loadTasks()
  timer = window.setInterval(tick, REFRESH_INTERVAL)
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <div v-loading="loading">
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
          <div class="stat-label">失败</div>
          <div class="stat-value">{{ failedCount }}</div>
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

      <!-- D3 状态筛选 + D9 清空历史 -->
      <div class="toolbar">
        <el-segmented v-model="filterStatus" :options="filterOptions" />
        <el-button class="clear-btn" @click="clearFinished">清空已完成</el-button>
      </div>

      <div v-if="error" class="poll-error">
        轮询失败：{{ error }}（每 {{ REFRESH_INTERVAL / 1000 }} 秒自动重试）
      </div>

      <el-table :data="filteredTasks" size="default" style="width: 100%">
        <el-table-column label="任务 ID" width="130">
          <template #default="{ row }">
            <span class="task-id" :title="row.id">{{ row.id.slice(0, 10) }}</span>
            <el-tag v-if="row.priority" size="small" type="success" class="prio-tag">置顶</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="200">
          <template #default="{ row }">
            <el-progress
              :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
              :stroke-width="8"
            >
              <span class="progress-text">{{ row.done }}/{{ row.total }}</span>
            </el-progress>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="160" />
        <el-table-column label="操作" width="270" fixed="right">
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
            <el-button v-if="row.status === 'failed'" link type="warning" @click="retryTask(row)">
              重试
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

      <el-empty v-if="tasks.length === 0 && !error" description="暂无下载任务，可在上方粘贴链接提交" />
    </div>

    <!-- 任务详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="`任务详情 ${detailTask?.id ?? ''}`" size="55%">
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
          :model-value="(detailTask.logs || []).join('\n')"
          type="textarea"
          :rows="8"
          readonly
          class="logs-box"
        />

        <div class="detail-title">链接明细（D7 含耗时）</div>
        <el-table :data="detailTask.items" size="small" border>
          <el-table-column type="index" label="#" width="46" />
          <el-table-column label="URL" min-width="260" show-overflow-tooltip>
            <template #default="{ row }">{{ row.url }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="itemTagType(row.status)">{{ itemText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else-if="Object.keys(row.stats || {}).length">
                {{ Object.entries(row.stats).map(([k, v]) => `${k} ${v}`).join(', ') }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              <span>{{ row.elapsed != null ? `${row.elapsed}s` : '-' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
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

.clear-btn {
  margin-left: auto;
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

.error-text {
  color: #ef4444;
}
</style>
