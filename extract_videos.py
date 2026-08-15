"""
视频相关模块：视频地址提取 + 视频下载（请求头 / 内容校验 / 下载函数全部集中于此）

由 download_files.py 调用，保持下载主流程只关注页面访问与编排：
    from extract_videos import extract_video_urls, download_video, VIDEO_SUBDIR
"""
import os
import re
from urllib.parse import urljoin, urlparse

import requests

# 通用下载核心（独立模块，无循环依赖）
from media_download import download_media

# ============ 视频下载配置 ============
# 浏览器 UA（与 download_files.HEADERS 保持一致，改动时需同步）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 视频下载专用请求头（同理不能含 text/html，模拟 <video> 标签加载）
VIDEO_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "video/webm,video/mp4,video/ogg,video/*;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# 视频保存子目录（相对于图片目录）
VIDEO_SUBDIR = "videos"
# 视频完整扩展名集合（供主文件识别媒体直链）
VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".ts")

# ============ 视频地址提取 ============
# 视频在 <video> 标签中常见属性（按优先级排列）
_VIDEO_ATTRS = ("src", "data-src", "data-video", "data-source", "data-src-mp4")
# 视频文件扩展名（用于识别 <a> 链接与正则兜底；与 VIDEO_EXTS 保持一致）
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|webm|mkv|mov|avi|flv|ts|m4v)(?:[?#]|$)", re.IGNORECASE)


def extract_video_urls(html: str, base_url: str) -> list[str]:
    """
    提取页面中所有视频地址，去重保序。
    来源：<video> 标签属性、<source> 子标签、<a> 视频文件链接，
    最后用正则兜底抓取全文 .mp4 等链接（播放器 JS 配置地址）。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    # 1. <video> 标签（取第一个有效属性）
    for v in soup.find_all("video"):
        for attr in _VIDEO_ATTRS:
            val = v.get(attr)
            if not val:
                continue
            s = str(val).strip()
            if s and not s.startswith("data:"):
                add(urljoin(base_url, s))
                break

    # 2. <source> 标签
    for src_tag in soup.find_all("source"):
        s = str(src_tag.get("src") or "").strip()
        if s and not s.startswith("data:"):
            add(urljoin(base_url, s))

    # 3. <a> 链接指向视频文件
    for a in soup.find_all("a"):
        href = str(a.get("href") or "").strip()
        if href and _VIDEO_EXT_RE.search(href):
            add(urljoin(base_url, href))

    # 4. 正则兜底：全文中的视频文件链接（如播放器 JS 配置）
    if not urls:
        for m in re.finditer(
            r"https?://[^\"'\s<>]+\.(?:mp4|webm|mkv|mov|avi|flv|ts|m4v)(?:[?#][^\"'\s<>]*)?",
            html,
            re.IGNORECASE,
        ):
            add(m.group(0))

    return urls


# ============ 视频内容校验与下载 ============
def is_video_url(url: str) -> bool:
    """判断 URL 是否为视频文件直链（按路径扩展名，小写匹配）"""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext in VIDEO_EXTS


def video_save_path(save_dir: str, v_url: str, index: int) -> str:
    """生成视频保存路径（扩展名提取/过滤 + 按顺序编号命名）"""
    ext = os.path.splitext(urlparse(v_url).path)[1] or ".mp4"
    ext = ext.split("?")[0][:8]  # 过滤 query 并限制长度
    return os.path.join(save_dir, f"{index:03d}{ext}")


def _looks_like_video(data: bytes) -> bool:
    """根据文件头 magic bytes 判断是否为常见视频格式（MP4/MOV/WebM/FLV）"""
    if len(data) < 12:
        return False
    if data[4:8] == b"ftyp" and data[8:12] not in (b"avif", b"avis"):  # MP4/MOV/M4V
        return True
    if data.startswith(b"\x1a\x45\xdf\xa3"):  # WebM/Matroska
        return True
    if data.startswith(b"FLV"):  # FLV
        return True
    return False


def download_video(session: requests.Session, url: str, save_path: str, referer: str) -> bool:
    """下载单个视频，返回是否成功（带 Referer 降级重试）"""
    return download_media(session, url, save_path, referer, VIDEO_HEADERS, _looks_like_video, "视频")
