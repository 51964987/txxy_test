<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
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
const sort = ref('date_desc')

const queryText = ref('')

// 下钻支持的合法 sort 选项（与模板 el-select 的 value 严格一致）
const SORT_OPTIONS = new Set([
  'date_desc', 'date_asc', 'created_at_desc', 'created_at_asc', 'likes_desc', 'replies_desc',
])

async function loadFidMeta() {
  try {
    fidMeta.value = await api.fidMeta()
  } catch (e) {
    ElMessage.error(`加载版块列表失败: ${(e as Error).message}`)
  }
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
      sort: sort.value,
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
  sort.value = 'date_desc'
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
      sort: sort.value,
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
  const qsort = route.query.sort
  if (typeof qsort === 'string' && SORT_OPTIONS.has(qsort)) {
    sort.value = qsort
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
          <el-select v-model="sort" style="width: 150px" @change="load">
            <el-option label="日期倒序" value="date_desc" />
            <el-option label="日期正序" value="date_asc" />
            <el-option label="发布时间倒序" value="created_at_desc" />
            <el-option label="发布时间正序" value="created_at_asc" />
            <el-option label="点赞数倒序" value="likes_desc" />
            <el-option label="回复数倒序" value="replies_desc" />
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

      <el-table
        v-loading="loading"
        :data="pageData?.items ?? []"
        size="default"
        empty-text="暂无数据"
        style="margin-top: 12px; width: 100%"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="45" />
        <el-table-column prop="title" label="标题" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <a class="title-link" @click.prevent="openPost(row.url)">{{ row.title }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="fid" label="版块" min-width="70">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ row.fid }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="likes" label="点赞" min-width="64" align="center">
          <template #default="{ row }">{{ row.likes || '-' }}</template>
        </el-table-column>
        <el-table-column prop="author" label="作者" min-width="90" show-overflow-tooltip>
          <template #default="{ row }">{{ row.author || '-' }}</template>
        </el-table-column>
        <el-table-column prop="replies" label="回复" min-width="64" align="center">
          <template #default="{ row }">{{ row.replies || '-' }}</template>
        </el-table-column>
        <el-table-column label="发布时间" width="110">
          <template #default="{ row }">
            <el-tooltip :content="formatFullTime(row.created_at)" placement="top">
              <span class="text-muted">{{ formatRelativeTime(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="132" align="center" class-name="op-col">
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
</style>
