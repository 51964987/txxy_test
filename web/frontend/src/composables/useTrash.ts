/**
 * 回收站数据与操作（TrashView 表格版 与 ResourcesView 抽屉版 共用）。
 *
 * 历史问题：两个入口各写了一份 load / restoreItem / purgeItem / purgeAll，
 * 约 90% 逐行相同，且**已经出现行为漂移**：
 *   - ResourcesView 的「恢复」没有二次确认，TrashView 有；
 *   - 「清空回收站」确认文案：TrashView 带占用总量（formatSize），ResourcesView 不带；
 *   - 操作后的刷新范围不一致（ResourcesView 还要刷新资源列表）。
 * 改一处漏一处就会出现「同一个回收站，两个入口行为不同」，故收敛为同一份实现；
 * 差异（额外刷新）通过 onChanged 回调表达。
 */
import { computed, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, formatSize, isAborted, type TrashItem } from '../api'

interface UseTrashOptions {
  /** 回收站内容变化后的额外刷新（如 ResourcesView 需同步刷新资源列表） */
  onChanged?: () => void | Promise<void>
}

export function useTrash(options: UseTrashOptions = {}) {
  const items = ref<TrashItem[]>([])
  const keepDays = ref(7)
  const loading = ref(false)
  const error = ref('')

  const totalSize = computed(() => items.value.reduce((s, i) => s + (i.size || 0), 0))
  const expiredCount = computed(() => items.value.filter((i) => i.expired).length)

  async function load() {
    loading.value = true
    error.value = ''
    try {
      const r = await api.trashList()
      items.value = r.items
      keepDays.value = r.keep_days || 7
    } catch (e) {
      if (isAborted(e)) return
      error.value = (e as Error).message
      ElMessage.error(`加载回收站失败: ${error.value}`)
    } finally {
      loading.value = false
    }
  }

  /** 操作成功后统一走这里：刷新回收站，再回调调用方做额外刷新 */
  async function refresh() {
    await load()
    await options.onChanged?.()
  }

  async function restoreItem(item: TrashItem) {
    try {
      await ElMessageBox.confirm(
        `恢复「${item.name}」到原位置？\n若原路径已存在同名文件会恢复失败。`,
        '恢复确认',
        { type: 'warning', confirmButtonText: '恢复', cancelButtonText: '取消' },
      )
    } catch {
      return // 用户取消
    }
    try {
      await api.restoreResource(item.id)
      ElMessage.success('已恢复到原位置')
      await refresh()
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
      return // 用户取消
    }
    try {
      await api.purgeResource(item.id)
      ElMessage.success('已彻底删除')
      await refresh()
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
      return // 用户取消
    }
    try {
      const r = await api.purgeResource('')
      ElMessage.success(`已彻底删除 ${r.count} 项`)
      await refresh()
    } catch (e) {
      if (isAborted(e)) return
      ElMessage.error(`清空失败: ${(e as Error).message}`)
    }
  }

  return {
    items,
    keepDays,
    loading,
    error,
    totalSize,
    expiredCount,
    load,
    restoreItem,
    purgeItem,
    purgeAll,
  }
}
