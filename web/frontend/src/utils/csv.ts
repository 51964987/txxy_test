/**
 * CSV 导出（全局唯一实现，见 CODEBUDDY.md 通用工程约束第 1 条）。
 *
 * 为什么在前端生成而不是后端出一个导出接口：
 * 导出内容要**跟随当前筛选结果**（页面所见即所得），而这些数据本就在前端内存里
 * （资源扫描结果一次取回后，类型/关键词/体积/时间筛选都在前端完成）。
 * 若走后端，既要多一次全盘扫描，又要为每个筛选维度在后端重复实现一遍过滤逻辑——
 * 两份过滤逻辑必然漂移（同一坑已在本项目踩过多次）。
 *
 * 编码规范：UTF-8 BOM + CRLF —— Excel 双击打开中文不乱码、行尾正确。
 */

/** 转义单个单元格：含逗号 / 引号 / 换行时用双引号包裹，内部引号翻倍（RFC 4180） */
function csvCell(v: unknown): string {
  const s = v === null || v === undefined ? '' : String(v)
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

/** 生成 CSV 文本（首行为表头，行尾 CRLF） */
export function buildCsv(header: readonly string[], rows: ReadonlyArray<ReadonlyArray<string | number>>): string {
  return [header, ...rows].map((r) => r.map(csvCell).join(',')).join('\r\n')
}

/** 触发浏览器下载（Blob + 临时 a 标签），文件名由调用方给定 */
export function downloadCsv(
  filename: string,
  header: readonly string[],
  rows: ReadonlyArray<ReadonlyArray<string | number>>,
): void {
  // BOM 必须位于内容最前，否则 Excel 会按本地编码解析导致中文乱码
  const blob = new Blob(['\ufeff' + buildCsv(header, rows)], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 延迟释放：立即 revoke 会让部分浏览器来不及取数就失效（下载变成空文件）
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}