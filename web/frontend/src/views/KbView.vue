<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Document, Position, Refresh, Search } from '@element-plus/icons-vue'
import { init as echartsInit, use, type ECharts } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { GraphChart } from 'echarts/charts'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import {
  api,
  isAborted,
  type FidMeta,
  type KbAskResp,
  type KbGraphResp,
  type KbGraphNode,
  type KbIndexStatus,
  type KbRagStatus,
  type KbSearchItem,
  type KbSearchResp,
} from '../api'
import FollowLogDrawer from '../components/FollowLogDrawer.vue'
import { postCopyUrl, postOpenUrl } from '../utils/postUrl'

/**
 * 知识库（二期，方案 §3.5）：FTS5 全文搜索 + ECharts 图谱。
 * - 只读消费 vault 索引（web/kb.py），不并入数据总览联动组（修订 7）；
 * - 摘要与全部下发内容一律文本插值渲染，**禁 v-html**（源自帖子正文的内容不可信，修订 16）；
 * - 图谱节点三类（source/concept/entity），超出 500 节点上限时后端加权截断、此处明示。
 * 三期（方案 §3.6）：RAG 问答卡（向量召回 + LLM 生成，非流式），答案与引用同样纯文本渲染。
 */
use([CanvasRenderer, GraphChart, LegendComponent, TooltipComponent])

// ---------- 索引状态条 ----------
const indexStatus = ref<KbIndexStatus | null>(null)
const indexError = ref('')
let statusTimer: ReturnType<typeof setInterval> | null = null
let ragTimer: ReturnType<typeof setInterval> | null = null

const indexReady = computed(() => indexStatus.value?.state === 'ready')

function statusText(s: KbIndexStatus): string {
  if (s.state === 'rebuilding') {
    const p = s.progress
    return p ? `索引重建中 ${p.done}/${p.total}` : '索引重建中'
  }
  if (s.state === 'empty') return '索引尚未建立'
  return '索引就绪'
}

async function loadStatus() {
  try {
    indexStatus.value = await api.kbIndexStatus()
    indexError.value = ''
    // 就绪且尚未加载过图谱时自动出图（首次进入页面的主路径）
    if (indexReady.value && !graphData.value) void loadGraph()
  } catch (e) {
    if (!isAborted(e)) indexError.value = e instanceof Error ? e.message : String(e)
  }
}

// ---------- 全文搜索 ----------
const fidList = ref<FidMeta[]>([])
const searchQuery = ref('')
const searchFid = ref<string>('')
const searching = ref(false)
const searchResult = ref<KbSearchResp | null>(null)
const searchError = ref('')

async function loadFids() {
  try {
    fidList.value = await api.fidMeta()
  } catch {
    fidList.value = [] // 版块元数据失败不阻塞搜索主功能
  }
}

async function doSearch() {
  const q = searchQuery.value.trim()
  if (!q) {
    ElMessage.warning('请输入搜索关键词')
    return
  }
  searching.value = true
  searchError.value = ''
  try {
    searchResult.value = await api.kbSearch(q, searchFid.value || undefined)
  } catch (e) {
    searchResult.value = null
    if (!isAborted(e)) searchError.value = e instanceof Error ? e.message : String(e)
  } finally {
    searching.value = false
  }
}

/** 摘要高亮切分（tokens 命中片段标记，纯文本插值渲染——替代 v-html 的唯一安全做法） */
function snippetParts(snippet: string, tokens: string[]): { text: string; hit: boolean }[] {
  if (!tokens.length || !snippet) return [{ text: snippet, hit: false }]
  const lowerTokens = [...tokens].sort((a, b) => b.length - a.length).map((t) => t.toLowerCase())
  const parts: { text: string; hit: boolean }[] = []
  let buf = ''
  let i = 0
  const lower = snippet.toLowerCase()
  while (i < snippet.length) {
    const hit = lowerTokens.find((t) => lower.startsWith(t, i))
    if (hit) {
      if (buf) {
        parts.push({ text: buf, hit: false })
        buf = ''
      }
      parts.push({ text: snippet.slice(i, i + hit.length), hit: true })
      i += hit.length
    } else {
      buf += snippet[i]
      i += 1
    }
  }
  if (buf) parts.push({ text: buf, hit: false })
  return parts
}

function openPost(item: Pick<KbSearchItem, 'url'>) {
  if (!item.url) return
  window.open(postOpenUrl(item.url), '_blank')
}

async function copyVaultPath(item: Pick<KbSearchItem, 'rel'>) {
  try {
    await navigator.clipboard.writeText(item.rel)
    ElMessage.success('已复制 vault 相对路径')
  } catch {
    ElMessage.error('复制失败（剪贴板不可用）')
  }
}

// ---------- RAG 问答（三期，方案 §3.6） ----------
const ragStatus = ref<KbRagStatus | null>(null)
const ragError = ref('')
const ragQuestion = ref('')
const asking = ref(false)
const askResult = ref<KbAskResp | null>(null)
const askError = ref('')
const rebuilding = ref(false)

/** 状态标签（穷举后端五分支） */
function ragStateTag(s: KbRagStatus): { text: string; type: 'success' | 'warning' | 'info' | 'danger' } {
  if (s.state === 'ready') return { text: '问答就绪', type: 'success' }
  if (s.state === 'rebuilding') {
    const p = s.progress
    return { text: p ? `向量重嵌中 ${p.done}/${p.total}` : '向量重嵌中', type: 'warning' }
  }
  if (s.state === 'mismatch') return { text: 'embedding 配置已变化，需重建', type: 'warning' }
  if (s.state === 'error') return { text: 'RAG 后端异常', type: 'danger' }
  return { text: '未建向量索引', type: 'info' }
}

const ragReady = computed(() => ragStatus.value?.state === 'ready')

// 执行日志抽屉：共用 FollowLogDrawer（与设置页 kb 批次日志同一交互形态），此处映射 RAG 重建日志
const ragLogOpen = ref(false)

function openRagLog() {
  ragLogOpen.value = true
}

/** RAG 重建日志 → 共用抽屉契约：进度 = done/total（按文件数） */
async function loadRagLogs(after: number) {
  const r = await api.kbRagLogs(after)
  const p = r.progress
  return {
    running: r.running,
    lines: r.lines,
    last_seq: r.last_seq,
    progressText: p ? `重嵌中 ${p.done}/${p.total} 文件` : null,
    progressPct: p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : null,
  }
}

async function loadRagStatus() {
  try {
    ragStatus.value = await api.kbRagStatus()
    ragError.value = ''
  } catch (e) {
    if (!isAborted(e)) ragError.value = e instanceof Error ? e.message : String(e)
  }
  // 轮询周期按状态自适应：重嵌中 5s（进度可见），其余 30s
  if (ragTimer) clearInterval(ragTimer)
  ragTimer = setInterval(() => void loadRagStatus(), ragStatus.value?.state === 'rebuilding' ? 5000 : 30000)
}

async function doAsk() {
  const q = ragQuestion.value.trim()
  if (!q) {
    ElMessage.warning('请输入问题')
    return
  }
  asking.value = true
  askError.value = ''
  askResult.value = null
  try {
    askResult.value = await api.kbAsk(q)
  } catch (e) {
    if (!isAborted(e)) askError.value = e instanceof Error ? e.message : String(e)
  } finally {
    asking.value = false
  }
}

/** 答案 [n] 引用标记切分（纯文本插值渲染，替代 v-html 的安全做法，与 snippetParts 同思路） */
function answerParts(answer: string): { text: string; cite: number | null }[] {
  const parts: { text: string; cite: number | null }[] = []
  const re = /\[(\d{1,2})\]/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(answer)) !== null) {
    if (m.index > last) parts.push({ text: answer.slice(last, m.index), cite: null })
    parts.push({ text: m[0], cite: Number(m[1]) })
    last = m.index + m[0].length
  }
  if (last < answer.length) parts.push({ text: answer.slice(last), cite: null })
  return parts
}

/** 引用编号是否存在（防 LLM 输出越界编号 → 标记降级为纯文本） */
function citeExists(n: number): boolean {
  return askResult.value?.citations.some((_, i) => i + 1 === n) ?? false
}

async function doRebuild() {
  rebuilding.value = true
  try {
    await api.kbRagRebuild()
    ElMessage.success('已排队全量重嵌（后台执行，完成后自动恢复）')
    await loadRagStatus()
  } catch (e) {
    if (!isAborted(e)) ElMessage.error(`触发重建失败: ${(e as Error).message}`)
  } finally {
    rebuilding.value = false
  }
}

function askOpenPost(rel: string, url: string) {
  if (!url) return
  window.open(postOpenUrl(url), '_blank')
}

async function askCopyRel(rel: string) {
  try {
    await navigator.clipboard.writeText(rel)
    ElMessage.success('已复制 vault 相对路径')
  } catch {
    ElMessage.error('复制失败（剪贴板不可用）')
  }
}

// ---------- 图谱 ----------
const chartEl = ref<HTMLDivElement>()
const chart = shallowRef<ECharts | null>(null)
const graphData = ref<KbGraphResp | null>(null)
const graphError = ref('')
const graphLoading = ref(false)
const graphFid = ref<string>('')
const centerNode = ref<KbGraphNode | null>(null)

// 节点三类配色（与节点类型徽标同源，图谱与列表视觉一致）
const KIND_META: Record<string, { label: string; color: string }> = {
  source: { label: '帖子笔记', color: '#2f6fed' },
  concept: { label: '概念页', color: '#e6a23c' },
  entity: { label: '实体页', color: '#8e5bd6' },
}

async function loadGraph() {
  if (!indexReady.value) return
  graphLoading.value = true
  graphError.value = ''
  try {
    graphData.value = await api.kbGraph(graphFid.value || undefined, 500, centerNode.value?.id)
    await nextTick()
    renderGraph()
  } catch (e) {
    graphData.value = null
    if (!isAborted(e)) graphError.value = e instanceof Error ? e.message : String(e)
  } finally {
    graphLoading.value = false
  }
}

function renderGraph() {
  const data = graphData.value
  if (!data || !chartEl.value) return
  if (!chart.value) {
    chart.value = echartsInit(chartEl.value)
    // 节点点击下钻：source → 打开原帖；entity/concept → 以该节点为中心展开一跳邻居
    chart.value.on('click', (params) => {
      const id = (params.data as { id?: string } | undefined)?.id
      const node = graphData.value?.nodes.find((n) => n.id === id)
      if (node) showNode(node)
    })
  }
  const nodes = data.nodes.map((n) => ({
    id: n.id,
    name: n.id, // 边以 id 关联；展示名由 label formatter 取（name 可能跨类重复）
    kind: n.kind,
    labelName: n.name,
    itemStyle: { color: KIND_META[n.kind]?.color ?? '#909399' },
    symbolSize: Math.min(12 + n.degree * 1.5, n.kind === 'source' ? 26 : 46),
    date: n.date,
    url: n.url,
    sub: n.sub,
    degree: n.degree,
  }))
  chart.value.setOption(
    {
      tooltip: {
        appendToBody: true,
        confine: true,
        formatter: (p: { data?: { kind?: string; labelName?: string; degree?: number; date?: string; sub?: string } }) => {
          const d = p.data ?? {}
          const kindLabel = KIND_META[d.kind ?? '']?.label ?? d.kind
          const lines = [`<b>${d.labelName ?? ''}</b>`, `${kindLabel}${d.sub ? ' · ' + d.sub : ''}`, `连接数 ${d.degree ?? 0}`]
          if (d.date) lines.push(String(d.date))
          return lines.join('<br/>')
        },
      },
      legend: {
        top: 4,
        data: Object.values(KIND_META).map((m) => m.label),
        textStyle: { color: '#606266' },
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          categories: Object.entries(KIND_META).map(([k, m]) => ({ name: m.label, key: k })),
          force: { repulsion: 90, edgeLength: [24, 90], gravity: 0.12 },
          emphasis: { focus: 'adjacency', lineStyle: { width: 2 } },
          label: {
            show: true,
            position: 'right',
            fontSize: 10,
            color: '#606266',
            formatter: (p: { data?: { labelName?: string; kind?: string } }) =>
              p.data?.kind === 'source' ? '' : (p.data?.labelName ?? ''), // 仅枢纽节点常显标签
          },
          data: nodes,
          links: data.edges.map((e) => ({ source: e.source, target: e.target })),
        },
      ],
    },
    true,
  )
}

// ---------- 节点详情弹窗 ----------
const selectedNode = ref<KbGraphNode | null>(null)
const nodeDialogVisible = ref(false)

function showNode(node: KbGraphNode) {
  selectedNode.value = node
  nodeDialogVisible.value = true
}

function nodeFromGraph(id: string): KbGraphNode | undefined {
  return graphData.value?.nodes.find((n) => n.id === id)
}

function openNodePost() {
  const n = selectedNode.value
  if (n?.url) window.open(postOpenUrl(n.url), '_blank')
}

async function copyNodeRel() {
  const n = selectedNode.value
  if (!n) return
  try {
    await navigator.clipboard.writeText(n.id)
    ElMessage.success('已复制 vault 相对路径')
  } catch {
    ElMessage.error('复制失败（剪贴板不可用）')
  }
}

function expandFromNode() {
  const n = selectedNode.value
  if (!n) return
  centerNode.value = n
  selectedNode.value = null
  void loadGraph()
}

function resetCenter() {
  centerNode.value = null
  void loadGraph()
}

watch(graphFid, () => {
  centerNode.value = null
  void loadGraph()
})

function handleResize() {
  chart.value?.resize()
}

onMounted(async () => {
  void loadStatus()
  void loadFids()
  void loadRagStatus()
  // 重建期间 5s 轮询进度；就绪后退化为 30s 低频（状态条 + 自动首图）
  statusTimer = setInterval(() => void loadStatus(), indexReady.value ? 30000 : 5000)
  // RAG 状态轮询：重嵌中 5s，其余 30s（廉价接口，仅内存态 + meta 计数）
  ragTimer = setInterval(() => void loadRagStatus(), ragStatus.value?.state === 'rebuilding' ? 5000 : 30000)
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  if (statusTimer) clearInterval(statusTimer)
  if (ragTimer) clearInterval(ragTimer)
  window.removeEventListener('resize', handleResize)
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div>
    <!-- 索引状态条 -->
    <div class="page-card kb-status" style="margin-bottom: 16px">
      <div class="kb-status-main">
        <span class="kb-title">知识库</span>
        <el-tag
          :type="indexStatus?.state === 'ready' ? 'success' : indexStatus?.state === 'rebuilding' ? 'warning' : 'info'"
          size="small"
        >
          {{ indexStatus ? statusText(indexStatus) : '状态获取中…' }}
        </el-tag>
        <template v-if="indexStatus">
          <span class="kb-meta">笔记 {{ indexStatus.docs }}</span>
          <span class="kb-meta">图谱节点 {{ indexStatus.nodes }}</span>
          <span class="kb-meta">互链边 {{ indexStatus.edges }}</span>
          <span v-if="indexStatus.built_at" class="kb-meta">建立于 {{ indexStatus.built_at }}</span>
        </template>
      </div>
      <el-progress
        v-if="indexStatus?.state === 'rebuilding' && indexStatus.progress"
        :percentage="indexStatus.progress.total ? Math.round((indexStatus.progress.done / indexStatus.progress.total) * 100) : 0"
        :stroke-width="6"
        style="margin-top: 10px"
      />
      <div class="note" style="margin-top: 10px; margin-bottom: 0">
        <span>
          语料为 Obsidian vault 的帖子笔记（<code>outputs/vault/</code>，由 <code>kb_export.py</code> 沉淀），
          图谱含笔记 / 概念页 / 实体页三类节点；Obsidian 打开 vault 目录可同步浏览。
        </span>
        <el-button size="small" text type="primary" :icon="Refresh" @click="loadStatus">刷新状态</el-button>
      </div>
    </div>

    <el-alert
      v-if="indexStatus && indexStatus.state !== 'ready'"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
      :title="indexStatus.state === 'rebuilding' ? '全文索引正在后台重建，完成后自动可搜（首请求不阻塞重建）' : '索引尚未建立：vault 为空或预热进行中'"
    />
    <el-alert v-else-if="indexError" type="error" :closable="false" show-icon style="margin-bottom: 16px" :title="indexError" />

    <!-- 全文搜索 -->
    <div class="page-card" style="margin-bottom: 16px">
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          class="kb-search-input"
          placeholder="搜索笔记正文（jieba 中文分词 + BM25 排序）"
          clearable
          :prefix-icon="Search"
          :disabled="!indexReady"
          @keyup.enter="doSearch"
        />
        <el-select v-model="searchFid" class="kb-fid-select" clearable placeholder="全部版块" :disabled="!indexReady">
          <el-option v-for="f in fidList" :key="f.fid" :label="f.name" :value="f.fid" />
        </el-select>
        <el-button type="primary" :icon="Search" :loading="searching" :disabled="!indexReady" @click="doSearch">
          搜索
        </el-button>
      </div>

      <el-alert v-if="searchError" type="error" :closable="false" show-icon style="margin-bottom: 12px" :title="searchError" />
      <el-alert
        v-else-if="searchResult?.hint"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        :title="searchResult.hint"
      />

      <template v-if="searchResult">
        <div class="kb-result-count">
          命中 <b>{{ searchResult.total }}</b> 条
          <span v-if="searchResult.items.length < searchResult.total" class="text-muted">（显示前 {{ searchResult.items.length }} 条）</span>
        </div>
        <div v-for="item in searchResult.items" :key="item.rel" class="kb-result-item">
          <div class="kb-result-head">
            <span class="kb-result-title" @click="openPost(item)">{{ item.title }}</span>
            <el-tag v-if="item.fid_name" size="small" type="info" effect="plain">{{ item.fid_name }}</el-tag>
            <span class="kb-result-date">{{ item.date }}</span>
          </div>
          <!-- 纯文本插值 + 命中片段标记：替代 v-html 的安全渲染（修订 16 红线） -->
          <p class="kb-snippet">
            <template v-for="(seg, i) in snippetParts(item.snippet, searchResult.tokens)" :key="i">
              <mark v-if="seg.hit" class="kb-mark">{{ seg.text }}</mark>
              <template v-else>{{ seg.text }}</template>
            </template>
          </p>
          <div class="kb-result-actions">
            <el-button link type="primary" :icon="Position" :disabled="!item.url" @click="openPost(item)">
              打开原帖
            </el-button>
            <el-button link :icon="CopyDocument" @click="copyVaultPath(item)">复制 vault 路径</el-button>
          </div>
        </div>
        <el-empty v-if="!searchResult.items.length && !searchResult.hint" description="无命中结果" />
      </template>
    </div>

    <!-- RAG 问答（三期，方案 §3.6）：非流式，答案 + 引用可回原帖 / vault -->
    <div class="page-card" style="margin-bottom: 16px">
      <div class="toolbar">
        <span class="kb-subtitle">知识库问答</span>
        <el-tag
          v-if="ragStatus"
          :type="ragStateTag(ragStatus).type"
          size="small"
        >
          {{ ragStateTag(ragStatus).text }}
        </el-tag>
        <el-progress
          v-if="ragStatus?.state === 'rebuilding' && ragStatus.progress"
          class="kb-rag-progress"
          :percentage="ragStatus.progress.total ? Math.round((ragStatus.progress.done / ragStatus.progress.total) * 100) : 0"
          :stroke-width="6"
        />
        <span class="flex-spacer" />
        <el-button size="small" text type="primary" :icon="Document" @click="openRagLog">执行日志</el-button>
        <el-button
          size="small"
          :icon="Refresh"
          :loading="rebuilding"
          :disabled="ragStatus?.state === 'rebuilding'"
          @click="doRebuild"
        >
          重建向量索引
        </el-button>
      </div>

      <div v-if="ragStatus" class="kb-rag-config">
        <span>
          embedding：{{ ragStatus.embed.provider }} / {{ ragStatus.embed.model || '未配置' }}
          <span v-if="!ragStatus.embed.ready" class="kb-rag-warn">（{{ ragStatus.embed.reason }}）</span>
        </span>
        <span>
          生成：{{ ragStatus.generation.provider }} / {{ ragStatus.generation.model || '未配置' }}
          （设置页「知识库」组可改）
        </span>
      </div>
      <el-alert
        v-if="ragStatus && ragStatus.state === 'mismatch'"
        type="warning"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        :title="ragStatus.detail + '；点击「重建向量索引」后恢复（全量重嵌，产生 embedding 调用费用）'"
      />
      <el-alert
        v-else-if="ragStatus && ragStatus.state === 'error'"
        type="error"
        :closable="false"
        show-icon
        style="margin-bottom: 12px"
        :title="ragStatus.detail"
      />
      <el-alert v-else-if="ragError" type="error" :closable="false" show-icon style="margin-bottom: 12px" :title="ragError" />

      <div class="toolbar" style="margin-bottom: 0">
        <el-input
          v-model="ragQuestion"
          class="kb-search-input"
          placeholder="用自然语言提问（基于 vault 笔记的向量语义检索 + LLM 生成，非流式）"
          clearable
          :disabled="!ragReady"
          @keyup.enter="doAsk"
        />
        <el-button type="primary" :loading="asking" :disabled="!ragReady" @click="doAsk">提问</el-button>
      </div>

      <el-alert v-if="askError" type="error" :closable="false" show-icon style="margin-top: 12px" :title="askError" />

      <template v-if="askResult">
        <div class="kb-ask-answer">
          <!-- 纯文本插值 + [n] 引用标记：替代 v-html 的安全渲染（修订 16 红线） -->
          <template v-for="(seg, i) in answerParts(askResult.answer)" :key="i">
            <el-tooltip v-if="seg.cite && citeExists(seg.cite)" :content="askResult.citations[seg.cite - 1].title" placement="top">
              <sup class="kb-cite-tag">[{{ seg.cite }}]</sup>
            </el-tooltip>
            <span v-else>{{ seg.text }}<template v-if="seg.cite">{{ seg.cite }}</template></span>
          </template>
        </div>
        <div class="kb-ask-meta">
          模型 {{ askResult.model }}
          <template v-if="askResult.usage_tokens"> · {{ askResult.usage_tokens }} tokens</template>
        </div>
        <div class="kb-result-count">引用 {{ askResult.citations.length }} 条</div>
        <div v-for="(c, i) in askResult.citations" :key="c.rel + i" class="kb-result-item">
          <div class="kb-result-head">
            <span class="kb-cite-tag">[{{ i + 1 }}]</span>
            <span class="kb-result-title" @click="askOpenPost(c.rel, c.url)">{{ c.title }}</span>
            <el-tag v-if="c.fid_name" size="small" type="info" effect="plain">{{ c.fid_name }}</el-tag>
            <span class="kb-result-date">{{ c.date }}</span>
            <span class="kb-result-date">相似度 {{ c.score }}</span>
          </div>
          <p class="kb-snippet">{{ c.snippet }}</p>
          <div class="kb-result-actions">
            <el-button link type="primary" :icon="Position" :disabled="!c.url" @click="askOpenPost(c.rel, c.url)">
              打开原帖
            </el-button>
            <el-button link :icon="CopyDocument" @click="askCopyRel(c.rel)">复制 vault 路径</el-button>
          </div>
        </div>
      </template>
      <div v-else-if="!asking" class="kb-chart-hint" style="margin-top: 10px">
        提问后返回答案与引用片段；引用条目可打开原帖或复制 vault 路径到 Obsidian 对照。
      </div>
    </div>

    <!-- 图谱 -->
    <div class="page-card">
      <div class="toolbar">
        <span class="kb-subtitle">知识图谱</span>
        <el-select v-model="graphFid" class="kb-fid-select" clearable placeholder="全部版块">
          <el-option v-for="f in fidList" :key="f.fid" :label="f.name" :value="f.fid" />
        </el-select>
        <el-tag v-if="centerNode" size="small" closable type="warning" @close="resetCenter">
          围绕：{{ centerNode.name }}
        </el-tag>
        <span class="flex-spacer" />
        <el-button :icon="Refresh" :loading="graphLoading" :disabled="!indexReady" @click="loadGraph">重新布局</el-button>
      </div>

      <el-alert
        v-if="graphData?.truncated"
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 8px"
        :title="`节点已达上限 ${graphData.node_cap}（全部 ${graphData.total_nodes} 个），已按互动加权截断；可用版块筛选或点击节点下钻缩小范围`"
      />
      <el-alert v-if="graphError" type="error" :closable="false" show-icon style="margin-bottom: 8px" :title="graphError" />

      <div v-loading="graphLoading" class="kb-chart" element-loading-text="图谱组装中…">
        <div v-show="graphData" ref="chartEl" class="kb-chart-canvas" />
        <el-empty
          v-if="!graphLoading && !graphData"
          :description="indexReady ? '暂无图谱数据' : '索引就绪后自动出图'"
        />
      </div>
      <div class="kb-chart-hint">点击节点查看详情；实体页 / 概念页可下钻展开一跳邻居。悬停查看节点信息。</div>
    </div>

    <!-- 节点详情弹窗 -->
    <el-dialog v-model="nodeDialogVisible" :title="selectedNode?.name" width="420px">
      <template v-if="selectedNode">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="类型">
            {{ KIND_META[selectedNode.kind]?.label ?? selectedNode.kind }}
            <span v-if="selectedNode.sub">（{{ selectedNode.sub }}）</span>
          </el-descriptions-item>
          <el-descriptions-item label="连接数">{{ selectedNode.degree }}</el-descriptions-item>
          <el-descriptions-item v-if="selectedNode.date" label="发布日期">{{ selectedNode.date }}</el-descriptions-item>
          <el-descriptions-item label="vault 路径">
            <code class="kb-rel">{{ selectedNode.id }}</code>
          </el-descriptions-item>
        </el-descriptions>
      </template>
      <template #footer>
        <el-button v-if="selectedNode?.kind === 'source'" type="primary" :disabled="!selectedNode?.url" @click="openNodePost">
          打开原帖
        </el-button>
        <el-button v-else type="primary" @click="expandFromNode">以此为中心展开</el-button>
        <el-button :icon="CopyDocument" @click="copyNodeRel">复制路径</el-button>
      </template>
    </el-dialog>

    <!-- RAG 重建执行日志抽屉（共用 FollowLogDrawer，与设置页 kb 批次日志同一形态） -->
    <FollowLogDrawer
      v-model="ragLogOpen"
      title="RAG 向量重建执行日志"
      :load="loadRagLogs"
      empty-text="暂无日志：点「重建向量索引」后此处实时输出重嵌进度（保留最近 800 行）"
      idle-hint="（空闲时展示最近一次重建日志）"
    />
  </div>
</template>

<style scoped>
.kb-status-main {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.kb-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2d3d;
}

.kb-subtitle {
  font-size: 14px;
  font-weight: 600;
  color: #1f2d3d;
}

.kb-meta {
  color: #909399;
  font-size: 13px;
}

.note {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #909399;
  font-size: 13px;
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
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.flex-spacer {
  flex: 1;
}

.kb-search-input {
  width: 360px;
  max-width: 100%;
}

.kb-fid-select {
  width: 150px;
}

.kb-result-count {
  color: #606266;
  font-size: 13px;
  margin-bottom: 10px;
}

.kb-result-item {
  padding: 12px 0;
  border-top: 1px solid var(--app-border);
}

.kb-result-item:first-of-type {
  border-top: none;
}

.kb-result-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.kb-result-title {
  font-weight: 600;
  color: #1f2d3d;
  cursor: pointer;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-result-title:hover {
  color: #2f6fed;
}

.kb-result-date {
  color: #909399;
  font-size: 12px;
}

.kb-snippet {
  color: #606266;
  font-size: 13px;
  line-height: 1.7;
  margin: 6px 0;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.kb-mark {
  background: #ffe9a8;
  color: inherit;
  padding: 0 1px;
  border-radius: 2px;
}

.kb-result-actions {
  display: flex;
  gap: 4px;
}

.kb-chart {
  min-height: 420px;
}

.kb-chart-canvas {
  width: 100%;
  height: 480px;
}

.kb-chart-hint {
  color: #909399;
  font-size: 12px;
  margin-top: 8px;
}

.kb-rel {
  word-break: break-all;
  font-size: 12px;
}

.kb-rag-progress {
  flex: 1;
  min-width: 120px;
}

.kb-rag-config {
  display: flex;
  gap: 18px;
  flex-wrap: wrap;
  color: #909399;
  font-size: 12px;
  margin-bottom: 10px;
}

.kb-rag-warn {
  color: #e6a23c;
}

.kb-ask-answer {
  margin-top: 12px;
  padding: 12px;
  background: #f7f9fc;
  border-radius: 6px;
  color: #303133;
  font-size: 14px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}

.kb-cite-tag {
  color: #2f6fed;
  font-weight: 600;
  cursor: default;
}

.kb-ask-meta {
  color: #909399;
  font-size: 12px;
  margin: 8px 0;
}

@media (max-width: 768px) {
  .kb-search-input {
    width: 100%;
  }

  .kb-fid-select {
    flex: 1;
    min-width: 120px;
  }

  .kb-chart-canvas {
    height: 360px;
  }
}
</style>
