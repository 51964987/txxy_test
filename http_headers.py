"""HTTP 请求头（全项目唯一定义处，零依赖，可被任意模块安全 import）。

历史问题：UA 字符串曾在 download_files / scraper / extract_images /
extract_videos / extract_torrents 五个模块各写一份，其中三处注释还写着
「与 download_files.HEADERS 保持一致，改动时需同步」——典型的手工同步债：
改 UA 要动五处，漏一处就会出现「有的请求像浏览器、有的不像」而被站点区别对待。

统一约定：
  - UA 与 Accept-Language 全项目一致；
  - Accept 按用途区分：页面抓取用 ACCEPT_HTML，图片/视频下载用各自的类型——
    媒体下载**不能**带 text/html，否则图床会判定为「浏览器直接打开图片」
    而 302 到广告查看页。
"""
# 浏览器 UA：站点按 UA 判定客户端类型，各抓取/下载模块必须一致
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"

# 页面抓取（HTML 列表页 / 种子解析页）
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
# 图片下载：必须是纯图片类型，模拟 <img> 标签加载
ACCEPT_IMAGE = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
# 视频下载：模拟 <video> 标签加载
ACCEPT_VIDEO = "video/webm,video/mp4,video/ogg,video/*;q=0.9,*/*;q=0.8"


def build_headers(accept: str) -> dict[str, str]:
    """构造请求头：UA / Accept-Language 统一，Accept 按用途传入（见上方常量）。"""
    return {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "Accept-Language": ACCEPT_LANGUAGE,
    }
