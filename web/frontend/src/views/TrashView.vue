<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatSize, isAborted, type TrashItem } from '../api'

const items = ref<TrashItem[]>([])
const loading = ref(false)
const loadError = ref('')
const keepDays = ref(7)

// 搜索与排序均在前端完成：回收站条目量远小于资源清单，无需后端分页
const keyword = ref('')
const sortKey = ref<'deleted_at' | 'size' | 'name'>('deleted_at')
const sortOrder = ref<'asc' | 'desc'>('desc')

const totalSize = computed(() => items.value.reduce((s, i) => s + (i.size || 0), 0))
const expiredCount = computed(() => items.value.filter((i) => i.expired).length)

const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const filtered = items.value.filter(
    (i) => !kw || i.name.toLowerCase().includes(kw) || i.rel.toLowerCase().includes(kw),
  )
  const dir = sortOrder.value === 'asc' ? 1 : -1
  return [...filtered].sort((a, b) => {
    if (sortKey.value === 'size') return ((a.size || 0) - (b.size || 0)) * dir
    if (sortKey.value === 'name') return a.name.localeCompare(b.name, 'zh-Hans-CN') * dir
    return a.deleted_at.localeCompare(b.deleted_at) * dir
  })
})

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const r = await api.trashList()
    items.value = r.items
    keepDays.value = r.keep_days
  } catch (e) {
    if (isAborted(e)) return
    loadError.value = (e as Error).message
    ElMessage.error(`加载回收站失败: ${loadError.value}`)
  } finally {
    loading.value = false
  }
}

async function restoreItem(item: TrashItem) {
  try {
    await ElMessageBox.confirm(
      `恢复「${item.name}」到原位置？\n若原路径已存在同名文件会恢复失败。`,
      '恢复确认',
      { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.restoreResource(item.id)
    ElMessage.success('已恢复到原位置')
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`恢复失败: ${(e as Error).message}`)
  }
}

async function purgeItem(item: TrashItem) {
  try {
    await ElMessageBox.confirm(
      `彻底删除「${item.name}」？该操作不可恢复。`,
      '彻底删除确认',
      { type: 'error', confirmButtonText: '彻底删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.purgeResource(item.id)
    ElMessage.success('已彻底删除')
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`删除失败: ${(e as Error).message}`)
  }
}

async function purgeAll() {
  const count = items.value.length
  if (!count) return
  try {
    await ElMessageBox.confirm(
      `彻底删除回收站中全部 ${count} 项（${formatSize(totalSize.value)}）？该操作不可恢复。`,
      '清空回收站确认',
      { type: 'error', confirmButtonText: '全部彻底删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const r = await api.purgeResource('')
    ElMessage.success(`已彻底删除 ${r.count} 项`)
    await load()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`清空失败: ${(e as Error).message}`)
  }
}

function fmtTime(s: string): string {
  return (s || '').replace('T', ' ')
}

function toggleSort(key: 'deleted_at' | 'size' | 'name') {
  if (sortKey.value === key) {
    sortOrder.value = sortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    sortKey.value = key
    sortOrder.value = key === 'name' ? 'asc' : 'desc'
  }
}

function sortIcon(key: string): string {
  if (sortKey.value !== key) return ''
  return sortOrder.value === 'asc' ? ' ↑' : ' ↓'
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-card" style="margin-bottom: 16px">
      <div class="note">
        <el-icon style="margin-right: 6px"><InfoFilled /></el-icon>
        <span>
          资源删除后先移入回收站（<code>outputs/trash/</code>），默认保留
          <b>{{ keepDays }}</b> 天，期间可恢复；到期条目不会自动清理，需手动彻底删除或清空。
          彻底删除不可恢复，请谨慎操作。
        </span>
      </div>

      <div class="trash-stats">
        <div>
          <div class="stat-label">条目数</div>
          <div class="stat-value">{{ items.length }}</div>
        </div>
        <div>
          <div class="stat-label">占用空间</div>
          <div class="stat-value">{{ formatSize(totalSize) }}</div>
        </div>
        <div>
          <div class="stat-label">已过期</div>
          <div class="stat-value" :class="{ 'value-warn': expiredCount > 0 }">
            {{ expiredCount }}
          </div>
        </div>
        <div class="stat-card-toolbar">
          <el-button :icon="'Refresh'" :loading="loading" type="primary" plain @click="load">
            刷新
          </el-button>
        </div>
      </div>
    </div>

    <el-result
      v-if="loadError"
      icon="warning"
      title="加载失败"
      :sub-title="loadError"
    >
      <template #extra>
        <el-button type="primary" @click="load">重试</el-button>
      </template>
    </el-result>

    <div v-else class="page-card">
      <div class="toolbar">
        <el-input
          v-model="keyword"
          class="toolbar-search"
          placeholder="搜索名称 / 原路径"
          clearable
          :prefix-icon="'Search'"
        />
        <div class="sort-group">
          <span class="text-muted">排序：</span>
          <el-button size="small" @click="toggleSort('deleted_at')">
            删除时间{{ sortIcon('deleted_at') }}
          </el-button>
          <el-button size="small" @click="toggleSort('size')">大小{{ sortIcon('size') }}</el-button>
          <el-button size="small" @click="toggleSort('name')">名称{{ sortIcon('name') }}</el-button>
        </div>
        <el-button type="danger" plain :disabled="!items.length" @click="purgeAll">
          清空回收站
        </el-button>
      </div>

      <el-table :data="rows" style="width: 100%" empty-text="暂无匹配项">
        <el-table-column label="名称" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="name-cell">{{ row.name }}</span>
            <el-tag v-if="row.is_dir" size="small" class="type-tag">目录</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="rel" label="原路径" min-width="220" show-overflow-tooltip />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="删除时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.deleted_at) }}</template>
        </el-table-column>
        <el-table-column label="保留状态" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.expired" size="small" type="danger">已过期</el-tag>
            <span v-else class="text-muted">剩余 {{ row.remain_days }} 天</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button link type="primary" @click="restoreItem(row)">恢复</el-button>
            <el-button link type="danger" @click="purgeItem(row)">彻底删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-empty v-if="!items.length" description="回收站为空" />
    </div>
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

.trash-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}

.stat-card-toolbar {
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat-label {
  color: #909399;
  font-size: 13px;
}

.stat-value {
  font-size: 20px;
  font-weight: 600;
  color: #1f2d3d;
}

.value-warn {
  color: #e6a23c;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.toolbar-search {
  width: 280px;
  max-width: 100%;
}

.sort-group {
  display: flex;
  align-items: center;
  gap: 6px;
}

.name-cell {
  margin-right: 6px;
}

.type-tag {
  vertical-align: middle;
}

@media (max-width: 900px) {
  .trash-stats {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
