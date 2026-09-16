<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  api,
  isAborted,
  type BlacklistItem,
  type ScheduleAction,
  type ScheduleStatus,
  type SettingItem,
} from '../api'
import { useAppStore } from '../stores/app'
import { useDashboardStore } from '../stores/dashboard'
import { legacyCopy } from '../utils/clipboard'

const app = useAppStore()
// 自动刷新总开关的实际状态在 Dashboard store（Header 与数据总览共用同一份）
const dash = useDashboardStore()
const isMobile = computed(() => app.isMobile)

const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const items = ref<SettingItem[]>([])
// 表单草稿：key -> 值（保存前不写回 items，避免未保存就改了回显）
const draft = ref<Record<string, number | boolean | string[] | string>>({})

/** 分组：与后端白名单顺序一致，按业务域切分（业界设置页通行做法）。
 *  desc 可选：组内各项已有自述时不再重复加组级说明（「内容资产」组即如此，2026-09-13 文案收敛） */
const GROUPS: { title: string; desc?: string; keys: string[]; extra?: 'schedule' }[] = [
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
const WIDE_TYPES: SettingItem['type'][] = ['array', 'times']

const SCOPE_TEXT: Record<SettingItem['scope'], string> = {
  immediate: '立即生效',
  next_task: '下一个下载任务生效（正在跑的任务不受影响）',
  frontend: '保存后前端立即应用',
}

function byKey(key: string): SettingItem | undefined {
  return items.value.find((i) => i.key === key)
}

function groupItems(keys: string[]): SettingItem[] {
  return keys.map((k) => byKey(k)).filter((i): i is SettingItem => !!i)
}

/** 草稿值（未改动时取当前生效值） */
function valueOf(it: SettingItem): number | boolean | string[] | string {
  const v = draft.value[it.key]
  return v === undefined ? it.value : v
}

/** 数组型设置项的当前草稿值（缺省回落到已生效值） */
function arrayValue(it: SettingItem): string[] {
  const v = draft.value[it.key]
  return Array.isArray(v) ? v : ((it.value as string[]) ?? [])
}
function isIncluded(it: SettingItem, key: string): boolean {
  return arrayValue(it).includes(key)
}
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
  draft.value[it.key] = next
}
function moveSection(it: SettingItem, key: string, dir: -1 | 1) {
  const cur = arrayValue(it).slice()
  const i = cur.indexOf(key)
  const j = i + dir
  if (i < 0 || j < 0 || j >= cur.length) return
  const tmp = cur[i]
  cur[i] = cur[j]
  cur[j] = tmp
  draft.value[it.key] = cur
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
const schedRunning = computed(() => sched.value?.running ?? null)
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
}

async function loadSchedule() {
  try {
    sched.value = await api.schedule()
  } catch (e) {
    if (isAborted(e)) return
    // 状态读取失败不阻断参数编辑：置空并显示提示，下次刷新重试
    sched.value = null
  }
}

/** 时刻列表（times 类型）：优先取草稿，未改动则取生效值 */
function timeList(it: SettingItem): string[] {
  const v = draft.value[it.key]
  return Array.isArray(v) ? v : ((it.value as string[]) ?? [])
}
function setTimeAt(it: SettingItem, index: number, value: string) {
  const next = timeList(it).slice()
  if (!/^\d{2}:\d{2}$/.test(value)) return
  next[index] = value
  draft.value[it.key] = next
}
/** 添加时刻：取第一个尚未占用的整点（同一天两个相同时刻没有意义，避免用户先存出重复项） */
function addTime(it: SettingItem) {
  const cur = timeList(it)
  const hours = Array.from({ length: 24 }, (_, h) => `${String(h).padStart(2, '0')}:00`)
  const pick = hours.find((t) => !cur.includes(t)) ?? '12:00'
  draft.value[it.key] = [...new Set([...cur, pick])].sort()
}
function removeTime(it: SettingItem, index: number) {
  const next = timeList(it).filter((_, i) => i !== index)
  if (!next.length) {
    // 空列表等于「没有计划时刻」：与其留一个含义不明的空配置，不如引导用户去关开关
    ElMessage.warning('至少保留一个抓取时刻；如要停止定时抓取，请关闭「启用定时抓取」')
    return
  }
  draft.value[it.key] = next
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

const dirty = computed(() =>
  items.value.some((it) => draft.value[it.key] !== undefined && draft.value[it.key] !== it.value),
)

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const cfg = await api.config()
    items.value = cfg.settings ?? []
    draft.value = {}
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
  draft.value[it.key] = v
}

function setBool(it: SettingItem, v: boolean | string | number) {
  draft.value[it.key] = Boolean(v)
}
function setText(it: SettingItem, v: string) {
  draft.value[it.key] = v
}

async function save() {
  if (!dirty.value) {
    ElMessage.info('没有改动')
    return
  }
  saving.value = true
  try {
    const r = await api.saveSettings(draft.value)
    items.value = r.settings
    draft.value = {}
    ElMessage.success('已保存，按各项标注的生效范围生效')
    // 自动刷新属前端参数：保存后立即同步到全局状态，不必刷新页面
    const auto = r.settings.find((s) => s.key === 'enable_auto_refresh')
    if (auto) dash.setEnableAutoRefresh(Boolean(auto.value))
    // 时刻/开关改动会改变「下次执行」：重取调度状态，避免页面显示与实际调度口径不一致
    await loadSchedule()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`保存失败: ${(e as Error).message}`)
  } finally {
    saving.value = false
  }
}

async function resetOne(it: SettingItem) {
  await ElMessageBox.confirm(
    `将「${it.label}」恢复为默认值 ${String(it.default)}，确定吗？`,
    '恢复默认',
    { type: 'warning', confirmButtonText: '恢复默认', cancelButtonText: '取消' },
  ).catch(() => false)
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
    draft.value = {}
    const auto = r.settings.find((s) => s.key === 'enable_auto_refresh')
    if (auto) dash.setEnableAutoRefresh(Boolean(auto.value))
    ElMessage.success('已恢复默认')
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`恢复失败: ${(e as Error).message}`)
  }
}

/** 保存前可复制当前草稿，便于反馈/备份（复用统一剪贴板实现） */
function copyDraft() {
  const text = JSON.stringify(draft.value, null, 2)
  if (legacyCopy(text)) ElMessage.success('已复制改动内容')
  else ElMessage.error('复制失败')
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
  <div v-loading="loading">
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
        </ul>
      </div>

      <div v-for="g in GROUPS" :key="g.title" class="page-card">
        <div class="group-title">{{ g.title }}</div>
        <div v-if="g.desc" class="group-desc text-muted">{{ g.desc }}</div>
        <!-- 定时抓取：状态区（只读，来自 /api/schedule，与真实触发判定同源）放在配置项之前，
             用户先看到「下次什么时候跑、上次为什么没跑」，再决定怎么改参数 -->
        <div v-if="g.extra === 'schedule'" class="sched-status">
          <template v-if="sched">
            <!-- 当前状态放第一行：点了「立即运行一次」或到点触发后，用户先要看的就是「现在在跑没有」 -->
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
              <span class="ss-value">{{ sched.next_run_at ?? '未启用（无计划时刻）' }}</span>
            </div>
            <div class="ss-row">
              <span class="ss-label">今日已处理</span>
              <span class="ss-value">{{ sched.today_done.length ? sched.today_done.join('、') : '—' }}</span>
            </div>
            <div class="ss-row">
              <span class="ss-label">上次结果</span>
              <span class="ss-value">
                <template v-if="sched.last">
                  <el-tag size="small" :type="SCHED_ACTION[sched.last.action].type">
                    {{ SCHED_ACTION[sched.last.action].text }}
                  </el-tag>
                  <span class="text-muted">{{ sched.last.at }} · {{ sched.last.reason }}</span>
                </template>
                <span v-else class="text-muted">暂无调度记录</span>
              </span>
            </div>
            <div class="ss-row">
              <span class="ss-label">调度线程</span>
              <span class="ss-value text-muted">
                {{
                  sched.last_tick
                    ? `最近判定 ${sched.last_tick}（每 ${sched.tick_seconds} 秒一次）`
                    : '尚未运行'
                }}
              </span>
            </div>
          </template>
          <div v-else class="text-muted">状态读取失败，稍后自动重试</div>
          <div class="ss-actions">
            <!-- 禁用态按钮上的 tooltip 需外包一层可命中元素（与下载中心同一约定）；
                 按钮可用性与后端守卫同源：批次在跑时禁用，避免点了才报「已有批次」 -->
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
                  <el-time-picker
                    :model-value="t"
                    format="HH:mm"
                    value-format="HH:mm"
                    :size="isMobile ? 'small' : 'default'"
                    placeholder="选择时刻"
                    class="st-picker"
                    @update:model-value="(v: string | number | Date | null) => setTimeAt(it, i, v == null ? t : String(v))"
                  />
                  <el-button link type="danger" size="small" @click="removeTime(it, i)">删除</el-button>
                </div>
                <div class="st-actions">
                  <el-button link type="primary" size="small" @click="addTime(it)">添加时刻</el-button>
                  <span class="sr-default">默认：{{ (it.default as string[]).join('、') }}</span>
                </div>
              </div>
              <template v-else-if="it.type === 'text'">
                <el-input
                  :model-value="String(valueOf(it))"
                  clearable
                  placeholder="留空则自动取访问地址"
                  class="sr-input"
                  @input="(v: string) => setText(it, v)"
                />
                <div class="sr-default text-muted">
                  默认：{{ it.default ? String(it.default) : '自动（取访问地址）' }}
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
              <el-button link type="primary" size="small" @click="resetOne(it)">恢复默认</el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 链接黑名单：作用于大屏各卡片口径（累计/近7日/近30日/榜单/推荐），不影响帖子页与下载 -->
      <div class="page-card">
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

      <!-- 操作条：移动端铺满换行（与工具栏同一模式） -->
      <div class="page-card actions">
        <span class="text-muted">
          <template v-if="dirty">有未保存的改动</template>
          <template v-else>无改动</template>
        </span>
        <div class="actions-right">
          <el-button v-if="dirty" @click="copyDraft">复制改动</el-button>
          <el-button @click="resetAll">全部恢复默认</el-button>
          <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
            保存设置
          </el-button>
        </div>
      </div>
    </template>
  </div>
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
  width: 130px;
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

.st-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
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
  .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .sr-ctrl {
    justify-content: flex-start;
    flex-wrap: wrap;
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
