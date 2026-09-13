<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, isAborted, type BlacklistItem, type SettingItem } from '../api'
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
const GROUPS: { title: string; desc?: string; keys: string[] }[] = [
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
  void loadBlacklist()
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
        <div class="setting-list">
          <div v-for="it in groupItems(g.keys)" :key="it.key" class="setting-row">
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
        <div v-loading="blLoading" class="bl-list">
          <div v-if="!blItems.length" class="text-muted bl-empty">暂无黑名单</div>
          <div v-for="it in blItems" :key="it.type + '|' + it.value" class="bl-row">
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

  .actions {
    flex-direction: column;
    align-items: stretch;
  }

  .actions-right {
    justify-content: flex-end;
  }
}
</style>
