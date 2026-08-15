"""
图片相关模块：图片地址提取 + 图片下载（请求头 / 内容校验 / 下载函数全部集中于此）

由 download_files.py 调用，保持下载主流程只关注页面访问与编排：
    from extract_images import (
        extract_image_urls, is_gif_url,
        download_image, GIF_SUBDIR, JPG_SUBDIR,
    )
"""
import os
import re
from urllib.parse import urljoin, urlparse

import requests

# 通用下载核心（独立模块，无循环依赖）
from media_download import download_media

# ============ 图片下载配置 ============
# 浏览器 UA（与 download_files.HEADERS 保持一致，改动时需同步）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 图片下载专用请求头：Accept 必须是纯图片类型（不能含 text/html），
# 否则 EasyImage 等图床会判定为"浏览器直接打开图片"而 302 到广告查看页
IMG_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
# 图片按类型分目录：GIF 单独存放，其余图片统一放 jpgs
GIF_SUBDIR = "gifs"
JPG_SUBDIR = "jpgs"
# 图片完整扩展名集合（供主文件识别媒体直链，与 is_gif_url 共用判断基准）
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg", ".ico")

# ============ 图片地址提取 ============
# 匹配 <img> 标签（用于 BeautifulSoup 解析失败时的正则兜底）
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# 常见懒加载/图片属性（按优先级排列，data-link 是跳转页链接不取；
# 注意必须是元组（带逗号），写成 ("src") 会被当作字符串导致遍历字符）
_IMG_ATTRS = ("src", "ess-data", "iyl-data", "data-original", "data-src", "data-lazy-src")
# 占位图/广告拦截图特征（命中则跳过）
_PLACEHOLDER_PATTERNS = (
    "adblo_ck",
    "blank.gif",
    "spacer.gif",
    "1x1.gif",
    "pixel.gif",
    "placeholder",
    "loading.gif",
    "transparent",
)


def _is_placeholder(url: str) -> bool:
    """判断 URL 是否为占位图/广告拦截图"""
    lower = url.lower()
    return any(p in lower for p in _PLACEHOLDER_PATTERNS)


def is_gif_url(url: str) -> bool:
    """判断图片 URL 是否指向 GIF（按路径扩展名，不区分大小写）"""
    ext = os.path.splitext(urlparse(url).path)[1] or ".jpg"
    return ext.lower() == ".gif"


def is_image_url(url: str) -> bool:
    """判断 URL 是否为图片文件直链（按路径扩展名，小写匹配）"""
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext in IMAGE_EXTS


def needs_split_dirs(image_urls: list[str]) -> bool:
    """是否按 gif/jpg 分目录：仅当页面同时存在 gif 与 jpg 时返回 True"""
    has_gif = any(is_gif_url(u) for u in image_urls)
    has_jpg = any(not is_gif_url(u) for u in image_urls)
    return has_gif and has_jpg


def image_save_path(
    save_dir: str,
    img_url: str,
    index: int,
    split_dirs: bool,
    gif_idx: int,
    jpg_idx: int,
) -> tuple[str, bool, int, int]:
    """
    生成单张图片保存路径（扩展名提取/过滤 + gif/jpg 分目录 + 编号命名）。
    返回 (save_path, is_gif, 更新后的 gif_idx, 更新后的 jpg_idx)；
    split_dirs=False（单一类型）时按页面顺序连续编号，直接存 save_dir 根目录。
    """
    ext = os.path.splitext(urlparse(img_url).path)[1] or ".jpg"
    ext = ext.split("?")[0][:8]  # 过滤 query 并限制长度
    is_gif = is_gif_url(img_url)
    if split_dirs and is_gif:
        gif_idx += 1
        return os.path.join(save_dir, GIF_SUBDIR, f"{gif_idx:03d}{ext}"), True, gif_idx, jpg_idx
    if split_dirs:
        jpg_idx += 1
        return os.path.join(save_dir, JPG_SUBDIR, f"{jpg_idx:03d}{ext}"), False, gif_idx, jpg_idx
    return os.path.join(save_dir, f"{index:03d}{ext}"), is_gif, gif_idx, jpg_idx


def extract_image_urls(html: str, base_url: str) -> list[str]:
    """
    提取页面中所有图片地址，去重保序。
    兼容多懒加载属性（ess-data / iyl-data / data-src 等），
    并过滤占位图；BeautifulSoup 解析不到时用正则兜底。
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    img_tags = soup.find_all("img")

    # 兜底：soup 解析异常时，用正则逐个抓取 <img> 标签再解析
    if not img_tags:
        for match in _IMG_TAG_RE.finditer(html):
            node = BeautifulSoup(match.group(0), "html.parser").find("img")
            if node:
                img_tags.append(node)

    urls: list[str] = []
    seen: set[str] = set()
    for img in img_tags:
        # 收集所有候选图片地址（跳过 base64 内联图）
        candidates: list[str] = []
        for attr in _IMG_ATTRS:
            val = img.get(attr)
            if not val:
                continue
            s = str(val).strip()
            if not s or s.startswith("data:"):
                continue
            candidates.append(urljoin(base_url, s))

        if not candidates:
            continue

        # 优先取非占位图地址；若全部是占位图，取第一个候选
        real = next((u for u in candidates if not _is_placeholder(u)), candidates[0])
        if real not in seen:
            seen.add(real)
            urls.append(real)
    return urls


# ============ 图片内容校验与下载 ============
def _looks_like_image(data: bytes) -> bool:
    """根据文件头 magic bytes 判断是否为常见图片格式（防止存下 HTML 反爬页）"""
    if len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):  # GIF
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":  # WebP
        return True
    if data.startswith(b"BM"):  # BMP
        return True
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):  # AVIF
        return True
    if data[:5] == b"<?xml" or data[:4] == b"<svg":  # SVG（文本格式）
        return True
    return False


def download_image(session: requests.Session, url: str, save_path: str, referer: str) -> bool:
    """下载单张图片，返回是否成功（带 Referer 降级重试）"""
    return download_media(session, url, save_path, referer, IMG_HEADERS, _looks_like_image, "图片")
