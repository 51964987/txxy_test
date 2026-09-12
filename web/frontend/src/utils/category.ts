/**
 * 资源类型（媒体类型）元数据——全站唯一来源。
 *
 * 用途：资源管理页的类型筛选 / 文件标签、首页内容资产卡「按类型」占比条与图例、
 * B6 容量洞察的类型分布，三处共用同一套分类、标签与颜色，保证同一类型在任何页面
 * 颜色与文案一致。分类口径来自后端 resources.category_of() 的七大类
 * （image/video/torrent/magnet/cloud/text/other），禁止再写第二份。
 */

import type { Assets } from '../api'

/** 媒体类型键（与后端 category_of 返回值严格一致） */
export type CategoryKey = 'image' | 'video' | 'torrent' | 'magnet' | 'cloud' | 'text' | 'other'

/** 类型展示顺序：图 / 视频 / 种子 / 磁力链接 / 云盘清单 / 文本 / 其他 */
export const CATEGORY_ORDER: CategoryKey[] = [
  'image',
  'video',
  'torrent',
  'magnet',
  'cloud',
  'text',
  'other',
]

/** 类型 → 中文标签 + Element Plus Tag 语义色 + 可选口径说明（悬浮 tooltip 用） */
export const categoryMeta: Record<string, { label: string; type: string; desc?: string }> = {
  image: { label: '图片', type: 'primary' },
  video: { label: '视频', type: 'success' },
  torrent: { label: '种子', type: 'warning' },
  // 磁力链接清单：抓取端 extract_magnets 导出为 magnets.txt（每行一条 magnet: 地址）
  magnet: { label: '磁力链接', type: 'info', desc: '磁力链接清单 magnets.txt（每行一条 magnet: 地址）' },
  // 云盘清单：抓取端 extract_clouds 导出为 clouds.txt（夸克 / 百度 / 迅雷等网盘地址）
  cloud: { label: '云盘清单', type: 'info', desc: '云盘链接清单 clouds.txt（夸克 / 百度 / 迅雷等）' },
  // 其余文本文件（非清单）：说明文档等
  text: { label: '文本', type: 'info', desc: '其它文本文件（.txt / .md / .log，非磁力/云盘清单）' },
  other: { label: '其他', type: 'info' },
}

/**
 * 类型分布色板（与统计 Tag 语义对应）：
 * 图片/视频/种子沿 fidColor 同族蓝/绿/橙；文本类（磁力/云盘/文本）用紫/青/灰区分且整体偏冷以示「非媒体主体」。
 */
export const categoryColors: Record<string, string> = {
  image: '#2f6fed',
  video: '#10b981',
  torrent: '#f59e0b',
  magnet: '#a855f7',
  cloud: '#14b8a6',
  text: '#909399',
  other: '#c0c4cc',
}

/** 类型筛选选项（含「全部」；资源管理页 segmented / select 共用，避免第二处硬编码） */
export const categoryOptions = [
  { label: '全部', value: 'all' },
  ...CATEGORY_ORDER.map((k) => ({ label: categoryMeta[k].label, value: k })),
]

/** 类型枚举 → 中文标签（找不到回退原值） */
export function categoryLabel(c: string): string {
  return (categoryMeta as Record<string, { label: string }>)[c]?.label ?? c
}

/**
 * 由后端 type_breakdown 派生「按类型」占比段。
 * R4（内容资产卡）与 B6（资源管理·容量洞察）共用同一实现，杜绝两边各算一遍导致
 * 口径 / 精度 / 顺序不一致（此前 B6 自行遍历文件聚合，属重复轮子，已移除）。
 * - sizePct：该类型体积占全库体积的比例（占比条宽度用，2 位小数）
 * - filePct：该类型文件数占全库文件数的比例（图例辅助参考，2 位小数）
 * - 仅保留文件数 > 0 的类型，按 CATEGORY_ORDER 固定顺序输出
 */
export interface TypeSegment {
  key: CategoryKey
  label: string
  color: string
  files: number
  size: number
  sizePct: number
  sizePctText: string
  filePct: number
}

export function buildTypeSegments(a: Assets | null): TypeSegment[] {
  if (!a || !a.type_breakdown) return []
  const tb = a.type_breakdown
  const totalSize = a.size || 0
  const totalFiles = a.files || 0
  return CATEGORY_ORDER.map((key) => {
    const item = tb[key]
    const files = item?.files ?? 0
    const size = item?.size ?? 0
    return {
      key,
      label: categoryMeta[key]?.label ?? key,
      color: categoryColors[key] ?? '#c0c4cc',
      files,
      size,
      sizePct: totalSize ? Number(((size / totalSize) * 100).toFixed(2)) : 0,
      sizePctText: totalSize ? `${((size / totalSize) * 100).toFixed(2)}%` : '0.00%',
      filePct: totalFiles ? Number(((files / totalFiles) * 100).toFixed(2)) : 0,
    }
  }).filter((r) => r.files > 0)
}
