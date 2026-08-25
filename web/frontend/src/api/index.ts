const BASE = '/api'

async function get<T>(path: string, params?: Record<string, string | number | undefined | null>): Promise<T> {
  const qs = new URLSearchParams()
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
    }
  }
  const s = qs.toString()
  const res = await fetch(BASE + path + (s ? `?${s}` : ''))
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`
    try {
      const j = await res.json()
      if (j && j.detail) msg = String(j.detail)
    } catch {
      /* ignore */
    }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

export interface Post {
  title: string
  fid: string
  date: string
  url: string
  likes: string
  author: string
  replies: string
  created_at: string
  update_at: string
  update_date: string
}

export interface FidMeta {
  fid: string
  name: string
  count: number
  latest_date: string | null
}

export interface BoardTop {
  fid: string
  name: string
  title: string
  url: string
  value: string
}

export interface Overview {
  total: number
  today: number
  yesterday: number
  week_new: number
  latest_created_at: string | null
  latest_date: string | null
  today_str: string
  total_users: number
  active_users: number
}

export interface Boards {
  top_likes: BoardTop[]
  top_replies: BoardTop[]
}

export interface TrendPoint {
  date: string
  count: number
}

export interface TrendByFid {
  dates: string[]
  series: { fid: number; name: string; data: number[] }[]
}

export interface FidDistItem {
  fid: string
  name: string
  count: number
  latest_date?: string | null
  today_count?: number
  yesterday_count?: number
}

export interface AppConfig {
  enable_auto_refresh: boolean
}

export interface PostsPage {
  total: number
  page: number
  page_size: number
  items: Post[]
}

export interface RunSummary {
  id?: number
  date: string
  dir: string
  time?: string
  source: string
  status: 'running' | 'ok' | 'cancelled' | 'error'
  ok: number
  fail: number
  skip: number
  csv: number
  sqlite: number
  duration?: number | null
  /** 实时进度百分比 0-100；running 状态时实时聚合，已结束为 100 */
  progress?: number | null
}

export interface RunSection {
  fid: string
  name: string
  status: 'running' | 'ok' | 'fail' | 'skip' | string
  csv: number
  sqlite: number
  duration?: number | null
  total_pages?: number
  current_page?: number
  progress?: number
}

export interface RunDetail {
  id?: number
  date: string
  dir: string
  time?: string
  source: 'run_batch' | 'scraper'
  status: 'running' | 'ok' | 'cancelled' | 'error'
  overall?: { ok: number; fail: number; skip: number }
  total: { csv: number; sqlite: number }
  progress?: number | null
  sections: RunSection[]
}

export interface ResourceFile {
  name: string
  rel_path: string
  size: number
  category: 'image' | 'video' | 'torrent' | 'text' | 'other'
}

export interface ResourceItem {
  name: string
  file_count: number
  total_size: number
  mtime: number
  files: ResourceFile[]
}

export interface Resources {
  count: number
  total_files: number
  total_size: number
  items: ResourceItem[]
}

export const api = {
  config: () => get<AppConfig>('/config'),
  overview: () => get<Overview>('/stats/overview'),
  boards: () => get<Boards>('/stats/boards'),
  trend: (days: number) => get<TrendPoint[]>('/stats/trend', { days }),
  trendByFid: (days: number, top = 8) =>
    get<TrendByFid>('/stats/trend_by_fid', { days, top }),
  fidDist: () => get<FidDistItem[]>('/stats/fid_dist'),
  recent: (limit = 10) => get<Post[]>('/stats/recent', { limit }),
  fidMeta: () => get<FidMeta[]>('/posts/fid'),
  posts: (p: {
    fid?: string
    date_from?: string
    date_to?: string
    q?: string
    page?: number
    page_size?: number
    sort?: string
  }) => get<PostsPage>('/posts', p as Record<string, string | number | undefined>),
  runs: () => get<{ dates: RunSummary[] }>('/runs'),
  runDetail: (dir: string) => get<RunDetail>(`/runs/${dir}`),
  runDetailById: (id: number) => get<RunDetail>(`/runs/detail/${id}`),
  resources: () => get<Resources>('/resources'),
}

/** 生成导出 CSV 的下载地址（当前筛选条件下） */
export function exportCsvUrl(p: Record<string, string | undefined>): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v) qs.set(k, v)
  return `/api/posts/export?${qs.toString()}`
}

export function formatSize(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

export function formatDuration(sec: number | null | undefined): string {
  if (!sec || sec < 0) return '-'
  if (sec < 60) return `${sec}秒`
  const m = Math.floor(sec / 60)
  const s = sec % 60
  return m >= 60 ? `${Math.floor(m / 60)}小时${m % 60}分` : `${m}分${s}秒`
}
