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
  /** HTTP 方法，默认 GET */
  method?: string
  /** 请求体（JSON 序列化），仅 POST/DELETE 等需要请求体的方法使用 */
  body?: unknown
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
    const init: RequestInit = { signal: controller.signal, method: opts.method ?? 'GET' }
    if (opts.body !== undefined) {
      init.headers = { 'Content-Type': 'application/json' }
      init.body = JSON.stringify(opts.body)
    }
    const res = await fetch(url, init)
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

/** POST 请求（不参与轮询去重：仅用户主动点击触发，避免同 key 顶掉已提交的请求） */
async function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, undefined, { method: 'POST', body, dedupe: false })
}

/** DELETE 请求（不参与轮询去重，同 POST） */
async function del<T>(path: string): Promise<T> {
  return request<T>(path, undefined, { method: 'DELETE', dedupe: false })
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
  latest_run_at: string | null
  today_str: string
  total_users: number
  active_users: number
}

export interface Boards {
  top_likes: BoardTop[]
  top_replies: BoardTop[]
}

export interface TodayTopItem {
  fid: string | null
  name: string
  title: string
  url: string
  likes: number
  replies: number
  date: string
  /** 新入榜（仅本月最热计算，最新最热恒为 false） */
  is_new?: boolean
}

/** 每日互动量（本月最热卡头 sparkline 用） */
export interface BoardDaily {
  date: string
  value: number
}

/** 榜单排序维度：综合互动量 / 点赞 / 回复 / 时间衰减热度 */
export type BoardSort = 'engagement' | 'likes' | 'replies' | 'hot'

export interface TodayTop {
  date: string
  items: TodayTopItem[]
  /** 时间窗内帖子总数（当日 / 当月） */
  total: number
  /** 时间窗内有数据的天数（最新最热恒为 1 / 本月最热=当月已入库天数） */
  days: number
  /** 每日互动量分布（仅本月最热返回） */
  daily?: BoardDaily[]
}

export interface TopAuthor {
  author: string
  total: number
  today: number
  week: number
  month: number
  prev_week: number
  delta: number | null // 近 7 日环比百分比；null = 前 7 日无基准（新增）
  value: number // 当前口径下的排序主值
}

export interface TopFid {
  fid: string | null
  name: string
  total: number
  today: number
  week: number
  month: number
  prev_week: number
  delta: number | null
  value: number
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

/** 回收站条目（软删除，保留期内可恢复） */
export interface TrashItem {
  id: string
  /** 原相对路径（相对 downloads/） */
  rel: string
  name: string
  is_dir: boolean
  size: number
  deleted_at: string
  /** 是否已过保留期 */
  expired: boolean
  /** 剩余保留天数 */
  remain_days: number
}

export interface TrashResp {
  items: TrashItem[]
  keep_days: number
  total_size: number
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

/** 资源目录来源帖（B1：目录名 = 帖子标题，匹配 posts 表结果） */
export interface ResourceSource {
  matched: boolean
  title?: string
  fid?: string | null
  fid_name?: string
  date?: string
  author?: string
  url?: string
}

export type DownloadItemStatus = 'pending' | 'ok' | 'skip' | 'fail' | 'cancelled'
export type DownloadTaskStatus = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

export interface DownloadItem {
  url: string
  status: DownloadItemStatus
  stats: Record<string, number>
  error: string | null
  saved_dir: string | null
  /** 单链接耗时（秒，1 位小数；未执行为 null） */
  elapsed?: number | null
}

/** 任务概要（R1：GET /downloads 与 SSE 推送同构，不含 items/logs） */
export interface DownloadTaskSummary {
  id: string
  status: DownloadTaskStatus
  total: number
  done: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  cancel_requested: boolean
  priority?: boolean
  /** 各状态链接计数（ok/skip/fail/running/pending/cancelled） */
  items_summary?: Record<string, number>
  /** 已产生的保存目录（供资源管理页 B7 关联） */
  saved_dirs?: string[]
}

/** 任务详情（GET /downloads/{tid}：概要字段 + 逐 URL 明细与日志） */
export interface DownloadTaskDetail extends DownloadTaskSummary {
  urls: string[]
  items: DownloadItem[]
  logs: string[]
}

/** 兼容别名：详情即完整任务结构 */
export type DownloadTask = DownloadTaskDetail

export const api = {
  config: () => get<AppConfig>('/config'),
  overview: () => get<Overview>('/stats/overview'),
  boards: () => get<Boards>('/stats/boards'),
  todayTop: (limit = 10, sort: BoardSort = 'engagement') =>
    get<TodayTop>('/stats/today_top', { limit, sort }),
  topAuthors: (limit = 10, range = 'all') =>
    get<TopAuthor[]>('/stats/top_authors', { limit, range }),
  topFids: (limit = 10, range = 'all') => get<TopFid[]>('/stats/top_fids', { limit, range }),
  monthTop: (limit = 10, sort: BoardSort = 'engagement') =>
    get<TodayTop>('/stats/month_top', { limit, sort }),
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
    author?: string
    page?: number
    page_size?: number
    sort?: string
  }) => get<PostsPage>('/posts', p as Record<string, string | number | undefined>),
  runs: () => get<{ dates: RunSummary[] }>('/runs'),
  runDetail: (dir: string) => get<RunDetail>(`/runs/${dir}`),
  runDetailById: (id: number) => get<RunDetail>(`/runs/detail/${id}`),
  resources: () => get<Resources>('/resources'),
  resourceSource: (name: string) => get<ResourceSource>('/resources/source', { name }),
  openResourceFolder: (relPath: string) =>
    post<{ ok: boolean }>('/resources/open', { rel_path: relPath }),
  deleteResource: (path: string, isDir: boolean) =>
    post<{ ok: boolean; id: string; rel: string; size: number }>('/resources/delete', {
      path,
      is_dir: isDir,
    }),
  trashList: () => get<TrashResp>('/resources/trash'),
  restoreResource: (id: string) => post<{ ok: boolean; rel: string }>('/resources/restore', { id }),
  purgeResource: (id: string) => post<{ ok: boolean; count: number }>('/resources/purge', { id }),
  submitDownload: (urls: string[]) => post<{ id: string; count: number }>('/downloads', { urls }),
  downloadTasks: () => get<{ tasks: DownloadTaskSummary[] }>('/downloads'),
  downloadTask: (id: string) => get<DownloadTaskDetail>(`/downloads/${id}`),
  checkDownloadDup: (urls: string[]) => post<{ duplicated: string[] }>('/downloads/check-dup'),
  cancelDownload: (id: string) => post<{ id: string }>(`/downloads/${id}/cancel`),
  retryDownload: (id: string) => post<{ id: string; retried: number }>(`/downloads/${id}/retry`),
  prioritizeDownload: (id: string) => post<{ id: string }>(`/downloads/${id}/prioritize`),
  clearDownloads: () => post<{ cleared: number }>('/downloads/clear'),
  deleteDownload: (id: string) => del<{ id: string }>(`/downloads/${id}`),
}

/** 生成导出 CSV 的下载地址（当前筛选条件下） */
export function exportCsvUrl(p: Record<string, string | undefined>): string {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v) qs.set(k, v)
  return `/api/posts/export?${qs.toString()}`
}

/** 生成资源图片预览地址（B5：受控接口，仅 downloads/ 内图片白名单） */
export function resourceFileUrl(relPath: string): string {
  return `/api/resources/file?path=${encodeURIComponent(relPath)}`
}

/** 生成资源视频播放地址（受控接口，仅 downloads/ 内视频白名单，支持 Range） */
export function resourceVideoUrl(relPath: string): string {
  return `/api/resources/video?path=${encodeURIComponent(relPath)}`
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
