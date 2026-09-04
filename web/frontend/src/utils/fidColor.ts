/**
 * 版块配色——全站唯一来源。
 *
 * 用途：数据总览的「活跃版块」横向条形图、分版块趋势折线、帖子浏览的版块标签，
 * 三处共用同一套映射，保证同一个版块在任何页面、任何刷新后都是同一个颜色。
 *
 * 关键：**按 fid 取模，而不是按排名索引取色**。
 * 按索引取色时，榜单顺序一变（换统计口径、数据更新）颜色就跟着变，
 * 同一版块在两张图里会对不上——这正是颜色语义化要避免的问题。
 * 按 fid 取模是确定性的：只要 fid 不变，颜色就不变。
 *
 * 色板取 13 色（与常见版块数量同量级），相邻色相/明度差异足够大，
 * 条形图并列时不易混淆；顺序经过挑选，前几色也是最常出现的版块。
 */
export const FID_PALETTE = [
  '#2f6fed', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444',
  '#06b6d4', '#ec4899', '#f97316', '#84cc16', '#14b8a6',
  '#6366f1', '#eab308', '#0ea5e9',
]

/** fid → 颜色（确定性；fid 缺失或非数字时回退到色板首色） */
export function colorForFid(fid: string | number | null | undefined): string {
  const n = typeof fid === 'number' ? Math.trunc(fid) : parseInt(String(fid ?? ''), 10)
  const idx = Number.isFinite(n) && n > 0 ? n % FID_PALETTE.length : 0
  return FID_PALETTE[idx]
}

/**
 * 按排名/索引取色（用于没有稳定 ID 的维度，如作者榜）。
 *
 * 复用同一套 FID_PALETTE，不另建作者色板：两套色板色值重合但顺序不同时，
 * 同一色值在两图里代表的排名不一致（如 #6366f1 在版块色板排第 11、在作者色板排第 1），
 * 读者会误以为颜色存在跨图对应关系。取模保证不越界，调用方无需再写兜底色。
 */
export function colorByIndex(i: number): string {
  const n = Number.isFinite(i) && i >= 0 ? Math.trunc(i) % FID_PALETTE.length : 0
  return FID_PALETTE[n]
}
