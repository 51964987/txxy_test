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
import { useAppStore } from '../stores/app'
import { briefList } from '../utils/text'

const app = useAppStore()
/** 与布局层同一断点（<768px）：窄屏下表格列宽固定必然横向溢出，
 *  操作列会被截掉大半，故小屏改为卡片列表（与 ResourcesView 同一模式）。 */
const isMobile = computed(() => app.isMobile)

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
/** 状态展示优先级：正在下载 → 排队中 → 失败 → 已取消 → 已完成。
 *  后端按 dict 遍历返回，顺序与状态无关且刷新可能变化，故在展示层统一排序，
 *  保证「刷新前后位置不变、运行中的始终在最前」；终态内「已完成」排在「已取消」
 *  之后沉底——历史成功任务不再干扰对失败/已取消任务的后续处理。 */
const STATUS_RANK: Record<string, number> = { running: 0, pending: 1, failed: 2, cancelled: 3, done: 4 }
function statusRank(status: string): number {
  return STATUS_RANK[status] ?? 2
}

const filteredTasks = computed<DownloadTaskSummary[]>(() => {
  let list: DownloadTaskSummary[]
  if (filterStatus.value === 'all') list = tasks.value
  else if (filterStatus.value === 'active')
    list = tasks.value.filter((t) => t.status === 'running' || t.status === 'pending')
  else list = tasks.value.filter((t) => t.status === filterStatus.value)
  // 复制后排序（不原地改 tasks，避免影响其它引用）
  return [...list].sort((a, b) => {
    const ra = statusRank(a.status)
    const rb = statusRank(b.status)
    if (ra !== rb) return ra - rb
    // 排队中/进行中：与实际执行顺序一致——被「优先执行」置顶的在前（队列优先级 0），
    // 其余按入队令牌 ticket 升序（先入队先执行，与 PriorityQueue 出队顺序相同）。
    // 此前按创建时间倒序展示，与 FIFO 执行顺序相反，让人误以为最新任务会先下载。
    if (ra <= 1) {
      if (!!a.priority !== !!b.priority) return a.priority ? -1 : 1
      const ta = a.ticket ?? 0
      const tb = b.ticket ?? 0
      if (ta !== tb) return ta - tb
      return a.id < b.id ? -1 : 1
    }
    // 终态：按创建时间倒序（新的在前），时间相同用 id 兜底保证排序完全稳定
    if (a.created_at !== b.created_at) return a.created_at < b.created_at ? 1 : -1
    return a.id < b.id ? -1 : 1
  })
})

/** 任务列表分页：与链接明细同一模式的前端切片分页（业界实时面板做法的轻量实现：
 *  活跃任务按排序始终置顶、不受翻页影响；历史终态区按页浏览。任务保留上限 200，
 *  此规模无需虚拟滚动/后端分页）。 */
const TASK_PAGE_SIZE = 20
const taskPage = ref(1)
const pagedTasks = computed(() =>
  filteredTasks.value.slice(
    (taskPage.value - 1) * TASK_PAGE_SIZE,
    taskPage.value * TASK_PAGE_SIZE,
  ),
)
// 切换状态筛选时回到第一页，避免停留在超出范围的页码
watch(filterStatus, () => {
  taskPage.value = 1
})

// ---- D2 重复提交提醒：区分「文件仍在 / 已不在 / 正在下载」三类 ----
// （escapeHtml / briefList 已抽到 utils/text.ts，与帖子浏览下载入口共用）

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
    ElMessage.success(`已在原任务内重跑 ${r.retried} 个未成功链接`)
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
  if (!tasks.value.some((t) => t.status === 'done')) {
    ElMessage.info('当前没有已完成的任务')
    return
  }
  const go = await ElMessageBox.confirm(
    '将删除全部「已完成」任务记录（不影响已下载文件）；失败 / 已取消任务会保留，确定吗？',
    '清空已完成任务',
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

/** 明细日志的渲染窗口（行数，按全部明细行计，非每组）。

  *  背景：一个任务的过程日志可达数千行（后端上限 2000 行 / 近 20 万字符），
  *  此前 <pre> 一次性渲染全部行，每 500ms 有更新就整体重建这个巨大的文本节点，
  *  浏览器重排/重绘成本极高——表现为「详情打开后日志一多就卡住」。
  *  而日志窗可视范围只有十几行，全量渲染纯属浪费。
  *  故按业界日志面板（CI / 终端）做法只保留最近 N 行：滚动窗口 + 超出部分丢弃，
  *  渲染量降到约 1/7，更新即流畅；需要看更早内容时可用「下载日志」。 */
const MAX_DETAIL_LINES = 300

/** 解析任务日志：非缩进行为任务事件 / 逐 URL 结果摘要；
      *  缩进的 [i/N] 行（下载过程明细）归入对应链接组，供折叠展示 */
const parsedLogs = computed(() => {
  const events: string[] = []
  /** 每个链接一组：必须按 seq 聚合，不能按出现顺序分组——
   *  并发下载时多个链接的日志是交错写入的（[9/50]、[8/50]、[9/50]…），
   *  按出现顺序分组会为每行新建一组（实测产生 1071 个折叠面板，是卡顿的主因）。 */
  const groupMap = new Map<
    string,
    { seq: string; title: string; lines: string[]; total: number }
  >()
  const detailLines: { seq: string; text: string }[] = []

  for (const raw of detailTask.value?.logs || []) {
    // 明细行：前导空白 + [i/N]（由后端缩进标注归属）
    const detail = raw.match(/^\s+\[(\d+\/\d+)\]\s?(.*)$/)
    if (detail) {
      detailLines.push({ seq: detail[1], text: detail[2] })
      continue
    }
    // 结果摘要行：[i/N] 开头（无缩进）—— 兼作该链接分组的标题
    const summary = raw.match(/^\[(\d+\/\d+)\]\s?(.*)$/)
    if (summary) {
      const g = groupMap.get(summary[1]) ?? {
        seq: summary[1],
        title: '',
        lines: [],
        total: 0,
      }
      g.title = summary[2]
      groupMap.set(summary[1], g)
      events.push(raw)
      continue
    }
    events.push(raw)
  }

  // 全局尾部窗口：只渲染最近 N 行明细（日志可达数千行，全量渲染必然卡）
  const hiddenTotal = Math.max(0, detailLines.length - MAX_DETAIL_LINES)
  for (const { seq, text } of detailLines.slice(-MAX_DETAIL_LINES)) {
    const g = groupMap.get(seq) ?? { seq, title: '', lines: [], total: 0 }
    g.lines.push(text)
    g.total += 1
    groupMap.set(seq, g)
  }

  // 按链接序号升序（'9/50' → 9）
  const groups = [...groupMap.values()].sort(
    (a, b) => Number(a.seq.split('/')[0]) - Number(b.seq.split('/')[0]),
  )
  return { events, groups, hiddenTotal, detailTotal: detailLines.length }
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

    // ---- 「按链接明细」固定高度日志窗：内容增长时自动跟随到底部 ----
    const detailScrollRef = ref<HTMLElement | null>(null)
    /** 是否跟随最新：用户上翻查看历史时自动关闭，滚回底部自动恢复 */
    const followLatest = ref(true)

    // ---- 链接明细：统计 + 状态筛选（几十条明细按业界 CI 做法：异常优先、可筛选） ----
    /** 明细状态筛选：all=全部 / problem=异常（失败+取消）/ ok=成功 */
    const itemFilter = ref<'all' | 'problem' | 'ok'>('all')

    const itemStats = computed(() => {
      const items = detailTask.value?.items ?? []
      const n = (s: string) => items.filter((i) => i.status === s).length
      const ok = n('ok')
      const fail = n('fail')
      const cancelled = n('cancelled')
      return { total: items.length, ok, fail, cancelled, skip: n('skip'), problem: fail + cancelled }
    })

    /** 异常优先排序 + 状态筛选：让用户第一眼看到需要处理的链接 */
    const filteredItems = computed(() => {
      const items = [...(detailTask.value?.items ?? [])]
      // 异常（fail/cancelled）在前，其次 pending/running，成功与跳过在后
      const rank: Record<string, number> = { fail: 0, cancelled: 0, running: 1, pending: 1 }
      items.sort((a, b) => (rank[a.status] ?? 2) - (rank[b.status] ?? 2))
      if (itemFilter.value === 'problem') return items.filter((i) => (rank[i.status] ?? 2) === 0)
      if (itemFilter.value === 'ok') return items.filter((i) => i.status === 'ok' || i.status === 'skip')
      return items
    })

    /** 明细分页：几十上百条链接全量渲染既卡又难翻找，数据已在内存，前端切片分页 */
    const ITEM_PAGE_SIZE = 20
    const itemPage = ref(1)
    const pagedItems = computed(() =>
      filteredItems.value.slice(
        (itemPage.value - 1) * ITEM_PAGE_SIZE,
        itemPage.value * ITEM_PAGE_SIZE,
      ),
    )
    // 序号列跨页连续显示（第 2 页从 21 开始，而非每页重新从 1 计数）
    function itemIndex(i: number): number {
      return (itemPage.value - 1) * ITEM_PAGE_SIZE + i + 1
    }
    // 切换任务或筛选条件时回到第一页，避免停留在超出范围的页码
    watch([itemFilter, () => detailTask.value?.id], () => {
      itemPage.value = 1
    })

    /** 明细行「重新下载」的可用性与提示：成功/跳过无需重下，进行中/排队中不可重下 */
    function itemRetryDisabled(status: string): boolean {
      return status === 'ok' || status === 'skip' || status === 'running' || status === 'pending'
    }
    function itemRetryTip(status: string): string {
      if (status === 'ok' || status === 'skip') return '该链接已成功，无需重下'
      if (status === 'running' || status === 'pending') return '该链接正在下载或排队中，暂不能重下'
      return '重新下载该链接（在本任务内重跑，结果显示在当前详情）'
    }

    /** 重新下载单条链接：就地重跑原任务的该链接（不另开任务），结果显示在当前详情页 */
    async function retryItem(row: { url: string; status: string }) {
      const tid = detailTask.value?.id
      if (!tid) return
      try {
        await api.retryDownloadUrl(tid, row.url)
        ElMessage.success('已在本任务内重新下载，可在当前页面查看进度')
      } catch (e) {
        if (isAborted(e)) return
        ElMessage.error(`重新下载失败: ${(e as Error).message}`)
      }
    }

    /** 日志指纹：行数 + 末行内容，任一变化即视为有新输出 */
    const detailLogKey = computed(() => {
      const logs = detailTask.value?.logs ?? []
      return `${logs.length}:${logs[logs.length - 1] ?? ''}`
    })

    function onDetailScroll() {
      const el = detailScrollRef.value
      if (!el) return
      // 距底部 24px 内视为「在底部」，恢复跟随
      followLatest.value = el.scrollHeight - el.scrollTop - el.clientHeight < 24
    }

    watch(detailLogKey, async () => {
      if (!followLatest.value) return
      await nextTick()
      const el = detailScrollRef.value
      if (el) el.scrollTop = el.scrollHeight
    })

    /** 切换任务时也回到顶部重新跟随 */
    watch(detailId, () => {
      followLatest.value = true
    })

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
        <!-- 只清「已完成」（done）任务：失败 / 已取消保留（2026-09-07 经用户确认变更）；
             自动轮转（TXXY_DOWNLOAD_TASK_MAX_KEEP）仍按全部终态裁剪以防持久化膨胀 -->
        <el-button class="clear-btn" @click="clearFinished">清空已完成</el-button>
      </div>

      <div v-if="error" class="poll-error">
        轮询失败：{{ error }}（每 {{ REFRESH_INTERVAL / 1000 }} 秒自动重试）
      </div>

      <el-table v-if="!isMobile" :data="pagedTasks" size="default" style="width: 100%">
        <el-table-column label="任务 ID" width="130">
          <template #default="{ row }">
            <span class="task-id" :title="row.id">{{ row.id.slice(0, 10) }}</span>
            <el-tag v-if="row.priority" size="small" type="success" class="prio-tag">置顶</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" min-width="150">
          <template #default="{ row }">
            <el-progress
              :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
              :stroke-width="8"
            >
              <span class="progress-text">{{ row.done }}/{{ row.total }}</span>
            </el-progress>
          </template>
        </el-table-column>
        <!-- show-overflow-tooltip：单行省略号 + 悬浮显示完整时间，杜绝换行 -->
        <el-table-column prop="created_at" label="创建时间" width="150" show-overflow-tooltip />
        <el-table-column label="操作" width="200" fixed="right">
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
            <!-- 已取消任务同样可重试：重跑其全部未成功（已取消）链接，原任务内更新进度 -->
            <el-button
              v-if="row.status === 'failed' || row.status === 'cancelled'"
              link
              type="warning"
              @click="retryTask(row)"
            >
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

      <!-- 移动端卡片：与桌面表格同源数据，主信息 + 副信息 + 操作（操作全部可见，
           不再被固定列宽截断；结构与 ResourcesView 的 file-cards 同一模式） -->
      <div v-else class="task-cards">
        <div v-for="row in pagedTasks" :key="row.id" class="task-card">
          <div class="tc-head">
            <el-tag size="small" :type="statusTagType(row.status)">{{ statusText(row.status) }}</el-tag>
            <span class="tc-id" :title="row.id">{{ row.id.slice(0, 10) }}</span>
            <el-tag v-if="row.priority" size="small" type="success">置顶</el-tag>
          </div>
          <div class="tc-meta text-muted">
            <span>{{ row.created_at }}</span>
            <span>进度 {{ row.done }}/{{ row.total }}</span>
          </div>
          <el-progress
            :percentage="row.total ? Math.round((row.done / row.total) * 100) : 0"
            :stroke-width="6"
            class="tc-progress"
          />
          <div class="tc-ops">
            <el-button size="small" type="primary" link @click="showDetail(row)">详情</el-button>
            <el-button
              v-if="row.status === 'pending'"
              size="small"
              type="success"
              link
              @click="prioritizeTask(row)"
            >
              优先执行
            </el-button>
            <!-- 已取消任务同样可重试：重跑其全部未成功（已取消）链接，原任务内更新进度 -->
            <el-button
              v-if="row.status === 'failed' || row.status === 'cancelled'"
              size="small"
              type="warning"
              link
              @click="retryTask(row)"
            >
              重试
            </el-button>
            <el-button
              v-if="!TERMINAL.includes(row.status)"
              size="small"
              type="danger"
              link
              @click="cancelTask(row)"
            >
              取消
            </el-button>
            <el-button v-if="TERMINAL.includes(row.status)" size="small" type="danger" link @click="deleteTask(row)">
              删除
            </el-button>
          </div>
        </div>
        <el-empty
          v-if="!filteredTasks.length && !error"
          description="暂无下载任务，可在上方粘贴链接提交"
        />
      </div>

      <!-- 任务列表分页：仅超过一页时显示（桌面表格与移动卡片共用同一份切片数据）。
           注意必须放在 v-if/v-else 两个列表之后：v-else 要求与 v-if 相邻，
           分页条插在中间会把 v-else 错配到分页条的 v-if 上，导致两套列表同时渲染 -->
      <el-pagination
        v-if="filteredTasks.length > TASK_PAGE_SIZE"
        v-model:current-page="taskPage"
        :page-size="TASK_PAGE_SIZE"
        :total="filteredTasks.length"
        layout="total, prev, pager, next"
        class="tasks-pagination"
      />

      <el-empty v-if="!isMobile && tasks.length === 0 && !error" description="暂无下载任务，可在上方粘贴链接提交" />
    </div>

    <!-- 任务详情抽屉（R1：明细按需加载） -->
    <el-drawer
      v-model="detailVisible"
      :title="`任务详情 ${detailTask?.id ?? detailId}`"
      :size="isMobile ? '100%' : '55%'"
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

        <div class="detail-title">
          按链接明细（{{ parsedLogs.groups.length }}）
          <span v-if="parsedLogs.hiddenTotal" class="follow-tip">
            仅显示最近 {{ MAX_DETAIL_LINES }} 行（共 {{ parsedLogs.detailTotal }} 行）
          </span>
          <span v-if="!followLatest" class="follow-tip">已暂停跟随，滚到底部恢复</span>
        </div>
        <!-- 固定高度的日志窗：内容增长时自动跟随到底部（业界 CI 日志面板做法），
             用户上翻看历史时暂停跟随，避免把正在阅读的内容顶走 -->
        <div
          ref="detailScrollRef"
          class="detail-log-scroll"
          @scroll="onDetailScroll"
        >
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
          <div v-else class="text-muted log-empty">暂无下载过程明细</div>
        </div>

        <!-- 链接明细：几十条起步，按业界 CI 做法处理——
             先给统计与状态筛选，异常项优先排序，列表固定高度内部滚动（不撑长抽屉） -->
        <div class="detail-title">
          链接明细（{{ itemStats.total }}）
          <span class="items-summary">
            成功 {{ itemStats.ok }} · 失败 {{ itemStats.fail }} · 取消 {{ itemStats.cancelled }} ·
            跳过 {{ itemStats.skip }}
          </span>
        </div>
        <div class="items-filter">
          <el-radio-group v-model="itemFilter" size="small">
            <el-radio-button value="all">全部（{{ itemStats.total }}）</el-radio-button>
            <el-radio-button value="problem">
              异常（{{ itemStats.problem }}）
            </el-radio-button>
            <el-radio-button value="ok">成功（{{ itemStats.ok }}）</el-radio-button>
          </el-radio-group>
          <span class="text-muted items-hint">异常项排在最前，可直接「重新下载」</span>
        </div>
        <el-table :data="pagedItems" size="small" border height="300">
          <el-table-column type="index" label="#" width="46" :index="itemIndex" />
          <el-table-column label="URL" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">{{ row.url }}</template>
          </el-table-column>
          <el-table-column label="状态" width="82">
            <template #default="{ row }">
              <el-tag size="small" :type="itemTagType(row.status)">{{ itemText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="结果" min-width="130" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="row.error" class="error-text">{{ row.error }}</span>
              <span v-else-if="Object.keys(row.stats || {}).length">
                {{ Object.entries(row.stats).map(([k, v]) => `${k} ${v}`).join(', ') }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="72">
            <template #default="{ row }">
              <span>{{ row.elapsed != null ? `${row.elapsed}s` : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="88" fixed="right">
            <template #default="{ row }">
              <!-- 成功/跳过无需重下；running/pending 正在处理不可重下 -->
              <el-tooltip :content="itemRetryTip(row.status)" placement="top">
                <el-button
                  link
                  type="primary"
                  :disabled="itemRetryDisabled(row.status)"
                  @click="retryItem(row)"
                >
                  重新下载
                </el-button>
              </el-tooltip>
            </template>
          </el-table-column>
        </el-table>
        <!-- 明细分页：纯前端切片（数据已在内存），仅超过一页时显示 -->
        <el-pagination
          v-if="filteredItems.length > ITEM_PAGE_SIZE"
          v-model:current-page="itemPage"
          :page-size="ITEM_PAGE_SIZE"
          :total="filteredItems.length"
          layout="total, prev, pager, next"
          size="small"
          class="items-pagination"
        />
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

/* ================= 移动端适配 =================
   与 ResourcesView 的 file-cards 同一模式：断点沿用布局层 isMobile（<768px），
   表格列宽固定 + 右侧固定列在窄屏会横向溢出、操作列被截去大半，
   故小屏改为单列卡片（主信息 + 副信息 + 操作），操作按钮全部可见。 */
.task-cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-height: 56px;
  padding: 10px 12px;
  background: #fafcff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
}

.tc-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.tc-id {
  font-family: Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
}

.tc-meta {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  font-size: 12px;
}

.tc-progress {
  margin: 2px 0;
}

/* 操作区允许换行：保证按钮全部显示，不会被裁掉 */
.tc-ops {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 10px;
  border-top: 1px dashed #ebeef5;
  padding-top: 6px;
}

/* 「按链接明细」日志窗：固定高度 + 内部滚动，新日志自动跟随到底部 */
.detail-log-scroll {
  height: 260px;
  overflow-y: auto;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  padding: 4px 8px;
  background: #fafcff;
}

.follow-tip {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #e6a23c;
}

.log-empty {
  font-size: 12px;
  padding: 8px 2px;
}

.log-trim-tip {
  color: #909399;
  font-size: 12px;
  padding: 2px 0 4px;
}

/* 链接明细：统计摘要 + 状态筛选（几十条时先给结论、再给筛选） */
.items-summary {
  margin-left: 8px;
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}

.items-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

/* 明细分页条：与表格保持紧凑间距 */
.items-pagination {
  margin-top: 8px;
  justify-content: flex-end;
}

/* 任务列表分页条 */
.tasks-pagination {
  margin-top: 10px;
  justify-content: flex-end;
}

.items-hint {
  font-size: 12px;
}

@media (max-width: 768px) {
  /* 手机屏高度紧张，日志窗略矮 */
  .detail-log-scroll {
    height: 200px;
  }
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
