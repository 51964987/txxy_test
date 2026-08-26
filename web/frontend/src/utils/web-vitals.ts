/**
 * 轻量 Web Vitals 采集（不引入额外依赖）。
 * 通过 PerformanceObserver 监听 FCP / LCP / CLS / INP，
 * 并取导航时序里的 TTFB 等指标，输出到浏览器 console，便于定位性能问题。
 */

function observe(type: string, handler: PerformanceObserverCallback): void {
  if (!('PerformanceObserver' in window)) return
  try {
    const obs = new PerformanceObserver(handler)
    obs.observe({ type, buffered: true } as PerformanceObserverInit)
  } catch {
    // 浏览器不支持该观测类型时静默跳过
  }
}

export function initWebVitals(): void {
  // 导航时序指标：TTFB / DOM 就绪 / 总加载
  const navEntries = performance.getEntriesByType('navigation')
  if (navEntries.length > 0) {
    const nav = navEntries[0] as PerformanceNavigationTiming
    console.info(`[web-vitals] TTFB: ${Math.round(nav.responseStart)}ms`)
    console.info(`[web-vitals] DOMContentLoaded: ${Math.round(nav.domContentLoadedEventEnd - nav.startTime)}ms`)
    console.info(`[web-vitals] load: ${Math.round(nav.loadEventEnd - nav.startTime)}ms`)
  }

  // FCP：首次内容绘制
  observe('paint', (list) => {
    for (const entry of list.getEntries()) {
      if (entry.name === 'first-contentful-paint') {
        console.info(`[web-vitals] FCP: ${Math.round(entry.startTime)}ms`)
      }
    }
  })

  // LCP：最大内容绘制（只记录最后一次）
  observe('largest-contentful-paint', (list) => {
    const entries = list.getEntries()
    const last = entries[entries.length - 1]
    if (last) console.info(`[web-vitals] LCP: ${Math.round(last.startTime)}ms`)
  })

  // CLS：累计布局偏移（累加无用户输入的偏移值）
  let cls = 0
  observe('layout-shift', (list) => {
    for (const entry of list.getEntries() as unknown as Array<{ hadRecentInput: boolean; value: number }>) {
      if (!entry.hadRecentInput) cls += entry.value
    }
    console.info(`[web-vitals] CLS: ${cls.toFixed(3)}`)
  })

  // INP：交互延迟（记录观测到的最大单次延迟）
  observe('event', (list) => {
    let worst = 0
    for (const entry of list.getEntries() as unknown as Array<{ duration: number }>) {
      worst = Math.max(worst, entry.duration)
    }
    if (worst > 0) console.info(`[web-vitals] INP(worst): ${Math.round(worst)}ms`)
  })
}
