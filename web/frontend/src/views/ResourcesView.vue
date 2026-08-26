<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, formatSize, isAborted, type Resources } from '../api'

const data = ref<Resources | null>(null)
const loading = ref(false)
const active = ref('') // 当前展开的文件夹

const totalSizeText = computed(() => formatSize(data.value?.total_size ?? 0))

const categoryMeta: Record<string, { label: string; type: string }> = {
  image: { label: '图片', type: 'primary' },
  video: { label: '视频', type: 'success' },
  torrent: { label: '种子', type: 'warning' },
  text: { label: '文本', type: 'info' },
  other: { label: '其他', type: 'info' },
}

async function load() {
  loading.value = true
  try {
    data.value = await api.resources()
  } catch (e) {
    if (isAborted(e)) return
    ElMessage.error(`加载资源失败: ${(e as Error).message}`)
  } finally {
    loading.value = false
  }
}

function toggle(name: string) {
  active.value = active.value === name ? '' : name
}

function copyPath(p: string) {
  navigator.clipboard
    .writeText(p)
    .then(() => ElMessage.success('路径已复制'))
    .catch(() => ElMessage.error('复制失败'))
}

function fmtTime(ts: number): string {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <!-- 统计卡片 -->
    <div class="stat-grid">
      <div class="stat-card">
        <div class="stat-icon" style="background: #10b981">
          <el-icon><FolderOpened /></el-icon>
        </div>
        <div>
          <div class="stat-label">资源目录</div>
          <div class="stat-value">{{ data?.count ?? 0 }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #2f6fed">
          <el-icon><Files /></el-icon>
        </div>
        <div>
          <div class="stat-label">文件总数</div>
          <div class="stat-value">{{ data?.total_files ?? 0 }}</div>
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-icon" style="background: #f59e0b">
          <el-icon><Coin /></el-icon>
        </div>
        <div>
          <div class="stat-label">总大小</div>
          <div class="stat-value">{{ totalSizeText }}</div>
        </div>
      </div>
    </div>

    <!-- 资源列表 -->
    <div class="page-card">
      <el-empty v-if="data && data.count === 0" description="downloads/ 下暂无下载资源" />
      <template v-else>
        <div
          v-for="item in data?.items ?? []"
          :key="item.name"
          class="folder"
        >
          <div class="folder-head" @click="toggle(item.name)">
            <el-icon class="folder-arrow" :class="{ open: active === item.name }">
              <ArrowRight />
            </el-icon>
            <el-icon class="folder-icon"><Folder /></el-icon>
            <span class="folder-name">{{ item.name }}</span>
            <span class="text-muted folder-meta">
              {{ item.file_count }} 个文件 · {{ formatSize(item.total_size) }} · {{ fmtTime(item.mtime) }}
            </span>
            <el-button link type="primary" class="copy-btn" @click.stop="copyPath(item.name)">
              复制目录名
            </el-button>
          </div>

          <el-collapse-transition>
            <div v-show="active === item.name" class="folder-body">
              <el-table :data="item.files" size="small">
                <el-table-column prop="name" label="文件名" min-width="320" show-overflow-tooltip />
                <el-table-column label="类型" width="90">
                  <template #default="{ row }">
                    <el-tag size="small" :type="(categoryMeta[row.category]?.type as any) ?? 'info'">
                      {{ categoryMeta[row.category]?.label ?? '其他' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="大小" width="110">
                  <template #default="{ row }">{{ formatSize(row.size) }}</template>
                </el-table-column>
                <el-table-column label="相对路径" min-width="280" show-overflow-tooltip>
                  <template #default="{ row }">
                    <span class="text-muted">downloads/{{ row.rel_path }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="120">
                  <template #default="{ row }">
                    <el-button link @click="copyPath(row.rel_path)">复制路径</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </el-collapse-transition>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.folder {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  margin-bottom: 12px;
  overflow: hidden;
}

.folder-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 14px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s ease;
}

.folder-head:hover {
  background: #f6f8fc;
}

.folder-arrow {
  transition: transform 0.2s ease;
  color: #909399;
}

.folder-arrow.open {
  transform: rotate(90deg);
}

.folder-icon {
  color: #2f6fed;
}

.folder-name {
  font-weight: 600;
  color: #1f2d3d;
}

.folder-meta {
  font-size: 12px;
}

.copy-btn {
  margin-left: auto;
}

.folder-body {
  padding: 0 14px 12px;
  border-top: 1px dashed var(--app-border);
  background: #fbfcfe;
}
</style>
