<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  api,
  isAborted,
  type BlacklistItem,
  type KbLogLine,
  type KbLogProgress,
  type ScheduleAction,
  type ScheduleStatus,
  type SettingItem,
} from '../api'
import { useAppStore } from '../stores/app'
import { useDashboardStore } from '../stores/dashboard'

const app = useAppStore()
// 自动刷新总开关的实际状态在 Dashboard store（Header 与数据总览共用同一份）
const dash = useDashboardStore()
const isMobile = computed(() => app.isMobile)

const loading = ref(false)
const loadError = ref('')
const items = ref<SettingItem[]>([])
// 当前选中的左侧导航项：分组索引或 'blacklist'（macOS/Windows 设置风格：左分类、右面板）
const activeNav = ref<number | 'blacklist'>(0)
// 逐条自动保存：记录正在保存的键，仅用于行内「保存中…」提示；不再有全局草稿/未保存态
const savingMap = ref<Record<string, boolean>>({})
const lastSavedAt = ref<number | null>(null)
const lastSavedText = computed(() => {
  if (!lastSavedAt.value) return '改动将自动保存，无需点「保存」'
  const d = new Date(lastSavedAt.value)
  const p = (n: number) => String(n).padStart(2, '0')
  return `已自动保存 · ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
})

/** 分组：与后端白名单顺序一致，按业务域切分（业界设置页通行做法）。
 *  desc 可选：组内各项已有自述时不再重复加组级说明（「内容资产」组即如此，2026-09-13 文案收敛） */
const GROUPS: { title: string; desc?: string; keys: string[]; extra?: 'schedule' | 'precipitate' | 'kbschedule' }[] = [
  {
    title: '访问链',
    desc: '抓取 / 中继 / 下载共用的有序端点：某项连不上自动切下一项，最后一项为公网主域兜底（不能是内网地址）。保存后看板立即生效；抓取批次自下一批生效',
    keys: ['fetch_chain'],
  },
  {
    title: '定时抓取',
    // 组级说明只讲一件用户必须知道的事：调度的唯一来源（避免与 OS 定时重复触发）
    desc: '由本服务按时刻自动启动抓取，保存后立即生效（Windows 计划任务与容器 cron 均已停用，同一时刻只应有一处调度）',
    extra: 'schedule',
    keys: [
      'scrape_schedule_enabled',
      'scrape_schedule_times',
      'scrape_schedule_restart',
      'scrape_schedule_use_proxy',
      'scrape_schedule_miss_tolerance',
    ],
  },
  {
    title: '抓取节流',
    desc: '并发与节奏直接影响源站压力，过高可能触发限流或封禁；保存后下一批抓取生效',
    keys: [
      'scrape_max_workers',
      'scrape_stagger_delay',
      'scrape_page_interval',
      'scrape_max_retries',
    ],
  },
  {
    title: '自动下载',
    desc: '按下面的筛选条件，每天在设定时刻自动把当天发布的帖子（含媒体与链接清单）提交到下载中心（downloads/）自动下载；保存后立即生效。建议自动下载时刻排在抓取时刻之后，确保当天数据已入库。',
    extra: 'precipitate',
    keys: [
      'precipitate_enabled',
      'precipitate_times',
      'precipitate_keywords',
      'precipitate_min_likes',
      'precipitate_min_replies',
      'precipitate_fids',
      'precipitate_min_free_gb',
    ],
  },
  {
    title: '知识库',
    desc: '把已入库帖子的正文沉淀为本地 Markdown 知识库（raw 原始件 + 笔记 + 实体页）。首次建库走项目根 kb_export.py 手动分片批次，本组只管日常定时增量；时刻与抓取 / 自动下载重叠会叠加镜像链压力',
    extra: 'kbschedule',
    keys: [
      'kb_export_enabled',
      'kb_export_times',
      'kb_fetch_interval',
      'kb_batch_max_posts',
      'kb_fids',
      'kb_date_scope',
      'llm_provider',
      'llm_model',
      'llm_base_url',
    ],
  },
  {
    title: '下载',
    desc: '并发与节流直接影响源站压力，过高可能触发限流或封禁',
    keys: [
      'download_concurrency',
      'download_task_concurrency',
      'download_max_batch',
      'download_task_max_keep',
      'download_interval',
      'download_max_retries',
      'download_retry_delay',
    ],
  },
  {
    title: '资源与回收站',
    desc: '影响资源扫描实时性与回收站保留策略',
    keys: ['trash_keep_days', 'resources_scan_ttl'],
  },
  {
    // 不写组级说明：两项的 desc 已各自说清作用，组级再讲一遍「三档对应 / 长尾稀释」即为重复
    title: '内容资产',
    keys: ['asset_goal_scope', 'asset_goal_rate'],
  },
  {
    title: '分享链接',
    desc: '控制生成的单文件分享链接指向的主机，便于局域网他人直接打开',
    keys: ['share_host'],
  },
  {
    title: '界面',
    desc: '仅影响前端行为',
    keys: ['enable_auto_refresh'],
  },
  {
    title: '演示轮播',
    desc: '投屏演示模式（Header「演示轮播」按钮）的行为，仅前端生效，保存后立即应用',
    keys: ['carousel_interval', 'carousel_sections'],
  },
]

/** 控件本身很宽、塞不进「标签｜控件」左右分栏的设置类型：这类行改为「标签在上、控件在下」。
 *  array（勾选 + 上下移 + 「默认：…」长句）的固有宽度约 1000px，分栏时会把标签压成
 *  130px 窄条、描述文字挤成竖条（实测「演示轮播板块序列」），故与 Element Plus / Ant Design
 *  表单「复杂控件独占一行」同一做法。 */
const WIDE_TYPES: SettingItem['type'][] = ['array', 'times', 'chain']

const SCOPE_TEXT: Record<SettingItem['scope'], string> = {
  immediate: '立即生效',
  next_task: '下一个下载任务生效（正在跑的任务不受影响）',
  next_batch: '下一批抓取生效（经环境变量传播到抓取子进程）',
  frontend: '保存后前端立即应用',
}

function byKey(key: string): SettingItem | undefined {
  return items.value.find((i) => i.key === key)
}

function groupItems(keys: string[]): SettingItem[] {
  return keys.map((k) => byKey(k)).filter((i): i is SettingItem => !!i)
}

/** 当前生效值（自动保存模式下无草稿，直接读 items 的已保存值） */
function valueOf(it: SettingItem): number | boolean | string[] | string {
  return it.value
}

/** 数组型设置项的当前值 */
function arrayValue(it: SettingItem): string[] {
  return Array.isArray(it.value) ? (it.value as string[]) : []
}
function isIncluded(it: SettingItem, key: string): boolean {
  return arrayValue(it).includes(key)
}
/** 勾选/取消某板块：算出新序列后立即单条保存（自动保存，无需点保存） */
function toggleSection(it: SettingItem, key: string) {
  const cur = arrayValue(it)
  let next: string[]
  if (cur.includes(key)) {
    next = cur.filter((k) => k !== key)
  } else {
    // 新增时按 options 规范顺序插入，保证默认次序稳定
    const order = (it.options ?? []).map((o) => o.value)
    next = cur.concat(key).sort((a, b) => order.indexOf(a) - order.indexOf(b))
  }
  void saveOne(it, next)
}
function moveSection(it: SettingItem, key: string, dir: -1 | 1) {
  const cur = arrayValue(it).slice()
  const i = cur.indexOf(key)
  const j = i + dir
  if (i < 0 || j < 0 || j >= cur.length) return
  const tmp = cur[i]
  cur[i] = cur[j]
  cur[j] = tmp
  void saveOne(it, cur)
}
/** 板块序列默认值用标签展示，便于回显 */
function arrayLabels(it: SettingItem, keys: string[]): string {
  const map = new Map((it.options ?? []).map((o) => [o.value, o.label]))
  return keys.map((k) => map.get(k) ?? k).join(' → ')
}

/** 单选枚举值 → 展示标签（找不到回退原值），供 enum 类型默认值回显 */
function optionLabel(it: SettingItem, value: string): string {
  return (it.options ?? []).find((o) => o.value === value)?.label ?? value
}

/** 布尔项取值（键不存在时用兜底），供「立即运行一次」读取当前开关 */
function boolOf(key: string, fallback: boolean): boolean {
  const it = byKey(key)
  return it ? Boolean(valueOf(it)) : fallback
}

// ===== 定时抓取（页面调度）：状态展示 + 时刻列表编辑 =====
/** 调度状态：下次执行 / 今日已处理 / 上次结果 / 线程心跳 / 当前是否在跑 */
const sched = ref<ScheduleStatus | null>(null)
const runNowLoading = ref(false)
const router = useRouter()

/** 状态轮询：批次一跑就是 25~40 分钟，状态必须自己刷新，不能指望用户手动刷新页面
 *  （业界：调度面板都显示 Job 的当前执行态）。页面不可见时暂停，与下载中心同一约定。 */
const SCHED_POLL_MS = 5000
let schedTimer: number | null = null
function startSchedPolling() {
  if (schedTimer !== null) return
  schedTimer = window.setInterval(() => {
    if (!document.hidden) void loadSchedule()
  }, SCHED_POLL_MS)
}
function stopSchedPolling() {
  if (schedTimer !== null) {
    window.clearInterval(schedTimer)
    schedTimer = null
  }
}
function onVisibility() {
  if (!document.hidden) void loadSchedule()
}

/** 当前是否有批次在跑：有则禁用「立即运行一次」（与后端 start_run 的守卫同源） */
const schedRunning = computed(() => sched.value?.scrape?.running ?? null)
const runNowDisabled = computed(() => schedRunning.value !== null || runNowLoading.value)
const runNowTip = computed(() =>
  schedRunning.value
    ? '已有抓取批次在运行，等它结束后才能再启动（同一时刻只允许一个批次）'
    : '立即启动一次抓取（与当前开关/时刻无关，仅用于验证参数）',
)
/** 运行中状态文案：区分「已写入运行记录」与「刚拉起、记录还没写」两种阶段 */
const schedRunningText = computed(() => {
  const r = schedRunning.value
  if (!r) return ''
  if (r.state === 'starting') return '批次正在启动（等待写入运行记录）…'
  const run = r.run
  if (!run) return '批次运行中…'
  const mins = run.elapsed_minutes == null ? '' : `，已运行 ${run.elapsed_minutes} 分钟`
  return `批次 #${run.id} 运行中${mins}`
})

const SCHED_ACTION: Record<ScheduleAction, { text: string; type: 'success' | 'info' | 'warning' | 'danger' }> = {
  started: { text: '已启动批次', type: 'success' },
  skipped: { text: '已跳过', type: 'warning' },
  missed: { text: '未执行', type: 'info' },
  failed: { text: '启动失败', type: 'danger' },
  done: { text: '已完成', type: 'success' },
}

/** 知识库任务的动作标签（done 为批次执行完毕，与 started 的「已启动」区分） */
function kbActionTag(action: string): { text: string; type: 'success' | 'info' | 'warning' | 'danger' } {
  return SCHED_ACTION[action as ScheduleAction] ?? { text: action, type: 'info' }
}

async function loadSchedule() {
  try {
    sched.value = await api.schedule()
    reconcileKbJustStarted()
  } catch (e) {
    if (isAborted(e)) return
    // 状态读取失败不阻断参数编辑：置空并显示提示，下次刷新重试
    sched.value = null
  }
}

/** 时刻列表（times 类型）已保存值 */
function timeList(it: SettingItem): string[] {
  return Array.isArray(it.value) ? (it.value as string[]) : []
}

/** 时刻下拉选项（5 分钟步长，共 288 项）：时刻在列表内必须互不相同，故用
 *  「已占用项置灰」的下拉替代自由滚轮——业界预约/排期通行做法（订座、日历「添加时段」、
 *  cron 编辑器）：从源头阻止选到重复值，而不是选完再校验回滚。el-time-picker 的
 *  disabled-hours/minutes 只能按整小时段禁用，挡不住「08:00 已占用、08:30 可选」的
 *  精确冲突，故不采用。步长如需更细改 TIME_STEP_MIN 即可。 */
const TIME_STEP_MIN = 5
const TIME_OPTIONS: string[] = Array.from({ length: (24 * 60) / TIME_STEP_MIN }, (_, i) => {
  const m = i * TIME_STEP_MIN
  return `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`
})

/** 该时刻是否已被本列表其它行占用（自身行除外） */
function isTimeTaken(it: SettingItem, value: string, index: number): boolean {
  return timeList(it).some((t, i) => i !== index && t === value)
}

function setTimeAt(it: SettingItem, index: number, value: string) {
  if (!/^\d{2}:\d{2}$/.test(value)) return
  const next = timeList(it).slice()
  // 重复选择已被置灰挡住，此处仅作防御（存量非步长值、手改数据等边界）：
  // 后端 normalize_times 会去重，直接提交会让另一行消失
  if (next.some((t, i) => i !== index && t === value)) {
    ElMessage.warning(`时刻 ${value} 已存在`)
    return
  }
  next[index] = value
  void saveOne(it, next)
}

/** 时刻列表已达上限（上限取后端白名单 spec.max = config.MAX_SCHEDULE_TIMES，随快照下发） */
function timesAtMax(it: SettingItem): boolean {
  return it.max != null && timeList(it).length >= it.max
}

/** 添加时刻：取第一个尚未占用的整点（同一天两个相同时刻没有意义，避免用户先存出重复项）。
 *  达上限时直接拦截：后端 normalize_times 会排序后截断到上限，多提交会把最晚的时刻挤掉 */
function addTime(it: SettingItem) {
  const cur = timeList(it)
  if (timesAtMax(it)) {
    ElMessage.warning(`最多 ${it.max} 个时刻，已达上限`)
    return
  }
  const hours = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, '0')}:00`)
  const pick = hours.find((t) => !cur.includes(t)) ?? '12:00'
  void saveOne(it, [...new Set([...cur, pick])].sort())
}
function removeTime(it: SettingItem, index: number) {
  const next = timeList(it).filter((_, i) => i !== index)
  if (!next.length) {
    // 空列表等于「没有计划时刻」：与其留一个含义不明的空配置，不如引导用户去关开关
    ElMessage.warning('至少保留一个抓取时刻；如要停止定时抓取，请关闭「启用定时抓取」')
    return
  }
  void saveOne(it, next)
}

// ===== 访问链（chain 类型）：有序端点列表编辑 =====
/** 本地编辑缓冲：允许「新增占位行先补全、失焦再保存」——后端要求链成员合法且非空，
 *  占位行直接提交会被 400 拒绝，故未保存的新行只存在缓冲里，成功后回到受控渲染 */
const chainBuf = ref<string[] | null>(null)
function chainRows(it: SettingItem): string[] {
  return chainBuf.value ?? (Array.isArray(it.value) ? (it.value as string[]) : [])
}
function bufferChain(it: SettingItem, index: number, value: string) {
  const rows = chainRows(it).slice()
  rows[index] = value
  chainBuf.value = rows
}
/** 提交链编辑：过滤空白行（新增占位行补全前不参与），全空则不提交（后端要求链非空） */
async function saveChain(it: SettingItem, rows: string[]) {
  const valid = rows.map((r) => r.trim()).filter(Boolean)
  if (!valid.length) return
  savingMap.value = { ...savingMap.value, [it.key]: true }
  try {
    const r = await api.saveSettings({ [it.key]: valid })
    const updated = r.settings.find((s) => s.key === it.key)
    if (updated) {
      const idx = items.value.findIndex((x) => x.key === it.key)
      if (idx >= 0) items.value[idx] = updated
    }
    chainBuf.value = null // 成功：丢弃缓冲回到受控渲染
    lastSavedAt.value = Date.now()
  } catch (e) {
    if (isAborted(e)) return
    // 校验失败（成员非法 / 末项为内网等）：保留缓冲与输入内容供继续修改，仅提示
    ElMessage.error(`保存「${it.label}」失败: ${(e as Error).message}`)
  } finally {
    const next = { ...savingMap.value }
    delete next[it.key]
    savingMap.value = next
  }
}
function editChainAt(it: SettingItem, index: number, value: string) {
  const rows = chainRows(it).slice()
  rows[index] = value
  chainBuf.value = rows
  void saveChain(it, rows)
}
function addChainRow(it: SettingItem) {
  const rows = chainRows(it).slice()
  rows.push('https://') // 占位前缀：补全 host 失焦后才真正提交
  chainBuf.value = rows
}
function removeChainRow(it: SettingItem, index: number) {
  const rows = chainRows(it).slice()
  rows.splice(index, 1)
  chainBuf.value = rows
  void saveChain(it, rows)
}
function moveChainRow(it: SettingItem, index: number, dir: -1 | 1) {
  const rows = chainRows(it).slice()
  const j = index + dir
  if (j < 0 || j >= rows.length) return
  ;[rows[index], rows[j]] = [rows[j], rows[index]]
  chainBuf.value = rows
  void saveChain(it, rows)
}

/** 立即运行一次：复用既有手动入口（/api/runs/start，同一防重），用于验证上面的参数 */
async function runNow() {
  runNowLoading.value = true
  try {
    await api.startRun({
      use_local_proxy: boolOf('scrape_schedule_use_proxy', true),
      restart: boolOf('scrape_schedule_restart', true),
    })
    ElMessage.success('已启动抓取批次，下方「当前状态」会显示运行中')
    await loadSchedule() // 立刻回填「运行中」，不等下一次轮询
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`启动失败: ${(e as Error).message}`)
  } finally {
    runNowLoading.value = false
  }
}

/** 立即自动下载一次：复用 /api/precipitate/run（同一筛选/磁盘守卫），用于即时验证参数 */
const autoDownloadNowLoading = ref(false)

/** 调度心跳停滞判定：last_tick 距今超过 3×tick 间隔 → 调度线程可能已死。
 *  tick_age_seconds 由服务端按服务端时钟计算，避免本机时钟偏差误报 */
function tickStale(job: { tick_age_seconds?: number | null; tick_seconds?: number } | undefined): boolean {
  if (!job || job.tick_age_seconds == null) return false
  return job.tick_age_seconds > 3 * (job.tick_seconds ?? 60)
}

const precipitateDiskLow = computed(
  () => (sched.value?.precipitate?.last?.reason ?? '').includes('磁盘'),
)
async function autoDownloadNow() {
  autoDownloadNowLoading.value = true
  try {
    const r = await api.precipitateRun()
    if (!r.enabled) {
      ElMessage.warning('自动下载未启用，先在上方开启开关')
      return
    }
    // 文案一律取后端 reason（与定时「上次结果」同源）：零结果时后端已区分「当天无数据入库 /
    // 有数据但没命中 / 命中但已下载过」，前端自拼会丢掉这层解释
    if (r.disk_low) {
      ElMessage.warning(r.reason)
    } else {
      ElMessage.success(r.reason)
    }
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`自动下载失败: ${(e as Error).message}`)
  } finally {
    autoDownloadNowLoading.value = false
    await loadSchedule() // 立刻回填「上次结果 / 今日已处理」，不等下一次轮询
  }
}

/** 立即执行一次知识库增量：复用 /api/kb/run（与定时同一入口/筛选/状态），异步执行。
 *  按钮状态机（穷举，规则 6）：空闲可点 / 本地刚启动（5s 轮询空窗）/ 批次运行中禁用 /
 *  抓取批次运行中禁用（后端 409 前置拦截，原因在 tooltip 露出）。
 *  「运行中」唯一判据 = 快照 sched.kb.running（后端 KbJob._worker.is_alive()），
 *  前端不自行推断。 */
const kbRunNowLoading = ref(false)
/** 本地防重标志：POST 秒回后、下一次轮询看到 running 前的空窗期拦截重复点击；
 *  值为点击时刻的 Date.now()（毫秒），交棒判定见 reconcileKbJustStarted */
const kbJustStartedAt = ref<number | null>(null)
const kbRunningNow = computed(() => Boolean(sched.value?.kb?.running) || kbJustStartedAt.value !== null)
const kbScrapeRunning = computed(() => Boolean(sched.value?.scrape?.running))
const kbRunDisabled = computed(() => kbRunningNow.value || kbScrapeRunning.value)
const kbRunTip = computed(() =>
  kbScrapeRunning.value
    ? '抓取批次正在运行，为避免叠加镜像链压力，等抓取结束后再触发'
    : kbRunningNow.value
      ? '知识库增量批次执行中，点「执行日志」查看实时进度'
      : '立即按当前筛选条件执行一次增量批次（与定时同一入口与状态），用于验证筛选与落盘效果',
)
/** loadSchedule 拿到快照后交棒：轮询看到 running 即解除本地防重；超 15s（3 个轮询
 *  周期）仍未观察到也解除（秒级小批次可能在两次轮询间直接跑完）。不与 last.at 比较：
 *  其粒度为分钟（"HH:MM"），无法区分「本次触发」与「上一轮结果」。 */
function reconcileKbJustStarted() {
  if (kbJustStartedAt.value === null) return
  if (sched.value?.kb?.running || Date.now() - kbJustStartedAt.value > 15_000) {
    kbJustStartedAt.value = null
  }
}
async function kbRunNow() {
  kbRunNowLoading.value = true
  try {
    await api.kbRun()
    kbJustStartedAt.value = Date.now()
    ElMessage.success('已启动知识库增量批次（后台执行，「执行日志」可看实时进度）')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`知识库增量触发失败: ${(e as Error).message}`)
  } finally {
    kbRunNowLoading.value = false
    await loadSchedule() // 立刻回填「状态 / 上次结果」，不等下一次轮询
  }
}

// ===== 执行日志抽屉：增量轮询 kb_export 进程内环形缓冲（业界 build log 通行做法：
// after 游标 + 跟随尾部 + 上滚暂停）。仅内存切片，抽屉关闭 / 页面隐藏即暂停。 =====
const KB_LOG_POLL_MS = 2000
const kbLogOpen = ref(false)
const kbLogLines = ref<KbLogLine[]>([])
const kbLogLastSeq = ref(0)
const kbLogProgress = ref<KbLogProgress | null>(null)
const kbLogRunning = ref(false)
const kbLogError = ref('')
const kbLogFollow = ref(true) // 自动跟随尾部：用户上滚即暂停，点「回到底部」恢复
let kbLogTimer: number | null = null
const kbLogBox = ref<HTMLElement | null>(null)

/** 进度百分比：LLM 段用 llm_idx/llm_total，抓取段用 idx/total（idx 为正在处理帖下标） */
const kbLogPct = computed(() => {
  const p = kbLogProgress.value
  if (!p) return 0
  const done = p.llm_total ? (p.llm_idx ?? 0) : p.idx + 1
  const total = p.llm_total || p.total
  return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
})
const kbLogStageText = computed(() => {
  const p = kbLogProgress.value
  if (!p) return ''
  return p.llm_total
    ? `${p.stage} · LLM ${p.llm_idx ?? 0}/${p.llm_total}`
    : `${p.stage} · 第 ${p.idx + 1}/${p.total} 帖`
})

async function pollKbLogs() {
  if (document.hidden) return
  try {
    const r = await api.kbLogs(kbLogLastSeq.value)
    kbLogError.value = ''
    kbLogRunning.value = r.running
    kbLogProgress.value = r.progress
    if (r.lines.length) {
      kbLogLines.value.push(...r.lines)
      // 与后端环形缓冲同限幅：只保留最近 800 行
      if (kbLogLines.value.length > 800) kbLogLines.value.splice(0, kbLogLines.value.length - 800)
      kbLogLastSeq.value = r.last_seq
      await nextTick()
      if (kbLogFollow.value) scrollKbLogBottom()
    }
  } catch {
    // 轮询失败可见但不打断（下一 tick 自动重试）：单机接口，失败多为瞬时
    kbLogError.value = '日志刷新失败，将自动重试'
  }
}

function scrollKbLogBottom() {
  const box = kbLogBox.value
  if (box) box.scrollTop = box.scrollHeight
}

function onKbLogScroll() {
  const box = kbLogBox.value
  if (!box) return
  // 距底部 < 40px 视为在底部 → 跟随；上滚即暂停跟随（业界日志终端惯例）
  kbLogFollow.value = box.scrollHeight - box.scrollTop - box.clientHeight < 40
}

function startKbLogPolling() {
  if (kbLogTimer !== null) return
  void pollKbLogs()
  kbLogTimer = window.setInterval(() => void pollKbLogs(), KB_LOG_POLL_MS)
}
function stopKbLogPolling() {
  if (kbLogTimer !== null) {
    window.clearInterval(kbLogTimer)
    kbLogTimer = null
  }
}
function openKbLog() {
  kbLogOpen.value = true
  startKbLogPolling()
}
watch(kbLogOpen, (open) => {
  if (!open) stopKbLogPolling()
})
function jumpKbLogBottom() {
  kbLogFollow.value = true
  scrollKbLogBottom()
}

/** 逐条自动保存：改一项即立即写后端并生效（去掉了「保存设置」批提交）。
 *  单人本机场景无需「未保存草稿」概念：任何改动立刻落盘 + 推进运行态（后端 update 内部 apply_runtime）。
 *  单键保存与后端白名单单键口径一致；失败仅提示该行，不阻断其它项编辑。 */
async function saveOne(it: SettingItem, value: number | boolean | string[] | string) {
  savingMap.value = { ...savingMap.value, [it.key]: true }
  try {
    const r = await api.saveSettings({ [it.key]: value })
    const updated = r.settings.find((s) => s.key === it.key)
    if (updated) {
      const idx = items.value.findIndex((x) => x.key === it.key)
      if (idx >= 0) items.value[idx] = updated
    }
    // frontend 作用域：自动刷新开关需同步全局状态（轮播等前端参数随页面加载生效）
    if (it.key === 'enable_auto_refresh') {
      dash.setEnableAutoRefresh(Boolean(value))
    }
    // 抓取/沉淀/知识库相关参数改动会改变「下次执行」：重取调度状态，避免页面显示与实际调度口径不一致
    if (
      it.key.startsWith('scrape_schedule_') ||
      it.key.startsWith('precipitate_') ||
      it.key.startsWith('kb_export_')
    ) {
      void loadSchedule()
    }
    lastSavedAt.value = Date.now()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`保存「${it.label}」失败: ${(e as Error).message}`)
  } finally {
    const next = { ...savingMap.value }
    delete next[it.key]
    savingMap.value = next
  }
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const cfg = await api.config()
    items.value = cfg.settings ?? []
  } catch (e) {
    if (isAborted(e)) return
    loadError.value = (e as Error).message
    ElMessage.error(`加载设置失败: ${loadError.value}`)
  } finally {
    loading.value = false
  }
}

function setNumber(it: SettingItem, v: number | null) {
  if (v === null || Number.isNaN(v)) return
  void saveOne(it, v)
}

function setBool(it: SettingItem, v: boolean | string | number) {
  void saveOne(it, Boolean(v))
}
function setText(it: SettingItem, v: string) {
  void saveOne(it, v)
}

/** LLM 三项联动（修订 37）：后端规格（base_url/models/key_env）随快照 providers 下发，
 *  此处不复制任何字面量；切后端 = 一次 PUT 同时回填该后端的接口地址与模型名默认值（业界同：
 *  切档即带出该档默认三元组，用户可再手改），避免「切了后端还留着上一档的地址/模型」混档 */
const llmProviders = computed(() => byKey('llm_provider')?.providers ?? null)
const llmProvider = computed(() => String(byKey('llm_provider')?.value ?? ''))
const llmSpec = computed(() => llmProviders.value?.[llmProvider.value] ?? null)
const llmModelOptions = computed(() => llmSpec.value?.models ?? [])
const llmDefaultBase = computed(() => llmSpec.value?.base_url ?? '')
const llmDefaultModel = computed(() => llmSpec.value?.models[0] ?? '')
const llmKeyEnv = computed(() => llmSpec.value?.key_env ?? '')

async function setLlmProvider(v: string) {
  const spec = llmProviders.value?.[v]
  const payload: Record<string, string> = { llm_provider: v }
  if (spec) {
    payload.llm_base_url = spec.base_url
    payload.llm_model = spec.models[0] ?? ''
  }
  savingMap.value = { ...savingMap.value, llm_provider: true }
  try {
    const r = await api.saveSettings(payload)
    // 三个键的回显一并刷新（响应快照含保存后的最新值，含回填的地址/模型）
    const llmKeys = ['llm_provider', 'llm_model', 'llm_base_url']
    for (const s of r.settings) {
      if (llmKeys.includes(s.key)) {
        const idx = items.value.findIndex((x) => x.key === s.key)
        if (idx >= 0) items.value[idx] = s
      }
    }
    ElMessage.success('已切换 LLM 后端，接口地址与模型名已回填该后端默认值')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`保存「LLM 后端」失败: ${(e as Error).message}`)
  } finally {
    const next = { ...savingMap.value }
    delete next.llm_provider
    savingMap.value = next
  }
}

async function resetOne(it: SettingItem) {
  // 与 resetAll 同款写法：取消必须 return。此前用 .catch(() => false) 吞掉取消后
  // 未拦截，继续执行 doReset——用户点「取消」也会恢复默认，确认弹窗形同虚设。
  const ok = await ElMessageBox.confirm(
    `将「${it.label}」恢复为默认值 ${String(it.default)}，确定吗？`,
    '恢复默认',
    { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' },
  ).then(
    () => true,
    () => false,
  )
  if (!ok) return
  await doReset([it.key])
}

async function resetAll() {
  const ok = await ElMessageBox.confirm(
    '将全部参数恢复为默认值（环境变量/代码默认值），确定吗？',
    '全部恢复默认',
    { type: 'warning', confirmButtonText: '全部恢复', cancelButtonText: '取消' },
  ).then(
    () => true,
    () => false,
  )
  if (!ok) return
  await doReset([])
}

async function doReset(keys: string[]) {
  try {
    const r = await api.resetSettings(keys)
    items.value = r.settings
    const auto = r.settings.find((s) => s.key === 'enable_auto_refresh')
    if (auto) dash.setEnableAutoRefresh(Boolean(auto.value))
    lastSavedAt.value = Date.now()
    ElMessage.success(keys.length ? '已恢复默认' : '已恢复全部默认')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`恢复失败: ${(e as Error).message}`)
  }
}

// ===== 链接黑名单（大屏卡片口径过滤）=====
const blItems = ref<BlacklistItem[]>([])
const blLoading = ref(false)
const blType = ref<'url' | 'author' | 'fid'>('url')
const blValue = ref('')
const blReason = ref('')

const BL_TYPE_LABEL: Record<BlacklistItem['type'], string> = {
  url: '链接',
  author: '作者',
  fid: '版块',
}

/** 黑名单条目一多，设置页会被拉成一条望不到头的长列表：按业界「管理列表」通行做法补
 *  **搜索 + 分页 + 计数**（Element Plus 后台表格、Ant Design Table、GitHub 的 blocked users
 *  都是这三件套）。为什么是前端切片分页：条目已随接口全量返回、量级在几十条（本项目实测
 *  17 条），与下载中心/资源管理页的分页口径一致，不需要为它加服务端分页参数；
 *  不选「无限滚动/加载更多」——管理场景需要「总共多少条、在哪一页」的确定性，
 *  也不选纯滚动容器——那只是把长列表换个地方滚动，仍无法快速定位。 */
const BL_PAGE_SIZE = 5
const blFilter = ref('')
const blPage = ref(1)
const blFiltered = computed(() => {
  const q = blFilter.value.trim().toLowerCase()
  if (!q) return blItems.value
  return blItems.value.filter((it) =>
    // 备注与类型名也可搜（用户常按「为什么拉黑」的备注找）
    [it.value, it.reason, BL_TYPE_LABEL[it.type]].some((s) => (s ?? '').toLowerCase().includes(q)),
  )
})
const blPageCount = computed(() => Math.max(1, Math.ceil(blFiltered.value.length / BL_PAGE_SIZE)))
const blPaged = computed(() =>
  blFiltered.value.slice((blPage.value - 1) * BL_PAGE_SIZE, blPage.value * BL_PAGE_SIZE),
)
// 筛选条件变化：回到第一页（否则会停在超出范围的页码上）
watch(blFilter, () => {
  blPage.value = 1
})
// 条目减少 / 筛选变窄导致页数变少：把页码收敛到最后一页（删除末页最后一条的典型场景）
watch(blPageCount, (n) => {
  if (blPage.value > n) blPage.value = n
})

async function loadBlacklist() {
  blLoading.value = true
  try {
    const r = await api.blacklist()
    blItems.value = r.items
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`加载黑名单失败: ${(e as Error).message}`)
  } finally {
    blLoading.value = false
  }
}

async function addBlacklistItem() {
  const value = blValue.value.trim()
  if (!value) {
    ElMessage.warning('请填写链接 / 作者 / 版块标识')
    return
  }
  try {
    await api.addBlacklist(blType.value, value, blReason.value.trim())
    blValue.value = ''
    blReason.value = ''
    ElMessage.success('已加入黑名单，大屏各卡片口径同步更新')
    await loadBlacklist()
    dash.bumpBlacklist()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`添加失败: ${(e as Error).message}`)
  }
}

async function removeBlacklistItem(it: BlacklistItem) {
  try {
    await api.removeBlacklist(it.type, it.value)
    ElMessage.success('已移除')
    await loadBlacklist()
    dash.bumpBlacklist()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`移除失败: ${(e as Error).message}`)
  }
}

onMounted(() => {
  void load()
  void loadSchedule()
  void loadBlacklist()
  // 调度状态需要自己刷新：批次一跑就是几十分钟，页面停留期间要能看到「运行中 → 结束」
  startSchedPolling()
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  stopSchedPolling()
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <div class="settings-layout">
    <!-- 左侧分类导航（macOS/Windows 设置风格：左分类、右面板；窄屏自动转为顶部横向滚动） -->
    <nav class="settings-nav">
      <button
        v-for="(g, i) in GROUPS"
        :key="g.title"
        type="button"
        class="nav-item"
        :class="{ active: activeNav === i }"
        @click="activeNav = i"
      >{{ g.title }}</button>
      <button
        type="button"
        class="nav-item"
        :class="{ active: activeNav === 'blacklist' }"
        @click="activeNav = 'blacklist'"
      >链接黑名单</button>
    </nav>

    <div class="settings-panel" v-loading="loading">
      <el-result v-if="loadError" icon="error" title="设置加载失败" :sub-title="loadError">
        <template #extra>
          <el-button type="primary" :loading="loading" @click="load">重试</el-button>
        </template>
      </el-result>

      <template v-else>
        <!-- 说明条：明确配置层级与生效口径，避免用户误以为改完会重启服务 -->
        <div class="page-card tip-card">
          <div class="tip-title">设置说明</div>
          <ul class="tip-list">
            <li>配置层级：<b>页内设置</b> &gt; 环境变量 &gt; 代码默认值；仅下列参数支持页内调整。</li>
            <li>
              端口、数据目录、域名等<b>部署配置不在此处</b>（与环境/部署相关，修改需改
              <code>.env</code> 并重启）。
            </li>
            <li>每项标注了生效范围；超出范围的值会自动收敛到允许区间。</li>
            <li><b>改动逐条自动保存</b>：改任意一项即刻写盘生效，无需点「保存设置」。</li>
          </ul>
        </div>

        <div
          v-for="(g, i) in GROUPS"
          v-show="activeNav === i"
          :key="g.title"
          class="page-card"
        >
          <div class="group-title">{{ g.title }}</div>
          <div v-if="g.desc" class="group-desc text-muted">{{ g.desc }}</div>
          <!-- 定时抓取：状态区（只读，来自 /api/schedule，与真实触发判定同源）放在配置项之前，
               用户先看到「下次什么时候跑、上次为什么没跑」，再决定怎么改参数 -->
          <!-- 抓取调度状态（只读，来自 /api/schedule.scrape，与真实触发判定同源） -->
          <div v-if="g.extra === 'schedule'" class="sched-status">
            <template v-if="sched && sched.scrape">
              <div class="ss-row">
                <span class="ss-label">当前状态</span>
                <span class="ss-value">
                  <template v-if="schedRunning">
                    <el-tag size="small" type="primary">{{ schedRunningText }}</el-tag>
                    <el-button link type="primary" size="small" @click="router.push('/runs')">
                      查看进度
                    </el-button>
                  </template>
                  <span v-else class="text-muted">空闲（没有正在运行的抓取批次）</span>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">下次执行</span>
                <span class="ss-value">{{ sched.scrape.next_run_at ?? '未启用（无计划时刻）' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">今日已处理</span>
                <span class="ss-value">{{ sched.scrape.today_done.length ? sched.scrape.today_done.join('、') : '—' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">上次结果</span>
                <span class="ss-value">
                  <template v-if="sched.scrape.last">
                    <el-tag size="small" :type="SCHED_ACTION[sched.scrape.last.action].type">
                      {{ SCHED_ACTION[sched.scrape.last.action].text }}
                    </el-tag>
                    <span class="text-muted">{{ sched.scrape.last.at }} · {{ sched.scrape.last.reason }}</span>
                  </template>
                  <span v-else class="text-muted">暂无调度记录</span>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">调度线程</span>
                <span
                  class="ss-value"
                  :class="tickStale(sched.scrape) ? '' : 'text-muted'"
                  :style="tickStale(sched.scrape) ? { color: 'var(--el-color-danger)', fontWeight: '600' } : {}"
                >
                  {{
                    sched.scrape.last_tick
                      ? `最近判定 ${sched.scrape.last_tick}（每 ${sched.scrape.tick_seconds} 秒一次）`
                      : '尚未运行'
                  }}{{ tickStale(sched.scrape) ? ' · 心跳停滞，调度可能已停止' : '' }}
                </span>
              </div>
            </template>
            <div v-else class="text-muted">状态读取失败，稍后自动重试</div>
            <div class="ss-actions">
              <el-tooltip :content="runNowTip" placement="top">
                <span class="tip-wrap">
                  <el-button
                    size="small"
                    :loading="runNowLoading"
                    :disabled="runNowDisabled"
                    @click="runNow"
                  >
                    立即运行一次
                  </el-button>
                </span>
              </el-tooltip>
              <span class="ss-hint text-muted">与「运行记录」页的「启动抓取」同一入口，用于验证上面的参数</span>
            </div>
          </div>

          <!-- 自动下载调度状态（只读，来自 /api/schedule.precipitate） -->
          <div v-else-if="g.extra === 'precipitate'" class="sched-status">
            <template v-if="sched && sched.precipitate">
              <div class="ss-row">
                <span class="ss-label">状态</span>
                <span class="ss-value">
                  <el-tag size="small" :type="sched.precipitate.enabled ? 'success' : 'info'">
                    {{ sched.precipitate.enabled ? '已启用' : '未启用' }}
                  </el-tag>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">下次执行</span>
                <span class="ss-value">{{ sched.precipitate.next_run_at ?? '未启用（无计划时刻）' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">今日已处理</span>
                <span class="ss-value">{{ sched.precipitate.today_done.length ? sched.precipitate.today_done.join('、') : '—' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">上次结果</span>
                <span class="ss-value">
                  <template v-if="sched.precipitate.last">
                    <span
                      :style="precipitateDiskLow ? { color: 'var(--el-color-danger)', fontWeight: '600' } : {}"
                    >{{ sched.precipitate.last.at }} · {{ sched.precipitate.last.reason }}</span>
                  </template>
                  <span v-else class="text-muted">暂无自动下载记录</span>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">调度线程</span>
                <span
                  class="ss-value"
                  :class="tickStale(sched.precipitate) ? '' : 'text-muted'"
                  :style="tickStale(sched.precipitate) ? { color: 'var(--el-color-danger)', fontWeight: '600' } : {}"
                >
                  {{ sched.precipitate.last_tick ? `最近判定 ${sched.precipitate.last_tick}` : '尚未运行' }}
                  {{ tickStale(sched.precipitate) ? '· 心跳停滞，调度可能已停止' : '' }}
                </span>
              </div>
              <el-alert
                v-if="precipitateDiskLow"
                type="warning"
                :closable="false"
                show-icon
                title="磁盘可用空间不足，已停止自动下载"
                :description="sched.precipitate.last?.reason"
                class="ss-alert"
              />
            </template>
            <div v-else class="text-muted">状态读取失败，稍后自动重试</div>
            <div class="ss-actions">
              <el-tooltip content="立即按当前筛选条件自动下载一次（不受定时时刻限制），用于验证参数" placement="top">
                <span class="tip-wrap">
                  <el-button
                    size="small"
                    :loading="autoDownloadNowLoading"
                    @click="autoDownloadNow"
                  >
                    立即自动下载
                  </el-button>
                </span>
              </el-tooltip>
              <span class="ss-hint text-muted">与定时自动下载同一筛选与磁盘守卫；触发即提交到下载中心</span>
            </div>
          </div>

          <!-- 知识库增量调度状态（只读，来自 /api/schedule.kb；一期方案 §3.7-5 两级口径） -->
          <div v-else-if="g.extra === 'kbschedule'" class="sched-status">
            <template v-if="sched && sched.kb">
              <div class="ss-row">
                <span class="ss-label">状态</span>
                <span class="ss-value">
                  <el-tag size="small" :type="sched.kb.running ? 'primary' : sched.kb.enabled ? 'success' : 'info'">
                    {{ sched.kb.running ? '批次执行中' : sched.kb.enabled ? '已启用' : '未启用' }}
                  </el-tag>
                </span>
              </div>
              <el-alert
                v-if="!sched.kb.initial_done"
                type="info"
                :closable="false"
                show-icon
                title="尚未执行过全库首建，当前仅按下方筛选条件做增量沉淀"
                description="全库首建为按需手动操作（kb_export.py 按自然月分片），见 docs/知识库一期操作手册.md"
                class="ss-alert"
              />
              <el-alert
                v-else-if="sched.kb.recovery_mode"
                type="warning"
                :closable="false"
                show-icon
                title="kb_state 处于恢复重建状态：--reconvert 不可用"
                description="生成 hash 不可重建，首轮覆盖须用 --force（重抓 + 重转）"
                class="ss-alert"
              />
              <div class="ss-row">
                <span class="ss-label">下次执行</span>
                <span class="ss-value">{{ sched.kb.next_run_at ?? '未启用（无计划时刻）' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">今日已处理</span>
                <span class="ss-value">{{ sched.kb.today_done.length ? sched.kb.today_done.join('、') : '—' }}</span>
              </div>
              <div class="ss-row">
                <span class="ss-label">上次结果</span>
                <span class="ss-value">
                  <template v-if="sched.kb.last">
                    <el-tag size="small" :type="kbActionTag(sched.kb.last.action).type">
                      {{ kbActionTag(sched.kb.last.action).text }}
                    </el-tag>
                    <span class="text-muted">{{ sched.kb.last.at }} · {{ sched.kb.last.reason }}</span>
                  </template>
                  <span v-else class="text-muted">暂无增量记录</span>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">进度与积压</span>
                <span class="ss-value">
                  已沉淀 {{ sched.kb.done }} 帖
                  <template v-if="sched.kb.backlog != null"> · 待处理积压 {{ sched.kb.backlog }} 帖</template>
                  <template v-if="sched.kb.llm_backlog > 0"> · LLM 段积压 {{ sched.kb.llm_backlog }}</template>
                  <template v-if="sched.kb.permanent > 0"> · 永久失败 {{ sched.kb.permanent }}</template>
                </span>
              </div>
              <div class="ss-row">
                <span class="ss-label">调度线程</span>
                <span
                  class="ss-value"
                  :class="tickStale(sched.kb) ? '' : 'text-muted'"
                  :style="tickStale(sched.kb) ? { color: 'var(--el-color-danger)', fontWeight: '600' } : {}"
                >
                  {{ sched.kb.last_tick ? `最近判定 ${sched.kb.last_tick}（每 ${sched.kb.tick_seconds} 秒一次）` : '尚未运行' }}
                  {{ tickStale(sched.kb) ? '· 心跳停滞，调度可能已停止' : '' }}
                </span>
              </div>
            </template>
            <div v-else class="text-muted">状态读取失败，稍后自动重试</div>
            <div class="ss-actions">
              <el-tooltip :content="kbRunTip" placement="top">
                <span class="tip-wrap">
                  <el-button
                    size="small"
                    :loading="kbRunningNow"
                    :disabled="kbRunDisabled"
                    @click="kbRunNow"
                  >
                    {{ kbRunningNow ? '执行中…' : '立即执行增量' }}
                  </el-button>
                </span>
              </el-tooltip>
              <el-button size="small" @click="openKbLog">执行日志</el-button>
              <span class="ss-hint text-muted">后台线程执行；运行中按钮禁用，重复点击会被拒绝，实时进度见「执行日志」</span>
            </div>
          </div>
          <div class="setting-list">
            <div
              v-for="it in groupItems(g.keys)"
              :key="it.key"
              class="setting-row"
              :class="{ 'row-wide': WIDE_TYPES.includes(it.type) }"
            >
              <div class="sr-main">
                <div class="sr-label">
                  {{ it.label }}
                  <el-tag v-if="it.source === 'file'" size="small" type="warning">已自定义</el-tag>
                  <el-tag v-else size="small" type="info">默认</el-tag>
                </div>
                <div class="sr-desc text-muted">{{ it.desc }}</div>
                <div class="sr-scope">生效范围：{{ SCOPE_TEXT[it.scope] }}</div>
              </div>
              <div class="sr-ctrl">
                <el-switch
                  v-if="it.type === 'bool'"
                  :model-value="Boolean(valueOf(it))"
                  active-text="开"
                  inactive-text="关"
                  @change="(v: boolean | string | number) => setBool(it, v)"
                />
                <div v-else-if="it.type === 'array'" class="sr-array">
                  <div v-for="opt in (it.options ?? [])" :key="opt.value" class="ar-row">
                    <el-checkbox
                      :model-value="isIncluded(it, opt.value)"
                      @change="() => toggleSection(it, opt.value)"
                    >{{ opt.label }}</el-checkbox>
                    <span v-if="isIncluded(it, opt.value)" class="ar-move">
                      <el-button
                        link
                        type="primary"
                        size="small"
                        :disabled="arrayValue(it)[0] === opt.value"
                        @click="moveSection(it, opt.value, -1)"
                      >上移</el-button>
                      <el-button
                        link
                        type="primary"
                        size="small"
                        :disabled="arrayValue(it)[arrayValue(it).length - 1] === opt.value"
                        @click="moveSection(it, opt.value, 1)"
                      >下移</el-button>
                    </span>
                  </div>
                  <div class="sr-default text-muted">默认：{{ arrayLabels(it, it.default as string[]) }}</div>
                </div>
                <!-- LLM 后端：切换即联动回填接口地址与模型名默认值（一次 PUT，见 setLlmProvider） -->
                <template v-else-if="it.key === 'llm_provider'">
                  <el-select
                    :model-value="String(valueOf(it))"
                    :size="isMobile ? 'small' : 'default'"
                    class="sr-input"
                    @change="(v: string) => setLlmProvider(v)"
                  >
                    <el-option
                      v-for="opt in (it.options ?? [])"
                      :key="opt.value"
                      :label="opt.label"
                      :value="opt.value"
                    />
                  </el-select>
                  <div class="sr-default text-muted">
                    默认：{{ optionLabel(it, String(it.default)) }}<template v-if="llmKeyEnv"> · 密钥取环境变量 {{ llmKeyEnv }}</template><template v-else> · 本地服务无需密钥</template>
                  </div>
                </template>
                <template v-else-if="it.type === 'enum'">
                  <el-select
                    :model-value="String(valueOf(it))"
                    :size="isMobile ? 'small' : 'default'"
                    class="sr-input"
                    @change="(v: string) => setText(it, v)"
                  >
                    <el-option
                      v-for="opt in (it.options ?? [])"
                      :key="opt.value"
                      :label="opt.label"
                      :value="opt.value"
                    />
                  </el-select>
                  <div class="sr-default text-muted">默认：{{ optionLabel(it, String(it.default)) }}</div>
                </template>
                <div v-else-if="it.type === 'times'" class="sr-times">
                  <div v-for="(t, i) in timeList(it)" :key="`${it.key}-${i}`" class="st-row">
                    <!-- 已占用时刻置灰的下拉（唯一性从源头阻断）；filterable 支持输入过滤定位 -->
                    <el-select
                      :model-value="t"
                      filterable
                      :size="isMobile ? 'small' : 'default'"
                      placeholder="选择时刻"
                      class="st-picker"
                      @change="(v: string) => setTimeAt(it, i, v)"
                    >
                      <el-option
                        v-for="opt in TIME_OPTIONS"
                        :key="opt"
                        :label="opt"
                        :value="opt"
                        :disabled="isTimeTaken(it, opt, i)"
                      />
                    </el-select>
                    <el-button link type="danger" size="small" @click="removeTime(it, i)">删除</el-button>
                  </div>
                  <div class="st-actions">
                    <el-button
                      link
                      type="primary"
                      size="small"
                      :disabled="timesAtMax(it)"
                      @click="addTime(it)"
                    >添加时刻</el-button>
                    <!-- 达上限露出原因（带分母），按钮不消失、悬停可感知不可用 -->
                    <span v-if="timesAtMax(it)" class="text-muted sr-default">已达上限 {{ it.max }} 个</span>
                    <span class="sr-default">默认：{{ (it.default as string[]).join('、') }}</span>
                  </div>
                </div>
                <!-- 访问链：占位新行（https://）不合法不提交，补全 host 失焦（change）才保存；
                     输入过程（input）只写本地缓冲，避免每个按键都触发一次 400 -->
                <div v-else-if="it.type === 'chain'" class="sr-times">
                  <div v-for="(u, i) in chainRows(it)" :key="`${it.key}-${i}`" class="st-row chain-row">
                    <el-tag
                      size="small"
                      :type="i === chainRows(it).length - 1 ? 'success' : 'info'"
                      class="chain-tag"
                    >{{ i === chainRows(it).length - 1 ? '主域' : `镜像${i + 1}` }}</el-tag>
                    <el-input
                      :model-value="u"
                      :size="isMobile ? 'small' : 'default'"
                      placeholder="http(s)://host（末项须公网可直达）"
                      class="chain-input"
                      @update:model-value="(v: string) => bufferChain(it, i, v)"
                      @change="(v: string) => editChainAt(it, i, v)"
                    />
                    <el-button link type="primary" size="small" :disabled="i === 0" @click="moveChainRow(it, i, -1)">上移</el-button>
                    <el-button link type="primary" size="small" :disabled="i === chainRows(it).length - 1" @click="moveChainRow(it, i, 1)">下移</el-button>
                    <el-button link type="danger" size="small" :disabled="chainRows(it).length <= 1" @click="removeChainRow(it, i)">删除</el-button>
                  </div>
                  <div class="st-actions">
                    <el-button link type="primary" size="small" @click="addChainRow(it)">添加端点</el-button>
                    <span class="sr-default text-muted">默认：{{ (it.default as string[]).join(' → ') }}</span>
                  </div>
                </div>
                <!-- LLM 模型名：可选当前后端预置模型，也允许直接输入任意模型名（ollama 本地档无预置） -->
                <template v-else-if="it.key === 'llm_model'">
                  <el-select
                    :model-value="String(valueOf(it))"
                    filterable
                    allow-create
                    default-first-option
                    :size="isMobile ? 'small' : 'default'"
                    placeholder="选择或输入模型名"
                    class="sr-input"
                    @change="(v: string) => setText(it, v)"
                  >
                    <el-option v-for="m in llmModelOptions" :key="m" :label="m" :value="m" />
                  </el-select>
                  <div class="sr-default text-muted">
                    默认：{{ llmDefaultModel || '未配置（LLM 段跳过）' }}
                  </div>
                </template>
                <!-- LLM 接口地址：占位与「默认」行跟随当前后端的官方默认（随快照 providers 下发） -->
                <template v-else-if="it.key === 'llm_base_url'">
                  <el-input
                    :model-value="String(valueOf(it))"
                    clearable
                    :placeholder="llmDefaultBase ? `留空自动：${llmDefaultBase}` : 'https://…（自定义后端必填）'"
                    class="sr-input"
                    @input="(v: string) => (it.value = v)"
                    @change="(v: string) => setText(it, v)"
                  />
                  <div class="sr-default text-muted">
                    默认：{{ llmDefaultBase || '自动（跟随所选后端）' }}
                  </div>
                </template>
                <template v-else-if="it.type === 'text'">
                  <el-input
                    :model-value="String(valueOf(it))"
                    clearable
                    placeholder="留空则自动取访问地址"
                    class="sr-input"
                    @input="(v: string) => (it.value = v)"
                    @change="(v: string) => setText(it, v)"
                  />
                  <div class="sr-default text-muted">
                    默认：{{ it.defaultText ?? (it.default ? String(it.default) : '自动（取访问地址）') }}
                  </div>
                </template>
                <template v-else>
                  <el-input-number
                    :model-value="Number(valueOf(it))"
                    :min="it.min ?? undefined"
                    :max="it.max ?? undefined"
                    :step="1"
                    :size="isMobile ? 'small' : 'default'"
                    controls-position="right"
                    class="sr-input"
                    @change="(v: number | null) => setNumber(it, v)"
                  />
                  <div class="sr-default text-muted">默认 {{ String(it.default) }}</div>
                </template>
                <span v-if="savingMap[it.key]" class="sr-saving text-muted">保存中…</span>
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="savingMap[it.key]"
                  @click="resetOne(it)"
                >恢复默认</el-button>
              </div>
            </div>
          </div>
        </div>

        <!-- 链接黑名单：作用于大屏各卡片口径（累计/近7日/近30日/榜单/推荐），不影响帖子页与下载 -->
        <div v-show="activeNav === 'blacklist'" class="page-card">
          <div class="group-title">链接黑名单（大屏卡片口径过滤）</div>
          <div class="group-desc text-muted">
            加入后，对应链接 / 作者 / 版块的全部帖子将从数据总览所有卡片中剔除（含已收录统计、趋势、榜单、待下载推荐）。
            仅影响大屏看板，帖子浏览页与下载中心不受影响，可随时移除恢复。
          </div>
          <div class="bl-add">
            <el-select v-model="blType" size="small" class="bl-type" style="width: 120px">
              <el-option label="链接" value="url" />
              <el-option label="作者" value="author" />
              <el-option label="版块" value="fid" />
            </el-select>
            <el-input
              v-model="blValue"
              size="small"
              class="bl-value"
              :placeholder="blType === 'url' ? '帖子链接（如 /htm_data/.../x.html）' : blType === 'author' ? '作者名' : '版块 fid（如 5）'"
            />
            <el-input v-model="blReason" size="small" class="bl-reason" placeholder="备注（可选）" />
            <el-button type="primary" size="small" :loading="blLoading" @click="addBlacklistItem">
              加入黑名单
            </el-button>
          </div>
          <!-- 搜索 + 计数：条目一多，先缩小范围再翻页（业界管理列表的固定搭配） -->
          <div class="bl-toolbar">
            <el-input
              v-model="blFilter"
              size="small"
              clearable
              class="bl-search"
              placeholder="搜索链接 / 作者 / 版块 / 备注"
            />
            <span class="text-muted bl-count">
              共 {{ blItems.length }} 条<template v-if="blFilter">，筛选出 {{ blFiltered.length }} 条</template>
            </span>
          </div>
          <div v-loading="blLoading" class="bl-list">
            <div v-if="!blFiltered.length" class="text-muted bl-empty">
              {{ blItems.length ? '没有匹配的黑名单条目' : '暂无黑名单' }}
            </div>
            <div v-for="it in blPaged" :key="it.type + '|' + it.value" class="bl-row">
              <el-tag
                size="small"
                :type="it.type === 'url' ? 'danger' : it.type === 'author' ? 'warning' : 'info'"
              >
                {{ BL_TYPE_LABEL[it.type] }}
              </el-tag>
              <span class="bl-value-text" :title="it.value">{{ it.value }}</span>
              <span v-if="it.reason" class="bl-reason-text text-muted">{{ it.reason }}</span>
              <el-button link type="danger" size="small" @click="removeBlacklistItem(it)">移除</el-button>
            </div>
          </div>
          <!-- 只有一页时不出分页器（避免"共 7 条 / 1 页"占位） -->
          <div v-if="blPageCount > 1" class="bl-pager">
            <el-pagination
              :current-page="blPage"
              :page-size="BL_PAGE_SIZE"
              :total="blFiltered.length"
              :layout="isMobile ? 'prev, pager, next' : 'total, prev, pager, next'"
              :pager-count="isMobile ? 5 : 7"
              small
              background
              @current-change="(p: number) => (blPage = p)"
            />
          </div>
        </div>

        <!-- 操作条：改动逐条自动保存，无需「保存设置」；仅保留「全部恢复默认」 -->
        <div class="page-card actions">
          <span class="text-muted auto-saved">
            <span class="as-dot" :class="{ on: lastSavedAt }"></span>
            {{ lastSavedText }}
          </span>
          <div class="actions-right">
            <el-button @click="resetAll">全部恢复默认</el-button>
          </div>
        </div>
      </template>
    </div>
  </div>

  <!-- 知识库批次执行日志抽屉：after 游标增量轮询 + 自动跟随尾部（上滚暂停）。
       日志内容 = kb_export 进程内环形缓冲，与文件日志 / web_*.log 同源同前缀 -->
  <el-drawer v-model="kbLogOpen" title="知识库批次执行日志" :size="isMobile ? '100%' : '560px'" :append-to-body="true">
    <div class="kb-log-head">
      <el-tag size="small" :type="kbLogRunning ? 'primary' : 'info'">
        {{ kbLogRunning ? '批次执行中' : '空闲' }}
      </el-tag>
      <template v-if="kbLogProgress">
        <span class="text-muted">{{ kbLogStageText }}</span>
        <el-progress :percentage="kbLogPct" :stroke-width="8" class="kb-log-bar" />
      </template>
    </div>
    <div ref="kbLogBox" class="kb-log-box" @scroll.passive="onKbLogScroll">
      <div v-if="!kbLogLines.length" class="text-muted kb-log-empty">
        暂无日志：批次启动后此处实时输出逐帖进度（保留最近 800 行）
      </div>
      <div v-for="l in kbLogLines" :key="l.seq" class="kb-log-line">{{ l.text }}</div>
    </div>
    <div class="kb-log-foot text-muted">
      <span v-if="!kbLogFollow" class="kb-log-jump" @click="jumpKbLogBottom">↓ 回到底部（已暂停跟随）</span>
      <span v-else-if="kbLogError">{{ kbLogError }}</span>
      <span v-else>自动跟随最新输出{{ kbLogRunning ? '' : '（空闲时展示最近一次批次日志）' }}</span>
    </div>
  </el-drawer>
</template>

<style scoped>
.tip-card {
  margin-bottom: 12px;
}

.tip-title {
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 6px;
}

.tip-list {
  margin: 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.8;
}

.group-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 2px;
}

.group-desc {
  font-size: 12px;
  margin-bottom: 10px;
}

/* 行布局：桌面「信息 + 控件」左右分栏；移动端自动换行堆叠 */
.setting-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 0;
  border-top: 1px solid var(--app-border);
}

.setting-row:first-child {
  border-top: none;
}

.sr-main {
  min-width: 0;
  flex: 1;
}

.sr-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 500;
}

.sr-desc {
  font-size: 12px;
  margin-top: 2px;
}

.sr-scope {
  font-size: 12px;
  color: #909399;
  margin-top: 2px;
}

.sr-ctrl {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
}

.sr-input {
  width: 260px;
}

.sr-array {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 280px;
}

.ar-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ar-move {
  display: flex;
  gap: 4px;
}

.sr-default {
  font-size: 12px;
}

/* 宽控件行（array 多选、times 时刻列表）：「标签｜控件」左右分栏会把标签挤成窄条
   （实测「演示轮播板块序列」控件固有宽度 1002px、标签只剩 132px，描述被压成竖条），
   故这类行改为「标签在上、控件在下」的堆叠布局（Element Plus / Ant Design 表单对
   复杂控件也是独占一行）。紧凑控件（开关 / 数字 / 下拉）仍保持左右分栏。 */
.setting-row.row-wide {
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
}

.row-wide .sr-ctrl {
  width: 100%;
  justify-content: flex-start;
  flex-wrap: wrap;
}

/* 宽控件独占一行；「恢复默认」被挤到下一行时仍贴右，与紧凑行的位置一致 */
.row-wide .sr-array,
.row-wide .sr-times {
  flex: 1 1 100%;
}

.row-wide .sr-ctrl > .el-button {
  margin-left: auto;
}

/* 定时抓取：状态区（只读，与调度同源；虚线框与「新建下载任务」提交区同一视觉语言） */
.sched-status {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  margin-bottom: 12px;
  border: 1px dashed var(--app-border, #dcdfe6);
  border-radius: 8px;
}

.ss-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  flex-wrap: wrap;
}

.ss-label {
  min-width: 76px;
  color: #909399;
}

.ss-value {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ss-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 2px;
  flex-wrap: wrap;
}

.ss-hint {
  font-size: 12px;
}

/* 知识库执行日志抽屉：进度头部 / 日志终端 / 跟随状态栏（样式唯一来源，供抽屉复用） */
.kb-log-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  /* 文字行锁单行：阶段文案 + 进度条同行展示不换行 */
  white-space: nowrap;
  min-width: 0;
}

.kb-log-bar {
  flex: 1;
  min-width: 0;
}

.kb-log-box {
  height: calc(100% - 72px);
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-darker, #1e1e1e);
  color: var(--el-text-color-regular, #d4d4d4);
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  padding: 8px 10px;
}

.kb-log-line {
  word-break: break-all;
  /* 单行不强制 nowrap：日志含长标题，允许折行保证完整性 */
}

.kb-log-empty {
  padding: 16px 0;
  text-align: center;
  color: inherit;
  opacity: 0.7;
}

.kb-log-foot {
  margin-top: 8px;
  font-size: 12px;
  text-align: center;
}

.kb-log-jump {
  color: var(--el-color-primary);
  cursor: pointer;
  font-weight: 600;
}

/* 禁用按钮上的 tooltip 需要一层可命中的包裹元素（disabled 的 button 不派发鼠标事件） */
.tip-wrap {
  display: inline-flex;
}

/* 时刻列表（times 类型）：逐行「时刻 + 删除」，末尾「添加时刻」 */
.sr-times {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.st-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.st-picker {
  width: 120px;
}

/* 访问链行：标签固定宽、输入框吃满剩余空间（含移动端收缩） */
.chain-tag {
  flex: none;
}

.chain-input {
  flex: 1 1 260px;
}

.st-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

/* 左侧分类导航 + 右侧面板（macOS/Windows 设置风格） */
.settings-layout {
  display: flex;
  align-items: flex-start;
  gap: 16px;
}

.settings-nav {
  position: sticky;
  top: 16px;
  flex: 0 0 168px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 4px;
  border-radius: 8px;
  background: var(--app-card-bg, #fff);
  border: 1px solid var(--app-border, #ebeef5);
}

.nav-item {
  text-align: left;
  padding: 8px 12px;
  border: none;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #606266;
  transition: background 0.15s, color 0.15s;
}

.nav-item:hover {
  background: var(--el-fill-color-light);
}

.nav-item.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.settings-panel {
  flex: 1;
  min-width: 0;
}

/* 行内「保存中…」提示 */
.sr-saving {
  font-size: 12px;
}

/* 自动保存状态指示 */
.auto-saved {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}

.as-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #c0c4cc;
}

.as-dot.on {
  background: var(--el-color-success);
}

.actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 12px;
}

.actions-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.bl-add {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 10px 0;
}

/* 黑名单工具栏：搜索框 + 计数（条目多时先缩小范围再翻页） */
.bl-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 2px 0 8px;
  flex-wrap: wrap;
}

.bl-search {
  width: 280px;
}

.bl-count {
  font-size: 12px;
}

.bl-pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}

.bl-type {
  flex-shrink: 0;
}

.bl-value {
  flex: 1;
  min-width: 240px;
}

.bl-reason {
  width: 160px;
}

.bl-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
  min-height: 24px;
}

.bl-empty {
  font-size: 12px;
}

.bl-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  border-top: 1px solid var(--app-border);
  font-size: 13px;
}

.bl-value-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: monospace;
}

.bl-reason-text {
  font-size: 12px;
  flex-shrink: 0;
}

@media (max-width: 767px) {
  /* 左侧导航在窄屏转为顶部横向滚动条（避免挤占面板宽度） */
  .settings-layout {
    flex-direction: column;
  }

  .settings-nav {
    position: static;
    flex: none;
    width: 100%;
    flex-direction: row;
    overflow-x: auto;
    gap: 6px;
  }

  .nav-item {
    white-space: nowrap;
  }

  .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .sr-ctrl {
    justify-content: flex-start;
    flex-wrap: wrap;
  }

  /* 设置项输入框：移动端占满整行，避免 260px 固定宽溢出 */
  .sr-input {
    width: 100%;
  }

  /* 黑名单：搜索框独占一行，计数紧随其后 */
  .bl-search {
    width: 100%;
  }

  .bl-pager {
    justify-content: center;
  }

  .actions {
    flex-direction: column;
    align-items: stretch;
  }

  .actions-right {
    justify-content: flex-end;
  }
}
</style>
