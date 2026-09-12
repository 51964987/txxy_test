/**
 * 资源类型（媒体类型）元数据——全站唯一来源。
 *
 * 用途：资源管理页的类型筛选 / 文件标签、首页内容资产卡「按类型」占比条与图例、
 * B6 容量洞察的类型分布，三处共用同一套分类、标签与颜色，保证同一类型在任何页面
 * 颜色与文案一致。分类口径来自后端 resources.category_of() 的五大类，禁止再写第二份。
 */

/** 媒体类型键（与后端 category_of 返回值严格一致） */
export type CategoryKey = 'image' | 'video' | 'torrent' | 'text' | 'other'

/** 类型展示顺序：图 / 视频 / 种子 / 文本 / 其他 */
export const CATEGORY_ORDER: CategoryKey[] = ['image', 'video', 'torrent', 'text', 'other']

/** 类型 → 中文标签 + Element Plus Tag 语义色 + 可选口径说明（悬浮 tooltip 用） */
export const categoryMeta: Record<string, { label: string; type: string; desc?: string }> = {
  image: { label: '图片', type: 'primary' },
  video: { label: '视频', type: 'success' },
  torrent: { label: '种子', type: 'warning' },
  // 口径说明：项目用 txt_export 把磁力链接 / 云盘清单导出为 .txt，归到此类，悬浮提示避免误解为「只是说明文档」
  text: { label: '文本', type: 'info', desc: '磁力链接、云盘清单等 TXT 导出（.txt / .md / .log）' },
  other: { label: '其他', type: 'info' },
}

/**
 * 类型分布色板（与统计 Tag 语义对应）：
 * 图片/视频/种子沿 fidColor 同族蓝/绿/橙，文本/其他用中性灰以示「非媒体主体」。
 */
export const categoryColors: Record<string, string> = {
  image: '#2f6fed',
  video: '#10b981',
  torrent: '#f59e0b',
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
