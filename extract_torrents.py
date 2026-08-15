"""
torrent/种子相关模块：种子链接提取 + 种子下载（rmdown 中转解析 / 直链下载 / 内容校验 / 标题解析全部集中于此）

由 download_files.py 调用，保持下载主流程只关注页面访问与编排：
    from extract_torrents import (
        extract_other_urls, download_torrent, sanitize_title,
        RMDOWN_LINK_RE, TORRENT_LINK_RE,
    )
"""
import os
import re
import time
from typing import cast
from urllib.parse import unquote, urljoin, urlparse

import requests

# 通用下载核心（独立模块，无循环依赖）
from media_download import TIMEOUT

# ============ 种子下载配置 ============
# 浏览器 UA（与 download_files.HEADERS 保持一致，改动时需同步）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 种子解析/下载请求头（与 download_files.HEADERS 一致，
# rmdown 解析页需要 Accept 含 text/html）
TORRENT_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# rmdown.com 中转链接（如 https://www.rmdown.com/link.php?hash=...）
# hash 长度不固定（实测 40~43 位十六进制），用 + 兜底
RMDOWN_LINK_RE = re.compile(
    r"https?://(?:www\.)?rmdown\.com/link\.php\?hash=([0-9a-f]+)", re.IGNORECASE
)
# .torrent 直接下载链接
TORRENT_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.torrent(?:\?[^\s\"'<>]*)?", re.IGNORECASE
)
# rmdown 站点限流（429）时等待秒数
RMDOWN_LIMIT_WAIT = 10

# 文件系统非法字符（Windows / Linux 通用）
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


# ============ 标题清理（页面标题 / 种子标题共用） ============
def sanitize_title(title: str) -> str:
    """清理标题中的非法字符，用作目录名"""
    cleaned = _INVALID_CHARS.sub("_", title).strip()
    cleaned = cleaned.strip(".")  # 避免以点结尾（Windows 不允许）
    return cleaned[:80] or time.strftime("%Y-%m-%d")


# ============ 种子地址提取 ============
def extract_other_urls(html: str, base_url: str) -> list[str]:
    """
    提取除图片、视频之外的其他媒体/资源地址（种子文件等）。

    当前支持：
      - rmdown.com 中转链接（rmdown.com/link.php?hash=...）
      - .torrent 直接下载链接
    后续需要支持新类型（音频/压缩包等）时仿照此模式继续追加即可，
    下载逻辑统一走 download_torrent()。
    """
    _ = base_url  # 预留参数：后续提取相对地址时用于拼接
    urls: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"https?://[^\s\"'<>]+", html):
        raw = m.group(0).rstrip(".,;:!?)]}")
        if raw in seen:
            continue
        if RMDOWN_LINK_RE.search(raw) or TORRENT_LINK_RE.fullmatch(raw):
            seen.add(raw)
            urls.append(raw)
    return urls


def _extract_rmdown_dl_url(html: str, base_url: str) -> str | None:
    """
    解析 rmdown link.php 页面中 #dl 表单的隐藏字段（des/esc/axs/reff/ref），
    构造真实下载地址 download.php?<字段>...，返回完整 URL；失败返回 None。
    """
    form_m = re.search(r'<form\b[^>]*id="dl"[^>]*>(.*?)</form>', html, re.IGNORECASE | re.DOTALL)
    if not form_m:
        print("  [错误] 页面中未找到下载表单 (#dl)")
        return None
    fields: list[tuple[str, str]] = re.findall(
        r'<input\b[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form_m.group(1), re.IGNORECASE
    )
    if not fields:
        print("  [错误] 下载表单中未找到隐藏字段")
        return None
    query = "&".join(f"{name}={value}" for name, value in fields)
    return f"{urljoin(base_url, 'download.php')}?{query}"


def _fix_filename_encoding(name: str) -> str:
    """
    修复 Content-Disposition 文件名的编码乱码。

    requests 将响应头按 latin-1 解码，服务器发送的中文文件名（UTF-8 字节）
    会变成 latin-1 乱码（如 無碼中字 → ç\x84¡ç¢¼ä¸\xadå\xad\x97），
    这里按常见乱码路径依次还原：UTF-8 被 latin-1 解码 → 被 GBK 解码 →
    GBK 被 latin-1 解码；均失败则保留原值。
    """
    if not name:
        return name
    # 场景 1：UTF-8 字节被 latin-1 解码（requests headers 最常见）
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # 场景 2：UTF-8 字节被 GBK 解码
    try:
        return name.encode("gbk").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    # 场景 3：GBK 字节被 latin-1 解码
    try:
        return name.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


def _parse_cd_filename(cd: str) -> str | None:
    """
    从 Content-Disposition 头解析文件名（含乱码修复），无则返回 None。
    优先级：filename*=（RFC 5987，URL 解码）→ filename=（乱码修复 + URL 解码）。
    """
    if not cd:
        return None
    # 1. filename*=UTF-8''<urlencoded>（RFC 5987）
    m = re.search(r"filename\*\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        m2 = re.match(r"([^']+)'[^']*'(.+)", m.group(1).strip())
        if m2:
            charset, encoded = m2.group(1), m2.group(2)
            try:
                name = unquote(encoded)
                if charset.lower() not in ("utf-8", "utf8"):
                    name = name.encode("latin-1", errors="ignore").decode(charset, errors="replace")
                return name
            except Exception:
                pass
    # 2. filename="..."
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    if "%" in name:  # 部分站点 filename 内为 URL 编码
        try:
            name = unquote(name)
        except Exception:
            pass
    return _fix_filename_encoding(name)


def _pick_torrent_filename(resp: requests.Response, file_hash: str) -> str:
    """选择 torrent 文件名：优先 Content-Disposition，其次用 hash 命名"""
    name = _parse_cd_filename(resp.headers.get("Content-Disposition", ""))
    if name and name.lower().endswith(".torrent"):
        return name
    return f"{file_hash}.torrent"


def _download_rmdown_torrent(link_url: str, file_hash: str, save_dir: str, dir_name: str | None = None) -> str | None:
    """下载 rmdown.com 中转链接背后的 .torrent 文件，返回保存路径或 None"""
    print(f"正在解析 rmdown 下载页: {link_url}")
    try:
        resp = requests.get(link_url, headers=TORRENT_HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            print(f"  [错误] 解析页返回状态码: {resp.status_code}")
            return None
        dl_url = _extract_rmdown_dl_url(resp.text, link_url)
        if not dl_url:
            return None
        print(f"解析到真实下载地址: {dl_url}")
    except Exception as e:
        print(f"  [错误] 解析失败: {e}")
        return None

    # 下载 torrent：站点可能限流 (429)，等待后重试
    data: bytes | None = None
    filename: str = ""
    for attempt in range(1, 4):
        try:
            r = requests.get(dl_url, headers=TORRENT_HEADERS, timeout=TIMEOUT, stream=True)
        except Exception as e:
            print(f"  [错误] 下载请求失败: {e}")
            return None
        if r.status_code == 429:
            print(f"  [限流] 站点限流 (429)，等待 {RMDOWN_LIMIT_WAIT} 秒后重试 ({attempt}/3)")
            r.close()
            time.sleep(RMDOWN_LIMIT_WAIT)
            continue
        if r.status_code != 200:
            print(f"  [错误] 下载返回状态码: {r.status_code}")
            r.close()
            return None
        data = r.content
        filename = _pick_torrent_filename(r, file_hash)
        r.close()
        break
    if data is None:
        print("[错误] 多次限流，下载失败")
        return None

    # 校验 torrent 文件头：Bencoding 格式以 b"d" 开头
    if not data.startswith(b"d"):
        print(f"  [错误] 返回内容不是 torrent 文件（文件头: {data[:16]!r}）")
        return None

    return _save_torrent(data, save_dir, filename, dir_name)


def _download_direct_torrent(url: str, save_dir: str, dir_name: str | None = None) -> str | None:
    """下载 .torrent 直链文件，返回保存路径或 None"""
    print(f"正在下载: {url}")
    try:
        r = requests.get(url, headers=TORRENT_HEADERS, timeout=TIMEOUT, stream=True)
        if r.status_code != 200:
            print(f"  [错误] 下载返回状态码: {r.status_code}")
            r.close()
            return None
        data = r.content
        headers = r.headers
        r.close()
    except Exception as e:
        print(f"  [错误] 下载请求失败: {e}")
        return None

    if not data.startswith(b"d"):
        print(f"  [错误] 返回内容不是 torrent 文件（文件头: {data[:16]!r}）")
        return None

    # 文件名：优先 Content-Disposition，其次 URL 文件名（URL 解码还原中文）
    filename = _parse_cd_filename(headers.get("Content-Disposition", ""))
    if not (filename and filename.lower().endswith(".torrent")):
        filename = unquote(os.path.basename(urlparse(url).path))
    if not filename.lower().endswith(".torrent"):
        filename += ".torrent"
    return _save_torrent(data, save_dir, filename, dir_name)


def _bdecode(data: bytes, pos: int) -> tuple[object, int]:
    """递归解析 bencode 数据，返回 (值, 下一个位置)；格式错误抛异常"""
    c = data[pos : pos + 1]
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1 : end]), end + 1
    if c == b"l":
        pos += 1
        items: list[object] = []
        while data[pos : pos + 1] != b"e":
            v, pos = _bdecode(data, pos)
            items.append(v)
        return items, pos + 1
    if c == b"d":
        pos += 1
        d: dict[object, object] = {}
        while data[pos : pos + 1] != b"e":
            k, pos = _bdecode(data, pos)
            v, pos = _bdecode(data, pos)
            d[k] = v
        return d, pos + 1
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    start = colon + 1
    return data[start : start + length], start + length


def _decode_torrent_text(data: bytes) -> str:
    """
    尝试多种编码解码 torrent 文本字段，避免中文乱码。
    依次尝试 UTF-8 → GB18030 → BIG5，全部失败则 latin-1 兜底（不抛异常）。
    """
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _torrent_title(data: bytes) -> str | None:
    """从 torrent 内容解析标题（info.name 字段，智能编码解码），失败返回 None"""
    try:
        top, _ = _bdecode(data, 0)
        if not isinstance(top, dict):
            return None
        info = cast(dict[bytes, object], top).get(b"info")
        if not isinstance(info, dict):
            return None
        name = cast(dict[bytes, object], info).get(b"name")
        if not isinstance(name, bytes):
            return None
        text = _decode_torrent_text(name).strip()
        return text or None
    except Exception:
        pass
    return None


def _save_torrent(data: bytes, save_dir: str, filename: str, dir_name: str | None = None) -> str | None:
    """
    保存 torrent 数据到目录，返回保存路径或 None。

    目录规则（两种入口对应两种目录）：
      - 传入 dir_name（页面场景，调用方传页面标题）：
        保存到 save_dir/<dir_name>/，目录名用页面标题，不用种子自身标题
      - 不传 dir_name（直接传入种子链接场景）：
        保存到 save_dir/<种子标题或日期>/，标题解析自种子 info.name，
        解析不到则用当天日期

    目标文件已存在且大小一致时跳过保存（避免重复下载），返回已有文件路径。
    """
    if dir_name is None:
        dir_name = _torrent_title(data) or time.strftime("%Y-%m-%d")
    target_dir = os.path.join(save_dir, sanitize_title(dir_name))
    try:
        save_path = os.path.join(target_dir, filename)
        if os.path.isfile(save_path) and os.path.getsize(save_path) == len(data):
            print(f"已存在，跳过: {save_path}（避免重复下载）")
            return save_path
        os.makedirs(target_dir, exist_ok=True)
        with open(save_path, "wb") as f:
            _ = f.write(data)
        print(f"已保存: {save_path} ({len(data)} 字节)")
        return save_path
    except Exception as e:
        print(f"  [错误] 保存失败: {e}")
        return None


def download_torrent(url: str, save_dir: str = ".", dir_name: str | None = None) -> str | None:
    """
    下载 .torrent 文件，返回保存路径或 None。

    支持两类链接：
      - rmdown.com 中转链接（rmdown.com/link.php?hash=...，自动解析真实下载地址）
      - .torrent 直接下载链接

    目录规则（两种入口对应两种目录）：
      - 传入 dir_name（页面场景，调用方传页面标题）：
        保存到 save_dir/<页面标题>/，不使用种子自身标题
      - 不传 dir_name（直接传入种子链接场景）：
        保存到 save_dir/<种子标题或日期>/，标题取自种子 info.name 字段，
        解析不到时用当天日期

    目标文件已存在时跳过，避免重复下载。
    """
    # 清理两侧空白与行尾标点（复制粘贴的链接常带句号/逗号等），
    # 避免 TORRENT_LINK_RE.fullmatch 因尾部标点匹配失败
    url = url.strip().rstrip(".,;:!?)]}。，；：！？、")
    hash_m = RMDOWN_LINK_RE.search(url)
    if hash_m:
        return _download_rmdown_torrent(url, hash_m.group(1), save_dir, dir_name)
    if TORRENT_LINK_RE.fullmatch(url):
        return _download_direct_torrent(url, save_dir, dir_name)
    print(f"[错误] 不支持的链接类型: {url}")
    return None
