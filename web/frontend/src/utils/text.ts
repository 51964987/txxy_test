/** HTML 转义与 URL 列表摘要：下载中心页内提交（D2 判重提醒）与帖子浏览下载入口共用，
 *  两处弹窗文案结构一致，抽到唯一实现避免复制粘贴 */

/** HTML 转义：URL 来自用户输入，拼进 MessageBox 的 HTML 内容前必须转义 */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => {
    const map: Record<string, string> = {
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }
    return map[c] as string
  })
}

/** 弹窗内只列出前 n 个链接，长列表截断以免撑爆弹窗 */
export function briefList(urls: string[], n = 3): string {
  const head = urls.slice(0, n).map(escapeHtml)
  const rest = urls.length - head.length
  return rest > 0 ? `${head.join('<br>')}<br>…等共 ${urls.length} 个` : head.join('<br>')
}
