<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FolderOpened, Refresh } from '@element-plus/icons-vue'
import { api, isAborted, type DownloadTask } from '../api'

const tasks = ref<DownloadTask[]>([])
const loading = ref(false)
const detail = ref<DownloadTask | null>(null)
const drawerVisible = ref(false)

/** 轮询句柄：页面存活期间每 3 秒静默刷新，运行中任务的进度与逐 URL 明细实时更新 */
let pollTimer: ReturnType<typeof setInterval> | null = null
const POLL_INTERVAL = 3000
/** 页面可见性：后台隐藏时暂停轮询，恢复可见时立即刷新并重启 */
let pageVisible = true
let polling = false

function statusType(s: string): 'success' | 'danger' | 'warning' | 'primary' | 'info' {
  if (s === 'done') return 'success'
  if (s === 'failed') return 'danger'
  if (s === 'cancelled') return 'warning'
  if (s === 'running') return 'primary'
  return 'info'
}

function statusText(s: string): string {
  if (s === 'done') return '完成'
  if (s === 'failed') return '失败'
  if (s === 'cancelled') return '已取消'
  if (s === 'running') return '进行中'
  if (s === 'pending') return '排队中'
  return s
}

/** 进度百分比：已完成 URL / 总 URL（前后端统一口径） */
function progressOf(t: DownloadTask): number {
  if (!t.total) return 0
  return Math.round((t.done / t.total) * 100)
}

function itemType(s: string): 'success' | 'danger' | 'warning' | 'info' {
  if (s === 'ok') return 'success'
  if (s === 'fail') return 'danger'
  if (s === 'skip') return 'warning'
  return 'info'
}

function itemText(s: string): string {
  if (s === 'ok') return '成功'
  if (s === 'fail') return '失败'
  if (s === 'skip') return '已存在跳过'
  if (s === 'cancelled') return '已取消'
  return '等待中'
}

/** URL 下载统计文本（跳过/失败为附加明细不参与成功统计，这里同样不展示） */
function statsText(stats: Record<string, number>): string {
  const parts = Object.entries(stats).filter(([k]) => k !== '跳过' && k !== '失败')
  return parts.map(([k, v]) => `${k} ${v}`).join(' ')
}

/** 汇总某任务的成功/跳过/失败数 */
function summarize(t: DownloadTask) {
  let ok = 0
  let skip = 0
  let fail = 0
  for (const it of t.items) {
    if (it.status === 'ok') ok++
    else if (it.status === 'skip') skip++
    else if (it.status === 'fail') fail++
  }
  return { ok, skip, fail }
}

/** 加载任务列表；打开详情抽屉时同步刷新选中任务 */
async function loadTasks(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const r = await api.downloadTasks()
    tasks.value = r.tasks
    if (detail.value) {
      const cur = r.tasks.find((t) => t.id === detail.value!.id)
      detail.value = cur ?? null
      if (!cur) drawerVisible.value = false
    }
  } catch (e) {
    if (isAborted(e)) return
    if (!quiet) ElMessage.error(`加载下载任务失败: ${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

/** 静默刷新：防重入（上一轮未完成则跳过本轮），单次失败忽略下轮重试 */
async function refreshQuiet() {
  if (polling) return
  polling = true
  try {
    await loadTasks(true)
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

function showDetail(row: DownloadTask) {
  detail.value = row
  drawerVisible.value = true
}

async function cancelTask(row: DownloadTask) {
  try {
    await api.cancelDownload(row.id)
    ElMessage.success('已请求取消')
    refreshQuiet()
  } catch (e) {
    ElMessage.error(`取消任务失败: ${(e as Error).message}`)
  }
}

async function deleteTask(row: DownloadTask) {
  try {
    await ElMessageBox.confirm(
      '确定删除该任务记录？运行中的任务将被取消，已下载文件不受影响。',
      '删除下载任务',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '保留' },
    )
  } catch {
    return
  }
  try {
    await api.deleteDownload(row.id)
    ElMessage.success('已删除')
    refreshQuiet()
  } catch (e) {
    ElMessage.error(`删除任务失败: ${(e as Error).message}`)
  }
}

onMounted(() => {
  loadTasks()
  syncPoll()
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div v-loading="loading">
    <div class="page-card">
      <div class="note">
        <el-icon style="margin-right: 6px"><InfoFilled /></el-icon>
        <span>
          下载中心：提交 URL（帖子页/总览页的「下载」按钮或「批量下载」勾选）后，
          后端调用 <code>download_files.py</code> 的
          <code>process_one</code> 逐个下载图片/视频/种子/磁力/云盘到 <code>downloads/</code>。
          单任务内按配置并发（默认 2），任务之间排队执行；
          本页每 3 秒自动刷新进度，任务历史持久化，服务重启不丢失。
          下载完成可在「资源管理」查看已下载的文件。
        </span>
      </div>

      <div class="toolbar">
        <el-button :icon="Refresh" :loading="loading" @click="loadTasks()">刷新</el-button>
        <el-button :icon="FolderOpened" @click="$router.push('/resources')">前往资源管理</el-button>
      </div>

      <el-table v-if="tasks.length" :data="tasks" style="width: 100%">
        <el-table-column label="创建时间" :min-width="150">
          <template #default="{ row }">{{ row.created_at }}</template>
        </el-table-column>
        <el-table-column label="状态" :min-width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="链接数" :min-width="70">
          <template #default="{ row }">{{ row.done }}/{{ row.total }}</template>
        </el-table-column>
        <el-table-column label="进度" :min-width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="progressOf(row)"
              :stroke-width="10"
              :status="row.status === 'done' ? 'success' : row.status === 'failed' || row.status === 'cancelled' ? 'exception' : undefined"
            />
          </template>
        </el-table-column>
        <el-table-column label="结果" :min-width="110">
          <template #default="{ row }">
            <template v-if="row.status !== 'pending'">
              <el-tag size="small" type="success" v-if="summarize(row).ok">成功 {{ summarize(row).ok }}</el-tag>
              <el-tag size="small" type="warning" v-if="summarize(row).skip" style="margin-left: 4px">跳过 {{ summarize(row).skip }}</el-tag>
              <el-tag size="small" type="danger" v-if="summarize(row).fail" style="margin-left: 4px">失败 {{ summarize(row).fail }}</el-tag>
            </template>
          </template>
        </el-table-column>
        <el-table-column label="操作" :min-width="160" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="showDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'pending' || row.status === 'running'"
              link
              type="warning"
              size="small"
              :disabled="row.cancel_requested"
              @click="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button link type="danger" size="small" @click="deleteTask(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="暂无下载任务（在帖子页选择链接提交下载）" />
    </div>

    <el-drawer v-model="drawerVisible" size="560px" :title="`下载任务 ${detail?.id ?? ''}`" append-to-body>
      <template v-if="detail">
        <div class="drawer-head">
          <el-tag size="small" :type="statusType(detail.status)">{{ statusText(detail.status) }}</el-tag>
          <span class="text-muted">创建 {{ detail.created_at }}</span>
          <span class="text-muted">完成 {{ detail.finished_at || '—' }}</span>
        </div>
        <el-progress
          :percentage="progressOf(detail)"
          :stroke-width="10"
          :status="detail.status === 'done' ? 'success' : detail.status === 'failed' || detail.status === 'cancelled' ? 'exception' : undefined"
          style="margin: 14px 0"
        />
        <div class="section-title">逐链接明细（{{ detail.done }}/{{ detail.total }}）</div>
        <el-table :data="detail.items" size="small" max-height="280" style="width: 100%">
          <el-table-column label="URL" :min-width="200" show-overflow-tooltip>
            <template #default="{ row }">{{ row.url }}</template>
          </el-table-column>
          <el-table-column label="状态" :min-width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="itemType(row.status)">{{ itemText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="下载结果" :min-width="110">
            <template #default="{ row }">{{ statsText(row.stats) || (row.error ? '失败' : '—') }}</template>
          </el-table-column>
        </el-table>
        <div class="section-title" style="margin-top: 16px">任务日志</div>
        <pre class="log-box">{{ detail.logs.join('\n') || '（无日志）' }}</pre>
      </template>
    </el-drawer>
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

.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.drawer-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.text-muted {
  color: #909399;
  font-size: 12px;
}

.section-title {
  font-weight: 600;
  color: #1f2d3d;
  margin-bottom: 8px;
}

.log-box {
  background: #0d1117;
  color: #c9d1d9;
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.7;
  max-height: 300px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
