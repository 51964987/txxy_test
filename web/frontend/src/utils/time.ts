// 时间展示工具：兼容旧 ISO 字符串与新的 Unix 秒时间戳两种入库形态

// 判定是否为 Unix 秒时间戳形态（6 位以上纯十进制数字串；
// 源站个别超老帖 data-timestamp 为 1970 年代的小值如 960041，需一并识别）
function isUnixSeconds(s: string): boolean {
  return /^\d{6,}$/.test(s)
}

// 二位补零
function pad(n: number): string {
  return String(n).padStart(2, '0')
}

// 相对时间主体：刚刚 / x 分钟前 / x 小时前 / x 天前，超过一周回落为 YYYY-MM-DD
function relativeFrom(d: Date, fallback: string): string {
  if (Number.isNaN(d.getTime())) return fallback
  const diffMs = Date.now() - d.getTime()
  const min = Math.floor(diffMs / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const h = Math.floor(min / 60)
  if (h < 24) return `${h} 小时前`
  const day = Math.floor(h / 24)
  if (day < 7) return `${day} 天前`
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// 相对时间：x 分钟前 / x 小时前 / x 天前（双格式兼容）
export function formatRelativeTime(raw?: string | null): string {
  if (!raw) return '-'
  const s = String(raw)
  if (isUnixSeconds(s)) {
    return relativeFrom(new Date(Number(s) * 1000), '-')
  }
  const [datePart = '', timePart = '00:00:00'] = s.replace('T', ' ').split(' ')
  return relativeFrom(new Date(`${datePart}T${timePart}`), s.slice(0, 19))
}

// 完整时间展示（tooltip 等）：'YYYY-MM-DD HH:mm:ss'
export function formatFullTime(raw?: string | null): string {
  if (!raw) return '-'
  const s = String(raw)
  if (isUnixSeconds(s)) {
    const d = new Date(Number(s) * 1000)
    if (Number.isNaN(d.getTime())) return '-'
    return (
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
      + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    )
  }
  return s.replace('T', ' ').slice(0, 19)
}

// 短时刻展示（HH:MM）：空/非法返回 null，由调用方决定占位文案
export function formatShortTime(raw?: string | null): string | null {
  if (!raw) return null
  const s = String(raw)
  let d: Date
  if (isUnixSeconds(s)) {
    d = new Date(Number(s) * 1000)
  } else {
    const [datePart = '', timePart = '00:00:00'] = s.replace('T', ' ').split(' ')
    d = new Date(`${datePart}T${timePart}`)
  }
  if (Number.isNaN(d.getTime())) return null
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}
