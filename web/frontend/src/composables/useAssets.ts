// 内容资产（媒体库沉淀进度）共享逻辑：取数 + 全部派生指标 + 下钻导航。
// 数据总览（精简 KPI 摘要）与资源管理（KPI 优先完整卡）共用同一份，禁止两处各写一份
// （项目通用工程约束第 1 条：同一份逻辑只能有一处实现）。
// 后端口径见 GET /api/stats/assets 与 docs/内容资产沉淀进度调研与建议.md。
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, type Assets } from '../api'
import { type CategoryKey } from '../utils/category'
import { pad2 } from '../utils/time'

/** 资产状态行的一行（五态：在途/失败/可重下/空壳/缺口） */
export interface AssetStateRow {
  key: string
  label: string
  num: number
  color: string
  cls: string
  title: string
  clickable?: boolean
}

export function useAssets() {
  const router = useRouter()
  const assets = ref<Assets | null>(null)

  /** 取数（从 loadP0 抽出）：失败静默保留旧值，下一轮自动重试（与首屏其它补充卡同容错策略） */
  async function loadAssets(): Promise<void> {
    try {
      assets.value = await api.assets()
    } catch {
      // 静默：保留旧值，等下一轮自动刷新
    }
  }

  /** 资产空态：已下载帖与本地文件均为 0（任务 + 文件 + 履历全部清空），才渲染引导空态 */
  const assetsEmpty = computed(() => {
    const a = assets.value
    if (!a) return false
    return a.downloaded_posts === 0 && a.files === 0 && a.folders === 0
  })

  /** 漏斗转化率：已沉淀帖 / 收录帖子（全库口径，会被长尾稀释；分档见 coverage） */
  const downloadRate = computed(() => {
    const a = assets.value
    if (!a || !a.posts_total) return null
    return ((a.downloaded_posts / a.posts_total) * 100).toFixed(1)
  })

  /** 头条 KPI 进度条宽度：以「目标刻度」为满格（当前覆盖率 / 目标覆盖率），超出由「已达成」表达 */
  const goalBarWidth = computed(() => {
    const g = assets.value?.goal
    if (!g || !g.target_rate) return '0%'
    return `${Math.min(100, Math.round((g.current_rate / g.target_rate) * 100))}%`
  })

  /** 目标进度说明：结构化「口径 · 目标 · 当前 → 缺口」（卡面已省字，明细按需展开） */
  const goalTip = computed(() => {
    const g = assets.value?.goal
    if (!g) return ''
    const head = `目标档 ${g.scope_label} · 目标 ${g.target_rate}% · 当前 ${g.downloaded}/${g.total}`
    return g.reached ? `${head}（已达成）` : `${head} → 还差 ${g.remain} 帖`
  })

  /** 进度条无障碍文案：卡面文字收敛后，完整口径（含分母与档位）只在此处完整保留 */
  const goalAria = computed(() => {
    const g = assets.value?.goal
    if (!g) return '沉淀目标进度'
    return (
      `沉淀目标档 ${g.scope_label}，目标 ${g.target_rate}%，`
      + `当前 ${g.current_rate}%（${g.downloaded} / ${g.total}）`
    )
  })

  /** 五态行（库存以外的状态才是「是否在推进」的信号；缺口项可下钻） */
  const stateRows = computed<AssetStateRow[]>(() => {
    const s = assets.value?.state
    if (!s) return []
    return [
      {
        key: 'active',
        label: '在途',
        num: s.active,
        color: '#2f6fed',
        cls: '',
        title: '排队中 / 正在下载的帖数（随自动刷新即时更新）',
      },
      {
        key: 'failed',
        label: '失败',
        num: s.failed,
        color: '#f56c6c',
        cls: s.failed ? 'is-bad' : '',
        clickable: true,
        title:
          '最近一次下载失败、此后未成功的帖数（持久记录，清空任务中心不会丢）。'
          + '注意这是「帖 / 链接数」：下载中心按任务统计的「失败任务」是另一个量纲'
          + '（一个任务可含多条失败链接），两者数字天然不同。点击查看失败缺口清单',
      },
      {
        key: 're_download',
        label: '可重下',
        num: s.re_download,
        color: '#e6a23c',
        cls: '',
        clickable: true,
        title:
          '曾下载成功、但文件已被资源管理清理的帖数（与「失败」互补：那是没下成，这是下成过又没了）。'
          + '点击查看可重下清单，可在清单里直接重新下载补回本地',
      },
      {
        key: 'empty_dirs',
        label: '空壳',
        num: s.empty_dirs,
        color: '#909399',
        cls: '',
        title: '目录存在但 0 个文件的残留（多为下载失败 / 取消留下）',
      },
      {
        key: 'gap',
        label: `近${s.gap_days}日缺口`,
        num: s.gap_recent,
        color: '#f59e0b',
        cls: '',
        clickable: true,
        title: '该窗口内有互动、但尚未沉淀到本地的帖子数（点击下钻缺口明细）',
      },
    ]
  })

  /** 状态项点击：可下钻的两项各落到与卡上数字**同源同口径**的清单
 *  （失败 → 下载中心「失败缺口」；可重下 → 下载中心「可重下」）；其余走缺口筛选 */
  function onStateClick(r: AssetStateRow) {
    if (!r.clickable) return
    if (r.key === 'failed') goDownloadFailures()
    else if (r.key === 're_download') goDownloadReDownloads()
    else goPendingPosts()
  }

  /** 对账提示：磁盘目录 = 已认领 + 未认领 + 空壳（三者互斥，不重复计数） */
  const reconcileTip = computed(() => {
    const a = assets.value
    if (!a) return ''
    return (
      `磁盘目录 ${a.folders} 个 = 已认领 ${a.reconcile.claimed} + 未认领 ${a.reconcile.unclaimed} + 空壳 ${a.reconcile.empty}。`
      + '「未认领」= 目录有内容但未与任何收录帖对上（目录名是标题清理 + 截断 80 字后的结果，正常应为 0）'
    )
  })

  /** 分版块沉淀集中度：只取确有沉淀的版块（其余为 0，无需占位），悬浮看明细 */
  const fidTop = computed(() => (assets.value?.by_fid ?? []).filter((f) => f.downloaded > 0))
  const fidTip = computed(() =>
    fidTop.value.map((f) => `${f.name} ${f.downloaded} 帖 / 沉淀率 ${f.rate}%`).join(' · '),
  )

  /** 近 N 日新增沉淀的体积合计（体积按目录当前占用估算，非沉淀当日快照） */
  const recentSize = computed(() =>
    (assets.value?.growth ?? []).reduce((sum, p) => sum + p.size, 0),
  )

  /** 增长迷你柱：高度按窗口内峰值归一（最小 4% 保基线，便于看出哪天断档） */
  const growthBars = computed(() => {
    const g = assets.value?.growth ?? []
    const max = Math.max(1, ...g.map((p) => p.posts))
    return g.map((p) => ({ h: Math.max(4, Math.round((p.posts / max) * 100)) }))
  })
  const growthTip = computed(() =>
    (assets.value?.growth ?? []).map((p) => `${p.date} +${p.posts} 帖`).join(' · '),
  )

  // ===== 下钻导航 =====
  /** 跳资源管理页（本地媒体资产总览） */
  function goResources() {
    router.push('/resources')
  }

  /** 已沉淀帖下钻：只保留已落盘帖子（卡片多少条，列表就多少条——数字自洽的硬校验） */
  function goPostsDownloaded() {
    router.push({ path: '/posts', query: { downloaded: '1' } })
  }

  /** 类型分布下钻：跳资源管理页并按该类型筛选（继承类型上下文，口径自洽） */
  function goResourcesType(key: CategoryKey) {
    router.push({ path: '/resources', query: { type: key } })
  }

  /** 缺口下钻：近 30 日未沉淀高互动帖（与帖子页「未下载」筛选同口径、同排序） */
  function goPendingPosts() {
    const to = new Date()
    const from = new Date()
    from.setDate(from.getDate() - 29) // 近 30 日（含今天）
    router.push({
      path: '/posts',
      query: {
        date_from: `${to.getFullYear()}-${pad2(from.getMonth() + 1)}-${pad2(from.getDate())}`,
        date_to: `${to.getFullYear()}-${pad2(to.getMonth() + 1)}-${pad2(to.getDate())}`,
        undownloaded: '1',
        sort: 'engagement_desc',
      },
    })
  }

  /** 失败下钻：下载中心「失败缺口」视图（与卡上 failed 同源同口径，条数严格相等） */
  function goDownloadFailures() {
    router.push({ path: '/downloads', query: { view: 'failures' } })
  }

  /** 可重下下钻：下载中心「可重下」视图（与卡上 re_download 同源同口径，条数严格相等） */
  function goDownloadReDownloads() {
    router.push({ path: '/downloads', query: { view: 're-downloads' } })
  }

  return {
    assets,
    loadAssets,
    assetsEmpty,
    downloadRate,
    goalBarWidth,
    goalTip,
    goalAria,
    stateRows,
    onStateClick,
    reconcileTip,
    fidTop,
    fidTip,
    recentSize,
    growthBars,
    growthTip,
    goResources,
    goPostsDownloaded,
    goResourcesType,
    goPendingPosts,
    goDownloadFailures,
    goDownloadReDownloads,
  }
}
