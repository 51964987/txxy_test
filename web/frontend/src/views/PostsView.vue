<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { CopyDocument, Download, Search, View } from '@element-plus/icons-vue'
import { api, exportCsvUrl, isAborted, type FidMeta, type PostsPage } from '../api'
import { formatRelativeTime } from '../utils/time'

const route = useRoute()

const fidMeta = ref<FidMeta[]>([])
const pageData = ref<PostsPage | null>(null)
const loading = ref(false)
const exporting = ref(false)

const filters = reactive({
  fid: [] as string[],
  dateRange: null as [string, string] | null,
  q: '',
})
const page = ref(1)
const pageSize = ref(50)
const sort = ref('date_desc')

const queryText = ref('')

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
  page.value = 1
  load()
}

function doReset() {
  filters.fid = []
  filters.dateRange = null
  filters.q = ''
  queryText.value = ''
  page.value = 1
  sort.value = 'date_desc'
  load()
}

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
            style="width: 250px"
          />
        </div>
        <div class="filter-item grow">
          <span class="filter-label">关键词</span>
          <el-input
            v-model="filters.q"
            placeholder="搜索标题（模糊匹配）"
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
            <el-option label="入库时间倒序" value="created_at_desc" />
            <el-option label="入库时间正序" value="created_at_asc" />
            <el-option label="点赞数倒序" value="likes_desc" />
            <el-option label="回复数倒序" value="replies_desc" />
          </el-select>
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
      >
        <el-table-column prop="title" label="标题" min-width="420" show-overflow-tooltip>
          <template #default="{ row }">
            <a class="title-link" @click.prevent="openPost(row.url)">{{ row.title }}</a>
          </template>
        </el-table-column>
        <el-table-column prop="fid" label="版块" min-width="80">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ row.fid }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="likes" label="点赞" min-width="70" align="center">
          <template #default="{ row }">{{ row.likes || '-' }}</template>
        </el-table-column>
        <el-table-column prop="author" label="作者" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.author || '-' }}</template>
        </el-table-column>
        <el-table-column prop="replies" label="回复" min-width="70" align="center">
          <template #default="{ row }">{{ row.replies || '-' }}</template>
        </el-table-column>
        <el-table-column label="抓取时间" width="130">
          <template #default="{ row }">
            <el-tooltip :content="row.created_at || '-'" placement="top">
              <span class="text-muted">{{ formatRelativeTime(row.created_at) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="90" align="center">
          <template #default="{ row }">
            <el-tooltip content="打开" placement="top">
              <el-button link type="primary" :icon="View" @click="openPost(row.url)" />
            </el-tooltip>
            <el-tooltip content="复制链接" placement="top">
              <el-button link :icon="CopyDocument" @click="copyUrl(row.url)" />
            </el-tooltip>
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
</style>
