// 相对时间：x 分钟前 / x 小时前 / x 天前
export function formatRelativeTime(raw?: string): string {
  if (!raw) return '-'
  const s = String(raw).replace('T', ' ')
  const [datePart = '', timePart = '00:00:00'] = s.split(' ')
  const t = new Date(`${datePart}T${timePart}`)
  if (Number.isNaN(t.getTime())) return s.slice(0, 19)
  const diffMs = Date.now() - t.getTime()
  const min = Math.floor(diffMs / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d} 天前`
  return s.slice(0, 10)
}
