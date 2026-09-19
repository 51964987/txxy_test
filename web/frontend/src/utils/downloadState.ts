// 下载状态四态 → 标签展示（已沉淀 / 下载中 / 可重下），前端只上色。
// 与数据总览榜单行「已沉淀」状态标复用同一份映射，禁止两处各写一份（项目通用工程约束第 1 条）。
// 判据在后端（asset_snapshot 一处实现），前端不重复判定——只负责把枚举翻译成文案 / 颜色 / 说明。
import type { BoardItemState } from '../api'

export interface DownloadStateBadge {
  text: string
  type: 'success' | 'primary' | 'warning'
  tip: string
}

/** 非 fresh 三态的标签配置：配色由 el-tag 的 type 承载，文案与 tooltip 在此集中定义 */
export const DOWNLOAD_STATE_BADGE: Record<
  Exclude<BoardItemState, 'fresh'>,
  DownloadStateBadge
> = {
  downloaded: {
    text: '已沉淀',
    type: 'success',
    tip: '文件已在本地（行内下载按钮仍可提交，判重会拦截已存在的文件）',
  },
  running: {
    text: '下载中',
    type: 'primary',
    tip: '下载任务进行中，可在下载中心查看进度',
  },
  re_download: {
    text: '可重下',
    type: 'warning',
    tip: '曾下载过但文件已被清理，可重新下载',
  },
}

/** 取下载状态标配置；fresh（从未下载，默认态）/ 缺省返回 undefined（不渲染标签，避免满屏标签噪音） */
export function stateBadge(state?: BoardItemState) {
  return state && state !== 'fresh' ? DOWNLOAD_STATE_BADGE[state] : undefined
}
