"""
下载指定 HTML 页面中的所有图片与视频到本地目录（目录以页面标题命名）

职责划分（依赖均为单向，无循环导入）：
    - 页面访问（HTML 获取、标题提取）与下载编排：本文件
    - 通用下载核心（download_media 等，图片/视频共用）：media_download.py
    - 图片相关（提取 / 请求头 / 校验 / 下载）：extract_images.py
    - 视频相关（提取 / 请求头 / 校验 / 下载）：extract_videos.py
    - torrent/种子相关（提取 / rmdown 解析 / 下载 / 标题解析）：extract_torrents.py
    - 磁力链接相关（提取全部 magnet: 地址 / TXT 清单导出）：extract_magnets.py
    - 云盘链接相关（提取 redircdn 中转网盘地址 / TXT 清单导出）：extract_clouds.py
用法（至少传入一个 URL，入参为必填项）:
    python download_files.py "http://xxx/htm_data/2603/8/7184094.html"
    python download_files.py "url1" "url2"   # 支持多个 URL
    python download_files.py "https://www.rmdown.com/link.php?hash=xxx"  # 直接下载种子 → downloads/<种子标题或日期>
    python download_files.py "https://xxx.com/a.jpg"  # 图片/视频直链 → downloads/<文件名(不含扩展名)>/，多个直链同目录
    python download_files.py "a.jpg" "https://xxx.com/b.jpg"  # 第一个入参为不带协议前缀的媒体文件名：仅作下载目录 downloads/a/，不发起请求
入参必须为 http:// 或 https:// 开头的完整链接；漏写协议前缀（如相对路径）不会自动拼接根地址，
会直接跳过并提示，绝不发起请求访问。
唯一例外：第一个入参若为媒体文件（图片/视频）文件名（不带协议前缀），只作为下载目录名使用，
不发起请求；带协议前缀的媒体直链则照常下载并决定下载目录。
"""
import requests
from bs4 import BeautifulSoup
import os
import sys
import time
import traceback
from urllib.parse import urlparse

import file_logger

from media_download import TIMEOUT
from extract_images import (
    extract_image_urls,
    is_image_url,
    needs_split_dirs,
    image_save_path,
    download_image,
    GIF_SUBDIR,
    JPG_SUBDIR,
)
from extract_videos import (
    extract_video_urls,
    download_video,
    is_video_url,
    video_save_path,
    VIDEO_SUBDIR,
)
from extract_torrents import (
    extract_other_urls,
    download_torrent,
    sanitize_title,
    RMDOWN_LINK_RE,
    TORRENT_LINK_RE,
)
from extract_magnets import extract_magnet_links, save_magnets_txt
from extract_clouds import extract_cloud_links, save_clouds_txt

# ============ 配置区域 ============
# 图片保存根目录（页面标题作为其下子目录名）
DOWNLOAD_ROOT = "downloads"

# 请求头，模拟浏览器（用于抓取 HTML 页面）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 单张图片下载间隔（秒），避免请求过快
DOWNLOAD_INTERVAL = 0.3

# 页面请求重试配置：临时性错误（网络异常 / 网关 5xx / 限流）自动重试
# 520 为 Cloudflare "源站返回未知错误"，属临时性故障，重试通常可恢复
MAX_RETRIES = 3      # 总尝试次数（首次 + 最多 2 次重试）
RETRY_DELAY = 2.0    # 首次重试等待（秒），后续按次数递增
# 可重试状态码：限流 429 与网关/源站 5xx（含 520 等 Cloudflare 错误）；
# 4xx（404/403/410 等）重试无意义，直接失败
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 524, 525, 526, 527}


# ============ 页面访问 ============


def _retry_wait(resp: requests.Response, attempt: int) -> float:
    """重试等待（秒）：429 优先尊重 Retry-After 响应头（上限 60 秒），其余按次数递增"""
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), 60.0)
            except ValueError:
                pass
    return RETRY_DELAY * attempt


def fetch_page(url: str) -> str | None:
    """获取 HTML 内容（自动检测编码，兼容 GBK/GB18030 等非 UTF-8 页面）。
    网络异常与可重试状态码（限流 429、网关/源站 5xx 含 520）自动重试，递增等待；
    4xx（404/403/410 等）重试无意义，直接失败。"""
    print(f"正在请求: {url}")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            # 编码策略：信任响应头声明的 charset；未声明或为 ISO-8859-1（requests 默认兜底值）
            # 时，改用 chardet 按内容检测（apparent_encoding），再兜底 UTF-8。
            # 直接强制 UTF-8 会对 GBK/GB18030 页面（如 mmonly.cc）解码产生乱码。
            enc = resp.encoding
            if not enc or enc.lower() == "iso-8859-1":
                enc = resp.apparent_encoding or "utf-8"
            resp.encoding = enc
            if resp.status_code == 200:
                return resp.text
            # 可重试状态码（限流 429、网关/源站 5xx 含 520）：等待后重试
            if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                wait = _retry_wait(resp, attempt)
                print(f"[重试] 状态码 {resp.status_code}（第 {attempt}/{MAX_RETRIES} 次），{wait:.1f} 秒后重试...")
                time.sleep(wait)
                continue
            print(f"[警告] 返回状态码: {resp.status_code}")
            return None
        except requests.RequestException as e:
            # 网络层异常（超时/连接重置等）多为暂时性，等待后重试
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                print(f"[重试] 请求异常: {e}（第 {attempt}/{MAX_RETRIES} 次），{wait:.1f} 秒后重试...")
                time.sleep(wait)
                continue
            print(f"[错误] 请求失败: {e}")
            return None
    return None


def extract_title(html: str, url: str) -> str:
    """提取页面标题：优先取帖内 h1/h4，其次 <title>，最后用 URL 文件名"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in ("h1", "h4", "h2"):
        node = soup.find(tag)
        if node:
            text = node.get_text(strip=True)
            if text:
                return text
    title_tag = soup.find("title")
    if title_tag:
        text = title_tag.get_text(strip=True)
        if text:
            return text
    # fallback: URL 文件名
    basename = os.path.basename(urlparse(url).path)
    return os.path.splitext(basename)[0] or "page"


# ============ 页面下载主流程 ============


def process_page(url: str) -> dict[str, int]:
    """处理单个页面：提取标题并下载全部图片与视频，返回各类型下载数量统计
    （"跳过"/"失败"为附加明细键，供执行汇总区分已存在与下载失败）"""
    stats: dict[str, int] = {}
    #print(f"目标页面: {url}\n")

    # --- 获取 HTML ---
    html = fetch_page(url)
    if not html:
        print("[失败] 无法获取页面内容", file=sys.stderr)
        return stats

    # --- 标题 → 目录 ---
    title = extract_title(html, url)
    save_dir = os.path.join(DOWNLOAD_ROOT, sanitize_title(title))
    os.makedirs(save_dir, exist_ok=True)
    print(f"标题: {title}")
    print(f"保存目录: {save_dir}\n")

    # --- 提取图片、视频、其他资源与磁力链接 ---
    image_urls = extract_image_urls(html, url)
    video_urls = extract_video_urls(html, url)
    magnet_links = extract_magnet_links(html)
    cloud_links = extract_cloud_links(html)
    other_urls = extract_other_urls(html, url)
    if not image_urls and not video_urls and not other_urls and not magnet_links and not cloud_links:
        # 页面解析不出任何资源：入参可能是媒体文件直链（无扩展名或图床重定向页），
        # 尝试把当前 URL 直接当单个媒体文件下载；内容校验失败会安全清理，不影响流程
        direct_status = download_media_direct(url, save_dir)
        if direct_status != "fail":
            stats = {"媒体": 1}
            if direct_status == "skip":
                stats["跳过"] = 1
            return stats
        print("[警告] 页面中未提取到任何图片、视频、其他资源、磁力链接或云盘链接")
        return stats

    session = requests.Session()

    # --- 下载图片 ---
    # 目录策略：仅当页面同时存在 gif 与 jpg 时才分 gifs/jpgs 两个子目录；
    # 只有单一类型（全 gif 或全 jpg）时图片直接存标题根目录，不创建子目录
    ok_count = 0
    ok_gif = 0
    ok_jpg = 0
    skip_count = 0
    fail_count = 0
    if image_urls:
        # 目录策略与命名规则位于 extract_images.needs_split_dirs / image_save_path
        split_dirs = needs_split_dirs(image_urls)
        gif_dir = os.path.join(save_dir, GIF_SUBDIR)
        jpg_dir = os.path.join(save_dir, JPG_SUBDIR)
        if split_dirs:
            os.makedirs(gif_dir, exist_ok=True)
            os.makedirs(jpg_dir, exist_ok=True)
        print(f"共提取到 {len(image_urls)} 张图片，开始下载...\n")
        try:
            gif_idx = 0
            jpg_idx = 0
            for i, img_url in enumerate(image_urls, start=1):
                save_path, is_gif, gif_idx, jpg_idx = image_save_path(
                    save_dir, img_url, i, split_dirs, gif_idx, jpg_idx
                )
                # 记录"已存在跳过"，供执行汇总区分新下载与已存在文件
                existed = os.path.exists(save_path) and os.path.getsize(save_path) > 0
                if download_image(session, img_url, save_path, referer=url):
                    ok_count += 1
                    if is_gif:
                        ok_gif += 1
                    else:
                        ok_jpg += 1
                    if existed:
                        skip_count += 1
                else:
                    fail_count += 1
                if i < len(image_urls):
                    time.sleep(DOWNLOAD_INTERVAL)
        except KeyboardInterrupt:
            print("\n[中断] 用户手动终止 (Ctrl+C)")
        except Exception as e:
            print(f"\n[异常] 程序发生错误: {e}", file=sys.stderr)
            traceback.print_exc()
        finally:
            # 汇总信息：raw 模式不加时间戳
            with file_logger.raw():
                if split_dirs:
                    print(
                        f"\n[汇总] 图片成功 {ok_count}/{len(image_urls)} - {save_dir}"
                        + f"\n\tgifs: {ok_gif} 张\n\tjpgs: {ok_jpg} 张"
                    )
                else:
                    print(f"\n[汇总] {save_dir} 图片成功 {ok_count}/{len(image_urls)} → {save_dir}")
    else:
        print("\n[提示] 页面中未提取到任何图片")

    # --- 下载视频 ---
    ok_video = 0
    skip_video = 0
    fail_video = 0
    if video_urls:
        video_dir = os.path.join(save_dir, VIDEO_SUBDIR)
        os.makedirs(video_dir, exist_ok=True)
        print(f"\n共提取到 {len(video_urls)} 个视频，开始下载...\n")
        try:
            for i, v_url in enumerate(video_urls, start=1):
                # 命名规则位于 extract_videos.video_save_path
                save_path = video_save_path(video_dir, v_url, i)
                existed = os.path.exists(save_path) and os.path.getsize(save_path) > 0
                if download_video(session, v_url, save_path, referer=url):
                    ok_video += 1
                    if existed:
                        skip_video += 1
                else:
                    fail_video += 1
                if i < len(video_urls):
                    time.sleep(DOWNLOAD_INTERVAL)
        except KeyboardInterrupt:
            print("\n[中断] 用户手动终止 (Ctrl+C)")
        except Exception as e:
            print(f"\n[异常] 程序发生错误: {e}", file=sys.stderr)
            traceback.print_exc()
        finally:
            # 汇总信息：raw 模式不加时间戳
            with file_logger.raw():
                print(f"\n[汇总] 视频成功 {ok_video}/{len(video_urls)} → {video_dir}")
    else:
        print("\n[提示] 页面中未提取到任何视频")

    # --- 其他媒体类型（种子等）：提取并下载 ---
    # 提取 / 下载逻辑位于 extract_torrents.py
    # （新增类型时在 extract_torrents.extract_other_urls() 中扩展提取规则）
    # 目录规则：页面场景用页面标题作目录（downloads/<页面标题>/），
    # 不使用种子自身标题（种子自身标题仅用于直接传入种子链接的场景）
    ok_other = 0
    if other_urls:
        print(f"\n共提取到 {len(other_urls)} 个其他类型资源，开始下载...\n")
        try:
            for i, o_url in enumerate(other_urls, start=1):
                if download_torrent(o_url, DOWNLOAD_ROOT, dir_name=sanitize_title(title)):
                    ok_other += 1
                if i < len(other_urls):
                    time.sleep(DOWNLOAD_INTERVAL)
        except KeyboardInterrupt:
            print("\n[中断] 用户手动终止 (Ctrl+C)")
        except Exception as e:
            print(f"\n[异常] 程序发生错误: {e}", file=sys.stderr)
            traceback.print_exc()
        finally:
            # 汇总信息：raw 模式不加时间戳
            with file_logger.raw():
                print(f"\n[汇总] 其他资源成功 {ok_other}/{len(other_urls)} → {save_dir}")
    else:
        print("\n[提示] 页面中未提取到其他类型资源")

    # --- 磁力链接：提取并导出 TXT 清单 ---
    # 匹配 HTML 中所有 magnet: 地址并输出，TXT 只保留 magnet: 开头的地址，每行一条
    # 逻辑位于 extract_magnets.py，无需网络下载
    # 输出到页面标题目录下：downloads/<页面标题>/magnets.txt
    if magnet_links:
        txt_path = save_magnets_txt(magnet_links, save_dir)
        # 汇总信息：raw 模式不加时间戳
        with file_logger.raw():
            print(f"\n[汇总] 磁力链接 {len(magnet_links)} 条 → {txt_path}")
    else:
        print("\n[提示] 页面中未提取到磁力链接")

    # --- 云盘链接：提取并导出 TXT 清单 ---
    # 匹配 HTML 中所有 /2023.redircdn.com/? 中转的网盘地址，还原为真实链接
    # 逻辑位于 extract_clouds.py，无需网络下载
    # 输出到页面标题目录下：downloads/<页面标题>/clouds.txt
    if cloud_links:
        txt_path = save_clouds_txt(cloud_links, save_dir)
        # 汇总信息：raw 模式不加时间戳
        with file_logger.raw():
            print(f"\n[汇总] 云盘链接 {len(cloud_links)} 条 → {txt_path}")
    else:
        print("\n[提示] 页面中未提取到云盘链接")

    # --- 统计各类型下载数量（供 main 最终执行汇总使用） ---
    stats["图片"] = ok_count
    stats["视频"] = ok_video
    stats["其他"] = ok_other
    if magnet_links:
        stats["磁力"] = len(magnet_links)
    if cloud_links:
        stats["云盘"] = len(cloud_links)
    # 附加明细：已存在跳过 / 失败数量（其他资源无法前置判断已存在，失败按 总数-成功 估算）
    skip_total = skip_count + skip_video
    fail_total = fail_count + fail_video + (len(other_urls) - ok_other if other_urls else 0)
    if skip_total:
        stats["跳过"] = skip_total
    if fail_total:
        stats["失败"] = fail_total
    return stats


# ============ 媒体文件直链下载 ============
# 入参直接为图片/视频直链（而非 HTML 页面）时，按路径扩展名识别并直接下载。
# 扩展名集合与判断分别位于 extract_images.IMAGE_EXTS / extract_videos.VIDEO_EXTS


def is_media_direct_url(url: str) -> bool:
    """判断 URL 是否为图片/视频文件直链（扩展名判断位于各资源模块）"""
    return is_image_url(url) or is_video_url(url)


def _media_filename(url: str) -> str:
    """从媒体直链 URL 解析保存文件名（保留扩展名，清理非法字符）"""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    filename = sanitize_title(os.path.basename(urlparse(url).path)) or "media"
    if not filename.lower().endswith(ext):
        filename += ext
    return filename


def download_media_direct(url: str, save_dir: str) -> str:
    """
    直接下载单个媒体文件直链到 save_dir/。
    返回三态：'ok'（新下载成功）/ 'skip'（已存在跳过）/ 'fail'（下载失败），
    供执行汇总区分新下载与已存在文件。
    """
    filename = _media_filename(url)
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    # 与 media_download.download_media 的已存在判断保持一致，提前返回跳过态
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        print(f"[跳过] 已存在: {filename}")
        return "skip"
    print(f"媒体直链: {url}")
    session = requests.Session()
    if is_image_url(url):
        ok = download_image(session, url, save_path, referer=url)
    else:
        ok = download_video(session, url, save_path, referer=url)
    return "ok" if ok else "fail"


def _is_url(arg: str) -> bool:
    """是否为 http:// 或 https:// 开头的完整链接（大小写不敏感）"""
    return arg.lower().startswith(("http://", "https://"))


def _parse_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """解析命令行入参：仅接受 http:// 或 https:// 开头的完整链接。

    不带协议前缀的入参（如漏写前缀的路径 / 相对路径）一律不发起请求，
    直接进入跳过列表返回，由调用方输出提示。其余入参与第 1 个入参同等对待，
    均为下载路径（URL）。
    返回 (有效 URL 列表, 被跳过的无效入参列表)。
    """
    urls: list[str] = []
    invalid: list[str] = []
    for arg in argv:
        arg = arg.strip()
        if not arg:
            continue
        if _is_url(arg):
            urls.append(arg)
        else:
            invalid.append(arg)
    return urls, invalid


def _first_media_dir(first: str) -> tuple[str | None, bool]:
    """第一个入参为媒体文件（图片/视频）时，返回 (下载目录名, 是否需要下载该入参)。

    - 带协议前缀的媒体直链 → (downloads/<文件名不含扩展名>/, True)：照常下载并决定目录；
    - 不带协议前缀的媒体文件名 → (downloads/<文件名不含扩展名>/, False)：仅作目录名，不发起请求；
    - 非媒体文件 → (None, False)：保持原逻辑，媒体直链目录在循环内惰性决定。
    """
    if not is_media_direct_url(first):
        return None, False
    return os.path.join(DOWNLOAD_ROOT, os.path.splitext(_media_filename(first))[0]), _is_url(first)


def main() -> None:
    """解析命令行入参（必填，支持多个 URL），逐个页面/媒体文件下载"""
    # 启用日志：所有打印同时写入 outputs/<日期>/download_files_<日期>.log
    _ = file_logger.setup("download_files")
    args = [a.strip() for a in sys.argv[1:] if a.strip()]
    if not args:
        print("错误: 未提供任何有效 URL（需为 http:// 或 https:// 开头），入参为必填项。", file=sys.stderr)
        print("用法: python download_files.py <url1> [url2 ...]", file=sys.stderr)
        sys.exit(1)

    # 媒体直链统一输出目录：downloads/<第一个媒体文件名（不含扩展名）>/，
    # 多个媒体直链全部下载到该目录，与页面场景 downloads/<标题>/ 结构保持一致。
    # 第一个入参若为媒体文件（图片/视频直链或裸文件名），一律以文件名（不含扩展名）
    # 作为下载目录名：带协议前缀则照常下载该文件，不带协议前缀仅作目录名、不发起请求。
    media_dir, first_download = _first_media_dir(args[0])
    rest_args = args
    if media_dir is not None:
        print(f"媒体直链保存目录: {media_dir}")
        if not first_download:
            print(
                f"[提示] 第一个入参 {args[0]!r} 不带协议前缀，仅作为下载目录使用，未发起请求",
                file=sys.stderr,
            )
            rest_args = args[1:]

    urls, invalid = _parse_args(rest_args)
    for arg in invalid:
        # 非 http/https 前缀不尝试请求访问：直接跳过并提示，
        # 避免漏写协议前缀时误拼接根地址访问到意外路径
        print(f"[跳过] 入参 {arg!r} 不是 http/https 链接，未发起请求，请检查是否漏写协议前缀", file=sys.stderr)
    if invalid:
        print(f"[提示] 已跳过 {len(invalid)} 个无效入参（非 http/https 链接）", file=sys.stderr)
    if not urls:
        if media_dir is not None:
            print("错误: 除下载目录外未提供任何可下载的 http/https 链接。", file=sys.stderr)
        else:
            print("错误: 未提供任何有效 URL（需为 http:// 或 https:// 开头），入参为必填项。", file=sys.stderr)
        print("用法: python download_files.py <url1> [url2 ...]", file=sys.stderr)
        sys.exit(1)

    # 收集每个输入的处理统计，全部完成后输出执行汇总
    results: list[tuple[str, dict[str, int]]] = []
    for n, url in enumerate(urls, start=1):
        if len(urls) > 1:
            print(f"\n{'=' * 60}\n[第 {n}/{len(urls)} 个页面/媒体文件]\n{'=' * 60}")
        # 与 extract_torrents.download_torrent 保持一致：清理尾部标点，
        # 避免带句号/逗号等粘贴痕迹的直链被误判为 HTML 页面
        url = url.strip().rstrip(".,;:!?)]}。，；：！？、")
        # rmdown 中转 / .torrent 直链：直接传入种子链接，
        # 目录用种子自身标题（downloads/<种子标题或日期>/）
        if RMDOWN_LINK_RE.search(url) or TORRENT_LINK_RE.fullmatch(url):
            ok = bool(download_torrent(url, DOWNLOAD_ROOT))
            results.append((url, {"种子": 1} if ok else {}))
            continue
        # 图片/视频直链：直接下载该文件，不再按 HTML 页面解析
        # （否则 mmonly.cc 等直接返回二进制的站点会解析不出任何资源）
        if is_media_direct_url(url):
            if media_dir is None:
                media_dir = os.path.join(DOWNLOAD_ROOT, os.path.splitext(_media_filename(url))[0])
                print(f"媒体直链保存目录: {media_dir}")
            status = download_media_direct(url, media_dir)
            if status == "ok":
                results.append((url, {"媒体": 1}))
            elif status == "skip":
                results.append((url, {"媒体": 1, "跳过": 1}))
            else:
                results.append((url, {}))
            continue
        results.append((url, process_page(url)))

    # --- 执行汇总：展示每个输入及各类型的下载数量（区分成功 / 已存在跳过 / 失败） ---
    # stats 键中"跳过"与"失败"为附加明细，不计入成功项
    units = {"图片": "张", "视频": "个", "其他": "个", "媒体": "项", "种子": "个", "磁力": "条", "云盘": "条"}
    with file_logger.raw():
        print(f"\n{'=' * 60}")
        print(f"执行汇总（共 {len(urls)} 个输入）")
        print("=" * 60)
        failed_inputs = 0
        total_ok = 0
        total_skip = 0
        total_fail = 0
        for i, (url, stats) in enumerate(results, start=1):
            label = url if len(url) <= 76 else url[:73] + "..."
            ok_items = sum(v for k, v in stats.items() if k not in ("跳过", "失败"))
            skip_items = stats.get("跳过", 0)
            fail_items = stats.get("失败", 0)
            if ok_items == 0:
                # 无任何成功项：整体记为失败（页面获取失败 / 有资源但全部下载失败）
                failed_inputs += 1
                detail = f"，失败 {fail_items} 项" if fail_items else ""
                print(f"[{i}] [失败] {label}{detail}")
                total_fail += fail_items
                continue
            parts = [f"{k} {v}{units.get(k, '项')}" for k, v in stats.items() if k not in ("跳过", "失败")]
            line = f"[{i}] [成功] {label}，{', '.join(parts)}"
            if skip_items:
                line += f"，已存在跳过 {skip_items} 项"
            if fail_items:
                line += f"，失败 {fail_items} 项"
            print(line)
            total_ok += ok_items
            total_skip += skip_items
            total_fail += fail_items
        print("-" * 60)
        summary = [f"成功 {total_ok} 项"]
        if total_skip:
            summary.append(f"跳过 {total_skip} 项")
        if total_fail:
            summary.append(f"失败 {total_fail} 项")
        if failed_inputs:
            summary.append(f"失败 {failed_inputs} 个输入")
        print(f"总计: {', '.join(summary)}")

    # 正常走完整个处理流程后清理过期日志；参数错误 / 未捕获异常 / 中断时不清理，
    # 保留日志现场便于排查
    try:
        removed = file_logger.cleanup_old_logs()
        if removed:
            print(
                f"[日志清理] 已删除 {removed} 个过期日志文件"
                + f"（保留最近 {file_logger.RETENTION_DAYS} 天）"
            )
    except Exception as e:
        print(f"[日志清理] 清理过期日志异常: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
