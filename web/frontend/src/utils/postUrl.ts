/**
 * 帖子链接拼装（前端唯一实现，所有「打开帖子 / 复制帖子链接」都必须走这里）。
 *
 * 为什么不再直接用后端下发的绝对地址：
 * 后端下发的展示地址在本机是 `http://127.0.0.1:1024/...`（本地镜像），而本机镜像
 * web.exe 只监听回环地址（实测 netstat 为 `TCP 127.0.0.1:1024 LISTENING`），
 * 手机等其它设备访问 `127.0.0.1` 只会指向手机自己 —— 链接必然打不开。
 * 因此浏览器侧统一改用**同源中继前缀** `/mirror`：由看板进程转发到回环镜像
 * （实现见 `web/mirror.py`），桌面 / 手机 / 局域网任何访问方式都自适应。
 *
 * 降级不需要前端判断：中继在「镜像未运行 / 未配置镜像」时会自行 302 到业务域名
 * 同一路径（即「1024 访问不了时用 PUBLIC_DOMAIN 打开」），前端恒用中继前缀即可。
 */

/** 同源中继前缀：与后端 `web/mirror.py` 的路由约定，改一处必须同步改另一处 */
export const MIRROR_PREFIX = '/mirror'

/** 本站帖子路径标记：与库内视图 `substr(url, instr(url,'/htm_data/'))` 同一判定口径，
 *  用于把「带域名的完整地址」归一成站点相对路径；站外链接（图床等）不含该标记。 */
const SITE_PATH_MARK = '/htm_data/'

/** 取本站帖子的相对路径（含查询串）；站外链接返回空串 */
export function postPathOf(url: string): string {
  const i = url.indexOf(SITE_PATH_MARK)
  return i >= 0 ? url.slice(i) : ''
}

/** 打开用：本站帖子 → 同源中继相对路径；站外链接原样返回 */
export function postOpenUrl(url: string): string {
  const path = postPathOf(url)
  return path ? MIRROR_PREFIX + path : url
}

/** 复制用：同源绝对地址（粘到本机或局域网其它设备都能打开）；
 *  站内相对路径粘出去无法使用，故这里补全为绝对地址。 */
export function postCopyUrl(url: string): string {
  const path = postPathOf(url)
  return path ? new URL(MIRROR_PREFIX + path, window.location.origin).href : url
}
