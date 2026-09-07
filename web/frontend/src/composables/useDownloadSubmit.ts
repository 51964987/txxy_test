import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, isAborted } from '../api'
import { briefList } from '../utils/text'

/** 通知钩子：数据总览真全屏时 ElMessage / ElMessageBox 挂在 body 上不可见，
 *  需先退出全屏再提示；不传则默认直接用 ElMessage（帖子浏览等常规页面）。 */
export interface DownloadSubmitNotify {
  success?: (msg: string) => void | Promise<void>
  error?: (msg: string) => void | Promise<void>
  warning?: (msg: string) => void | Promise<void>
  /** 弹确认框前的准备（数据总览真全屏时先退出全屏，否则确认框不可见） */
  beforeDialog?: () => void | Promise<void>
}

/** 提交下载任务的统一入口（帖子浏览 / 数据总览共用，与下载中心页内提交同一套 D2 判重交互）：
 *  防连点（submitting 守卫）→ check-dup 三分类 → 正在下载中的剔除（避免并发写同一文件）→
 *  文件仍在/已不在的确认弹窗 → 提交。创建成功仅提示，进度在下载中心查看。 */
export function useDownloadSubmit() {
  /** 提交中标志：供按钮 loading 绑定，同时在函数入口拦截连点 */
  const submitting = ref(false)

  async function submitDownload(urls: string[], notify?: DownloadSubmitNotify) {
    const okMsg = notify?.success ?? ((m: string) => ElMessage.success(m))
    const errMsg = notify?.error ?? ((m: string) => ElMessage.error(m))
    const warnMsg = notify?.warning ?? ((m: string) => ElMessage.warning(m))
    if (!urls.length) {
      await warnMsg('请先选择要下载的链接')
      return
    }
    // 防连点：上一次提交还在进行中（含等待确认弹窗）时忽略本次点击
    if (submitting.value) return
    submitting.value = true
    let pending = urls
    try {
      const dup = await api.checkDownloadDup(urls)
      // 1) 正在下载中的链接直接剔除，避免两个任务并发写同一文件
      const drop = new Set(dup.running)
      if (drop.size) {
        pending = pending.filter((u) => !drop.has(u))
        await warnMsg(`已移除 ${drop.size} 个正在下载中的链接，避免同一文件被并发写入`)
        if (!pending.length) {
          await warnMsg('所选链接均已在下载中，未重复提交')
          return
        }
      }
      // 2) 历史下载过的链接：文件「仍在」与「已不在」后果不同，弹窗说清楚后再提交
      const alive = dup.still_exists.filter((u) => !drop.has(u))
      const gone = dup.gone.filter((u) => !drop.has(u))
      if (alive.length || gone.length) {
        if (notify?.beforeDialog) await notify.beforeDialog()
        const lines: string[] = []
        if (alive.length) {
          lines.push(
            `<b>${alive.length} 个链接文件仍在</b>，提交后会跳过（不重复下载）：<br>${briefList(alive)}`,
          )
        }
        if (gone.length) {
          lines.push(
            `<b>${gone.length} 个链接曾下载过但文件已不在</b>，提交后会重新下载：<br>${briefList(gone)}`,
          )
        }
        const go = await ElMessageBox.confirm(lines.join('<br><br>'), '重复提交提醒', {
          type: 'warning',
          dangerouslyUseHTMLString: true,
          confirmButtonText: '仍要提交',
          cancelButtonText: '取消',
        })
          .then(() => true)
          .catch(() => false)
        if (!go) return
      }
      const r = await api.submitDownload(pending)
      await okMsg(`已创建下载任务（${r.count} 个链接），可在下载中心查看进度`)
    } catch (e) {
      if (isAborted(e)) return
      await errMsg(`创建下载任务失败: ${(e as Error).message}`)
    } finally {
      submitting.value = false
    }
  }

  return { submitting, submitDownload }
}
