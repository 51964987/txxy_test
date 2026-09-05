<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  ElMessage,
  ElMessageBox,
  ElNotification,
} from 'element-plus'
import {
  api,
  isAborted,
  sseUrl,
  type DownloadTaskDetail,
  type DownloadTaskSummary,
} from '../api'

const REFRESH_INTERVAL = 3000 // 轮询降级通道间隔（SSE 正常时不轮询）

const tasks = ref<DownloadTaskSummary[]>([])
const loading = ref(false)
const error = ref('') // 轮询失败提示信息

let pollTimer: number | null = null

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
const filteredTasks = computed<DownloadTaskSummary[]>(() => {
  if (filterStatus.value === 'all') return tasks.value
  if (filterStatus.value === 'active')
    return tasks.value.filter((t) => t.status === 'running' || t.status === 'pending')
  return tasks.value.filter((t) => t.status === filterStatus.value)
})

// ---- D2 重复提交提醒：区分「文件仍在 / 已不在 / 正在下载」三类 ----

/** HTML 转义：URL 来自用户输入，拼进 MessageBox 的 HTML 内容前必须转义 */
function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => {
    const map: Record<string, string> = {
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }
    return map[c] as string
  })
}

/** 弹窗内只列出前 n 个链接，长列表截断以免撑爆弹窗 */
function briefList(urls: string[], n = 3) {
  const head = urls.slice(0, n).map(escapeHtml)
  const rest = urls.length - head.length
  return rest > 0 ? `${head.join('<br>')}<br>…等共 ${urls.length} 个` : head.join('<br>')
}

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
async function retryTask(row: DownloadTaskSummary) {
  try {
    const r = await api.retryDownload(row.id)
    ElMessage.success(`已创建重试任务（${r.retried} 个链接）`)
    await loadTasks()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`重试失败: ${(e as Error).message}`)
  }
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

/** 解析任务日志：非缩进行为任务事件 / 逐 URL 结果摘要；
 *  缩进的 [i/N] 行（下载过程明细）归入对应链接组，供折叠展示 */
const parsedLogs = computed(() => {
  const events: string[] = []
  const groups: { seq: string; title: string; lines: string[] }[] = []
  let cur: { seq: string; title: string; lines: string[] } | null = null
  for (const raw of detailTask.value?.logs || []) {
    // 明细行：前导空白 + [i/N]（由后端缩进标注归属）
    const detail = raw.match(/^\s+\[(\d+\/\d+)\]\s?(.*)$/)
    if (detail) {
      if (!cur || cur.seq !== detail[1]) {
        cur = { seq: detail[1], title: '', lines: [] }
        groups.push(cur)
      }
      cur.lines.push(detail[2])
      continue
    }
    // 结果摘要行：[i/N] 开头（无缩进）
    const summary = raw.match(/^\[(\d+\/\d+)\]\s?(.*)$/)
    if (summary) {
      cur = { seq: summary[1], title: summary[2], lines: [] }
      groups.push(cur)
      events.push(raw)
      continue
    }
    events.push(raw)
    cur = null
  }
  return { events, groups }
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
    const box = (logsBoxRef.value as { textarea?: HTMLTextAreaElement } | undefined)
      ?.textarea
    if (box) box.scrollTop = box.scrollHeight
  },
)

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

// ---- 任务更新通道：R2 SSE 实时推送为主，3s 轮询作为断线降级 ----
async function loadTasks() {
  try {
    const r = await api.downloadTasks()
    error.value = ''
    tasks.value = r.tasks
    diffAndNotify(r.tasks)
    if (detailVisible.value && detailId.value) void fetchDetail()
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
    tasks.value = list
    diffAndNotify(list)
    if (detailVisible.value && detailId.value) void fetchDetail()
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

      <!-- D3 状态筛选 + D9 清空历史 + R2 实时通道标识 -->
      <div class="toolbar">
        <el-segmented v-model="filterStatus" :options="filterOptions" />
        <span v-if="usingSse" class="sse-badge" title="服务端推送，进度亚秒级更新">实时推送</span>
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

    <!-- 任务详情抽屉（R1：明细按需加载） -->
    <el-drawer
      v-model="detailVisible"
      :title="`任务详情 ${detailTask?.id ?? detailId}`"
      size="55%"
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

        <div class="detail-title">按链接明细（{{ parsedLogs.groups.length }}）</div>
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
            </template>
            <pre class="log-detail">{{ g.lines.join('\n') }}</pre>
          </el-collapse-item>
        </el-collapse>

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
