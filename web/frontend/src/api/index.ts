const BASE = '/api'

/** 统一 API 请求错误类型：超时 / 网络故障 / HTTP 错误 / 主动取消 */
export type ApiErrorType = 'timeout' | 'network' | 'http' | 'aborted'

/** 统一 API 请求错误 */
export class ApiError extends Error {
  readonly type: ApiErrorType
  readonly status?: number

  constructor(type: ApiErrorType, message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.type = type
    this.status = status
  }
}

/** 判断是否为「主动取消 / 被新请求顶替」的请求错误，view 的 catch 中可直接忽略 */
export function isAborted(e: unknown): boolean {
  return e instanceof ApiError && e.type === 'aborted'
}

/** 默认请求超时（毫秒）：防止网络异常时请求无限挂起、轮询堆积 */
const DEFAULT_TIMEOUT = 10_000

/** 进行中的请求表：key -> AbortController；同 key 新请求会取消旧请求（防轮询堆积） */
const inflight = new Map<string, AbortController>()

interface RequestOptions {
  /** 超时毫秒，默认 10000；传 0 表示不设超时 */
  timeout?: number
  /** 请求去重 key；缺省由 path + query 自动计算 */
  key?: string
  /** 同 key 新请求是否取消旧请求，默认 true */
  dedupe?: boolean
}

async function request<T>(
  path: string,
  params?: Record<string, string | number | undefined | null>,
  opts: RequestOptions = {},
): Promise<T> {
  const qs = new URLSearchParams()
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
    }
  }
  const s = qs.toString()
  const url = BASE + path + (s ? `?${s}` : '')
  const key = opts.key ?? `${path}?${s}`

  // 同 key 并发时：新请求顶掉旧请求，防止轮询期间请求堆积
  const controller = new AbortController()
  if (opts.dedupe !== false) {
    inflight.get(key)?.abort()
    inflight.set(key, controller)
  }

  // 超时控制：到期主动 abort，并标记为超时（区别于被新请求顶替）
  let timedOut = false
  const timeoutMs = opts.timeout ?? DEFAULT_TIMEOUT
  let timer: ReturnType<typeof setTimeout> | null = null
  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      timedOut = true
      controller.abort()
    }, timeoutMs)
  }

  try {
    const res = await fetch(url, { signal: controller.signal })
    if (!res.ok) {
      let msg = `${res.status} ${res.statusText}`
      try {
        const j = await res.json()
        if (j && j.detail) msg = String(j.detail)
      } catch {
        /* ignore */
      }
      throw new ApiError('http', msg, res.status)
    }
    return (await res.json()) as T
  } catch (e) {
    // 主动取消：区分「超时」与「被新请求顶替」
    if ((e as DOMException | null)?.name === 'AbortError') {
      throw new ApiError(
        timedOut ? 'timeout' : 'aborted',
        timedOut ? `请求超时(${timeoutMs}ms): ${path}` : `请求已取消: ${path}`,
      )
    }
    // HTTP 错误（已包装）原样抛出
    if (e instanceof ApiError) throw e
    // fetch 网络层错误（连接失败 / DNS / CORS 等）
    if (e instanceof TypeError) throw new ApiError('network', `网络请求失败: ${path}`)
    throw e
  } finally {
    if (timer) clearTimeout(timer)
    // 仅当该 key 仍指向当前控制器时才清理，避免误删后续新请求的登记
    if (inflight.get(key) === controller) inflight.delete(key)
  }
}

async function get<T>(path: string, params?: Record<string, string | number | undefined | null>): Promise<T> {
  return request<T>(path, params)
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
