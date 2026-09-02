<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { CopyDocument, Download, Search, View } from '@element-plus/icons-vue'
import { api, exportCsvUrl, isAborted, type FidMeta, type Post, type PostsPage } from '../api'
import { formatRelativeTime, formatFullTime } from '../utils/time'

const route = useRoute()

const fidMeta = ref<FidMeta[]>([])
const pageData = ref<PostsPage | null>(null)
const loading = ref(false)
const exporting = ref(false)

/** 表格多选的行（批量下载用） */
const selectedRows = ref<Post[]>([])
/** 提交下载任务中：防连点重复创建任务 */
const submitting = ref(false)

const filters = reactive({
  fid: [] as string[],
  dateRange: null as [string, string] | null,
  q: '',
  author: '',
})
const page = ref(1)
const pageSize = ref(50)

/**
 * 统一排序状态：下拉与表头排序**共用这一份**，避免出现
 * 「下拉显示日期倒序、实际却按标题排」的状态冲突。
 * order 为 null 表示不按列排序（语义上回到默认：日期倒序）。
 * 分页列表必须走服务端排序，只排当前页等于没排序，故这里只负责描述排序意图。
 */
const colSort = ref<{ by: string; order: 'asc' | 'desc' | null }>({ by: 'date', order: 'desc' })

/**
 * 有对应表格列、可参与表头排序的字段。
 * fid 已并入标题列显示，没有独立列，因此不在此集合——只能由下拉触发排序
 * （后端 _SORT_FIELDS 仍支持，按 CAST(fid AS INTEGER) 排数字序）。
 * engagement / hot 同理：无对应列，纯下拉触发。
 */
const SORTABLE_PROPS = new Set(['title', 'likes', 'author', 'replies', 'created_at'])

const tableRef = ref<{ sort: (p: string, o: string) => void; clearSort: () => void } | null>(null)

/**
 * 表格容器的**实测可用宽度**（用 ResizeObserver 而非视口宽度——侧边栏折叠、
 * 窗口缩放、系统缩放都会改变它，视口宽度推算是算不准的）。
 *
 * el-table 各列 min-width 合计约 970px，容器一旦小于它就会出现横向滚动条。
 * 故按实测宽度分级隐藏低优先级列，保证任何宽度下都不横向滚动。
 * 阈值取「隐藏该列后剩余列的下限合计」：
 *   全列 970 → 隐藏发布时间 874 → 再隐藏作者 798 → 再隐藏回复 732 → 再隐藏点赞 666。
 */
const tableBoxRef = ref<HTMLElement | null>(null)
const tableWidth = ref(1200)
let tableRo: ResizeObserver | null = null

onMounted(() => {
  if (!tableBoxRef.value) return
  tableRo = new ResizeObserver((entries) => {
    const w = entries[0]?.contentRect.width ?? 0
    if (w > 0) tableWidth.value = w
  })
  tableRo.observe(tableBoxRef.value)
})

onBeforeUnmount(() => tableRo?.disconnect())

const colVisible = computed(() => {
  const w = tableWidth.value
  return {
    time: w >= 970,
    author: w >= 874,
    replies: w >= 798,
    likes: w >= 732,
  }
})

/** 被隐藏列的信息补进标题的 tooltip，避免窄屏下信息丢失 */
function rowTip(row: Post) {
  const v = colVisible.value
  const parts: string[] = [row.title]
  if (!v.author && row.author) parts.push('\u4f5c\u8005\uff1a' + row.author)
  if (!v.time && row.created_at) parts.push('\u53d1\u5e03\uff1a' + formatFullTime(row.created_at))
  if (!v.replies) parts.push('\u56de\u590d\uff1a' + (row.replies ?? '-'))
  if (!v.likes) parts.push('\u70b9\u8d5e\uff1a' + (row.likes ?? '-'))
  return parts.join('\n')
}

const queryText = ref('')

// 下钻支持的合法 sort 选项（与后端 _SORTS 的键一致）
const SORT_OPTIONS = new Set([
  'date_desc', 'date_asc', 'created_at_desc', 'created_at_asc',   'likes_desc', 'replies_desc',
  'engagement_desc', 'hot_desc',
  // 版块并入标题列后，排序入口只剩下拉，这里补上可选项
  'fid_asc', 'fid_desc',
])

/** 排序字段的中文名 */
const FIELD_LABEL: Record<string, string> = {
  date: '日期',
  created_at: '发布时间',
  title: '标题',
  author: '作者',
  fid: '版块',
  likes: '点赞',
  replies: '回复',
  engagement: '互动量',
  hot: '热度',
}

/** 当前排序的中文描述（摘要条与下拉的自定义项共用） */
const sortLabel = computed(() => {
  const { by, order } = colSort.value
  if (!order) return '日期倒序'
  return `${FIELD_LABEL[by] ?? by}${order === 'asc' ? '升序' : '降序'}`
})

/**
 * 下拉的显示值：由 colSort 反推。
 * 若是表头排出来的组合（如「标题升序」）没有对应预置项，则显示 custom，
 * 由 sortOptions 动态补一个「自定义：标题升序」选项，保证 v-model 始终有项可显示。
 */
const sortSelect = computed({
  get() {
    const { by, order } = colSort.value
    if (!order) return 'date_desc'
    const key = `${by}_${order}`
    return SORT_OPTIONS.has(key) ? key : 'custom'
  },
  set(v: string) {
    if (v === 'custom') return
    const i = v.lastIndexOf('_')
    colSort.value = { by: v.slice(0, i), order: v.slice(i + 1) as 'asc' | 'desc' }
    syncHeaderSort()
    page.value = 1
    load()
  },
})

const sortOptions = computed(() => {
  const base = [
    { value: 'date_desc', label: '日期倒序' },
    { value: 'date_asc', label: '日期正序' },
    { value: 'created_at_desc', label: '发布时间倒序' },
    { value: 'created_at_asc', label: '发布时间正序' },
    { value: 'likes_desc', label: '点赞数倒序' },
    { value: 'replies_desc', label: '回复数倒序' },
    { value: 'engagement_desc', label: '互动量倒序' },
    { value: 'hot_desc', label: '热度倒序' },
    { value: 'fid_asc', label: '版块升序' },
    { value: 'fid_desc', label: '版块降序' },
  ]
  if (sortSelect.value === 'custom') {
    base.unshift({ value: 'custom', label: `自定义：${sortLabel.value}` })
  }
  return base
})

/** 把 colSort 同步到表头高亮（Element Plus 不会自动同步下拉引起的变化） */
function syncHeaderSort() {
  const { by, order } = colSort.value
  if (!order || !SORTABLE_PROPS.has(by)) {
    tableRef.value?.clearSort()
    return
  }
  tableRef.value?.sort(by, order === 'asc' ? 'ascending' : 'descending')
}

/**
 * 表头三态排序：升序 → 降序 → 取消（取消即回到默认：日期倒序）。
 * 注意：syncHeaderSort 调 sort() 会反向触发 sort-change，故先比较，值没变就忽略，避免重复请求。
 */
function onSortChange({ prop, order }: { prop: string | null; order: 'ascending' | 'descending' | null }) {
  const next: { by: string; order: 'asc' | 'desc' } =
    order && prop
      ? { by: prop, order: order === 'ascending' ? 'asc' : 'desc' }
      : { by: 'date', order: 'desc' }
  if (next.by === colSort.value.by && next.order === colSort.value.order) return
  colSort.value = next
  page.value = 1
  load()
}

async function loadFidMeta() {
  try {
    fidMeta.value = await api.fidMeta()
  } catch (e) {
    ElMessage.error(`加载版块列表失败: ${(e as Error).message}`)
  }
}

/** 版块 fid → 中文名；未命中（版块元数据未加载或已下线）时退回 fid，避免出现空白列 */
function fidName(fid: string | number | null | undefined) {
  if (fid === null || fid === undefined || fid === '') return '-'
  return fidMeta.value.find((m) => String(m.fid) === String(fid))?.name ?? String(fid)
}

async function load() {
  loading.value = true
  try {
    pageData.value = await api.posts({
      fid: filters.fid.length ? filters.fid.join(',') : undefined,
      date_from: filters.dateRange?.[0],
      date_to: filters.dateRange?.[1],
      q: queryText.value || undefined,
      author: filters.author || undefined,
      page: page.value,
      page_size: pageSize.value,
      sort_by: colSort.value.order ? colSort.value.by : undefined,
      sort_order: colSort.value.order ?? undefined,
    })
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`查询失败: ${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function doSearch() {
  queryText.value = filters.q.trim()
  // 主动输入关键词即视为放弃「作者精确过滤」：否则下钻带来的 author 条件会残留，
  // 出现「换了关键词却仍被旧作者限制」的结果
  filters.author = ''
  page.value = 1
  load()
}

function doReset() {
  filters.fid = []
  filters.dateRange = null
  filters.q = ''
  filters.author = ''
  queryText.value = ''
  page.value = 1
  colSort.value = { by: 'date', order: 'desc' }
  syncHeaderSort()
  load()
}

/** 日期跨度天数（含首尾），用于条件条直观显示「共 N 天」 */
function dateRangeDays(from: string, to: string): number {
  const a = new Date(`${from}T00:00:00`).getTime()
  const b = new Date(`${to}T00:00:00`).getTime()
  if (Number.isNaN(a) || Number.isNaN(b)) return 0
  return Math.round((b - a) / 86400000) + 1
}

/** 近 N 日的起止日期（含今天），与数据总览活跃榜口径一致 */
function lastDays(days: number): [Date, Date] {
  const to = new Date()
  const from = new Date()
  from.setDate(from.getDate() - (days - 1))
  return [from, to]
}

const dateShortcuts = [
  { text: '近 7 日', value: () => lastDays(7) },
  { text: '近 30 日', value: () => lastDays(30) },
]

/**
 * 当前生效的查询条件汇总（顶部摘要条）。
 * 大屏下钻会同时带上作者/版块与时间范围，集中展示并支持逐项清除，
 * 避免用户对着筛选框猜测「现在究竟按什么在过滤」。
 */
const activeFilters = computed(() => {
  const list: { key: string; label: string; clear: () => void }[] = []
  if (filters.fid.length) {
    const names = filters.fid.map((f) => fidMeta.value.find((m) => m.fid === f)?.name ?? f)
    list.push({
      key: 'fid',
      label: `版块：${names.join('、')}`,
      clear: () => {
        filters.fid = []
        page.value = 1
        load()
      },
    })
  }
  if (filters.author) {
    list.push({
      key: 'author',
      label: `作者：${filters.author}（精确）`,
      clear: () => {
        filters.author = ''
        filters.q = ''
        queryText.value = ''
        page.value = 1
        load()
      },
    })
  } else if (queryText.value) {
    list.push({
      key: 'q',
      label: `关键词：${queryText.value}`,
      clear: () => {
        queryText.value = ''
        filters.q = ''
        page.value = 1
        load()
      },
    })
  }
  if (filters.dateRange) {
    const [from, to] = filters.dateRange
    list.push({
      key: 'date',
      label: `日期：${from} ~ ${to}（共 ${dateRangeDays(from, to)} 天）`,
      clear: () => {
        filters.dateRange = null
        page.value = 1
        load()
      },
    })
  }
  // 排序：仅非默认（日期倒序）时进摘要条，避免每条都显示噪音；清除即还原默认
  if (!(colSort.value.by === 'date' && colSort.value.order === 'desc')) {
    list.push({
      key: 'sort',
      label: `排序：${sortLabel.value}`,
      clear: () => {
        colSort.value = { by: 'date', order: 'desc' }
        syncHeaderSort()
        page.value = 1
        load()
      },
    })
  }
  return list
})

function onPageChange(p: number) {
  page.value = p
  load()
}

function onSizeChange(s: number) {
  pageSize.value = s
  page.value = 1
  load()
}

function openPost(url: string) {
  window.open(url, '_blank', 'noopener')
}

function doExport() {
  exporting.value = true
  try {
    const url = exportCsvUrl({
      fid: filters.fid.length ? filters.fid.join(',') : undefined,
      date_from: filters.dateRange?.[0],
      date_to: filters.dateRange?.[1],
      q: queryText.value || undefined,
      author: filters.author || undefined,
      sort_by: colSort.value.order ? colSort.value.by : undefined,
      sort_order: colSort.value.order ?? undefined,
    })
    window.open(url, '_blank', 'noopener')
  } finally {
    exporting.value = false
  }
}

function copyUrl(url: string) {
  navigator.clipboard
    .writeText(url)
    .then(() => ElMessage.success('链接已复制'))
    .catch(() => ElMessage.error('复制失败'))
}

/** 表格多选变化回调（模板中不直接赋值 ref，避免 ts-plugin 类型收窄误报） */
function onSelectionChange(rows: Post[]) {
  selectedRows.value = rows
}

/** 提交下载任务（单选/批量共用）：创建成功仅提示，进度在下载中心查看 */
async function submitDownload(urls: string[]) {
  if (!urls.length) {
    ElMessage.warning('请先选择要下载的链接')
    return
  }
  submitting.value = true
  try {
    const r = await api.submitDownload(urls)
    ElMessage.success(`已创建下载任务（${r.count} 个链接），可在下载中心查看进度`)
  } catch (e) {
    ElMessage.error(`创建下载任务失败: ${(e as Error).message}`)
  } finally {
    submitting.value = false
  }
}

/** 批量下载：提交当前勾选行的 URL */
function downloadSelected() {
  submitDownload(selectedRows.value.map((row) => row.url))
}

onMounted(() => {
  const qfid = route.query.fid
  if (typeof qfid === 'string' && qfid) {
    filters.fid = [qfid]
  }
  const qDateFrom = route.query.date_from
  const qDateTo = route.query.date_to
  if (typeof qDateFrom === 'string' && typeof qDateTo === 'string' && qDateFrom && qDateTo) {
    filters.dateRange = [qDateFrom, qDateTo]
  }
  // 兼容大屏下钻链接（形如 /posts?sort=engagement_desc）：映射到统一的 colSort
  const qsort = route.query.sort
  if (typeof qsort === 'string' && SORT_OPTIONS.has(qsort)) {
    const i = qsort.lastIndexOf('_')
    colSort.value = { by: qsort.slice(0, i), order: qsort.slice(i + 1) as 'asc' | 'desc' }
  }
  const qauthor = route.query.author
  if (typeof qauthor === 'string' && qauthor) {
    filters.author = qauthor
    // 作者数据适配到关键词输入框：回填作者名，使关键词（标题/作者）搜索立即生效
    filters.q = qauthor
    queryText.value = qauthor
  }
  loadFidMeta()
  load()
})
</script>

<template>
  <div>
    <!-- 筛选栏 -->
    <div class="page-card filter-bar">
      <div class="filter-row">
        <div class="filter-item">
          <span class="filter-label">版块</span>
          <el-select
            v-model="filters.fid"
            multiple
            collapse-tags
            collapse-tags-tooltip
            clearable
            placeholder="全部版块"
            style="width: 220px"
          >
            <el-option
              v-for="f in fidMeta"
              :key="f.fid"
              :label="`${f.name}(${f.fid}) · ${f.count.toLocaleString()}条`"
              :value="f.fid"
            />
          </el-select>
        </div>
        <div class="filter-item">
          <span class="filter-label">日期</span>
          <el-date-picker
            v-model="filters.dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            range-separator="至"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            :shortcuts="dateShortcuts"
            style="width: 250px"
          />
        </div>
        <div class="filter-item grow">
          <span class="filter-label">关键词</span>
          <el-input
            v-model="filters.q"
            placeholder="搜索标题/作者（模糊匹配）"
            clearable
            @keyup.enter="doSearch"
            @clear="doSearch"
          >
            <template #append>
              <el-button :icon="Search" @click="doSearch" />
            </template>
          </el-input>
        </div>
        <el-button @click="doReset">重置</el-button>
      </div>
      <!-- 当前查询条件：大屏下钻会带上作者/版块与时间范围，集中展示且可逐项清除 -->
      <div v-if="activeFilters.length" class="active-filters">
        <span class="af-label">当前条件</span>
        <el-tag
          v-for="f in activeFilters"
          :key="f.key"
          closable
          size="small"
          type="info"
          @close="f.clear"
        >
          {{ f.label }}
        </el-tag>
        <el-button link type="primary" size="small" @click="doReset">清空全部</el-button>
      </div>
    </div>

    <!-- 列表 -->
    <div class="page-card" style="margin-top: 16px">
      <div class="toolbar">
        <span>
          共 <b>{{ pageData?.total?.toLocaleString() ?? '-' }}</b> 条记录
        </span>
        <div class="toolbar-right">
          <!-- 与表头排序共用 colSort；表头排出的组合（如标题升序）会动态多出「自定义：…」项 -->
          <el-select v-model="sortSelect" style="width: 168px">
            <el-option v-for="o in sortOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-button
            type="success"
            :icon="Download"
            :disabled="!selectedRows.length"
            :loading="submitting"
            @click="downloadSelected"
          >
            批量下载{{ selectedRows.length ? `（${selectedRows.length}）` : '' }}
          </el-button>
          <el-button type="primary" :loading="exporting" @click="doExport">
            <el-icon style="margin-right: 4px"><Download /></el-icon>
            导出 CSV
          </el-button>
        </div>
      </div>

      <!-- 列排序用 custom 模式：分页列表必须走服务端排序，本地排序只作用于当前页，等于没排。
           三态由 el-table 原生提供（升序 → 降序 → 取消），顺序见 sort-orders -->
      <div ref="tableBoxRef" style="margin-top: 12px">
      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="pageData?.items ?? []"
        size="default"
        empty-text="暂无数据"
        style="width: 100%"
        @sort-change="onSortChange"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="42" />
        <!-- 版块已合并进标题列（高亮标签前置），原版块列的 100px 让给标题 -->
        <el-table-column prop="title" label="标题" min-width="520" sortable="custom">
          <template #default="{ row }">
            <div class="title-cell">
              <span class="fid-chip" :title="fidName(row.fid)">{{ fidName(row.fid) }}</span>
              <a class="title-link" :title="rowTip(row)" @click.prevent="openPost(row.url)">{{ row.title }}</a>
            </div>
          </template>
        </el-table-column>
        <el-table-column v-if="colVisible.likes" prop="likes" label="点赞" min-width="66" align="center" sortable="custom">
          <template #default="{ row }">{{ row.likes || '-' }}</template>
        </el-table-column>
        <el-table-column v-if="colVisible.author" prop="author" label="作者" min-width="76" sortable="custom" show-overflow-tooltip>
          <template #default="{ row }">{{ row.author || '-' }}</template>
        </el-table-column>
        <el-table-column v-if="colVisible.replies" prop="replies" label="回复" min-width="66" align="center" sortable="custom">
          <template #default="{ row }">{{ row.replies || '-' }}</template>
        </el-table-column>
        <el-table-column v-if="colVisible.time" prop="created_at" label="发布时间" width="96" sortable="custom">
          <template #default="{ row }">
            <el-tooltip :content="formatFullTime(row.created_at)" placement="top">
              <span class="text-muted">{{ formatRelativeTime(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="104" align="center" class-name="op-col">
          <template #default="{ row }">
            <div class="op-btns">
              <el-tooltip content="打开" placement="top">
                <el-button link type="primary" :icon="View" @click="openPost(row.url)" />
              </el-tooltip>
              <el-tooltip content="复制链接" placement="top">
                <el-button link :icon="CopyDocument" @click="copyUrl(row.url)" />
              </el-tooltip>
              <el-tooltip content="下载" placement="top">
                <el-button link type="success" :icon="Download" @click="submitDownload([row.url])" />
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
      </el-table>
      </div>

      <div class="pager">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next, jumper"
          :total="pageData?.total ?? 0"
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[20, 50, 100, 200]"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  flex-wrap: wrap;
}

/* 当前查询条件摘要条：大屏下钻会带上作者/版块与时间范围，集中展示且可逐项清除 */
.active-filters {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  width: 100%;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #ebeef5;
}

.af-label {
  color: #909399;
  font-size: 12px;
  white-space: nowrap;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  width: 100%;
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 8px;
}

.filter-item.grow {
  flex: 1;
  min-width: 220px;
}

.filter-label {
  color: #606266;
  font-size: 13px;
  white-space: nowrap;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.pager {
  margin-top: 14px;
  display: flex;
  justify-content: flex-end;
}

/* 操作列：三个图标按钮单行不换行，消除相邻按钮默认 12px 间距 */
.op-btns {
  display: inline-flex;
  align-items: center;
  white-space: nowrap;
}

.op-btns :deep(.el-button + .el-button) {
  margin-left: 0;
}

/* 表头：列标题与排序箭头必须同行显示。
   Element Plus 默认允许 .cell 换行，窄列下箭头会被挤到第二行，
   导致表头高度不齐、点击排序时整表布局跳动。
   业界表格（Ant Design / GitHub / Jira）的排序指示器一律与列标题同行。 */
:deep(.el-table th.el-table__cell > .cell) {
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
  overflow: hidden;
  /* 比默认的 12px 收窄，给窄列（点赞 / 回复）的「文字 + 箭头」腾出空间 */
  padding-left: 6px;
  padding-right: 6px;
}

/* 居中列（点赞 / 回复）改成 flex 后，需要 justify-content 才能保持居中 */
:deep(.el-table th.is-center > .cell) {
  justify-content: center;
}

/* 箭头不参与压缩，否则窄列下会被挤变形 */
:deep(.el-table th.el-table__cell > .cell .caret-wrapper) {
  flex-shrink: 0;
}

/* 标题单元格：版块标签 + 标题同行。标签固定不压缩，标题超长省略，
   两者用 flex 排版避免标题长短不一时标签位置跳动 */
.title-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  overflow: hidden;
}

/* 版块标签：高亮色 chip，比原先的灰色 tag 更醒目，作为标题的前缀标识 */
.fid-chip {
  flex-shrink: 0;
  max-width: 104px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 7px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 18px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border: 1px solid var(--el-color-primary-light-7);
}

.title-cell .title-link {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
