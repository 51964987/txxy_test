/** 复制文本到剪贴板：Clipboard API 优先 + execCommand 降级，覆盖非安全上下文与 iOS。
 *  此前资源管理页与帖子浏览各写一套（且后者无降级、无返回值校验），
 *  在手机通过 http://局域网IP 访问时会「提示已复制但实际没复制到」。 */

/** execCommand 降级路径（导出供「选中可见输入框」方案之后的兜底使用）：
 *  非安全上下文（http 局域网访问）或不支持 Clipboard API 时走这里。
 *  iOS Safari 仅 select() 选不中内容，必须可编辑 + setSelectionRange，否则返回 false。 */
export function legacyCopy(text: string): boolean {
  const ta = document.createElement('textarea')
  ta.value = text
  // 固定定位 + 不可见：不能用 display:none（不可选中），用尺寸与透明隐藏
  ta.style.position = 'fixed'
  ta.style.top = '0'
  ta.style.left = '0'
  ta.style.width = '1px'
  ta.style.height = '1px'
  ta.style.padding = '0'
  ta.style.border = 'none'
  ta.style.outline = 'none'
  ta.style.boxShadow = 'none'
  ta.style.background = 'transparent'
  ta.style.opacity = '0'
  ta.contentEditable = 'true'
  ta.readOnly = false
  ta.style.fontSize = '16px' // 避免 iOS 聚焦时页面缩放
  document.body.appendChild(ta)
  try {
    if (/iPad|iPhone|iPod/.test(navigator.userAgent)) {
      ta.focus()
      ta.setSelectionRange(0, text.length)
    } else {
      ta.select()
    }
    // 必须校验返回值：execCommand 返回 false 即复制失败
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(ta)
  }
}

/** 复制文本，返回是否成功（调用方据此提示，不要无条件提示成功） */
export async function copyText(text: string): Promise<boolean> {
  // Clipboard API 仅在安全上下文（HTTPS / localhost）可用；
  // 非安全上下文直接同步走降级，避免在 Promise 回调里执行 execCommand 时已丢失用户手势
  if (navigator.clipboard?.writeText && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 继续走降级
    }
  }
  return legacyCopy(text)
}
