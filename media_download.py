"""
通用下载核心：图片/视频共用的下载与重试逻辑（独立模块，避免模块间循环依赖）

依赖方向（单向）：
    extract_images.py / extract_videos.py / download_files.py  →  media_download.py

用法:
    from media_download import download_media, TIMEOUT
    ok = download_media(session, url, save_path, referer, headers, validator, "图片")
"""
import os
from typing import Callable

import requests

# 下载超时（秒）
TIMEOUT = 20


def _remove_partial(save_path: str) -> None:
    """删除半截/非法文件，便于下次重下"""
    if os.path.exists(save_path):
        try:
            os.remove(save_path)
        except OSError:
            pass


def _download_once(
    session: requests.Session,
    url: str,
    save_path: str,
    headers: dict[str, str],
    validator: Callable[[bytes], bool],
    kind: str = "文件",
) -> tuple[bool, str]:
    """执行一次下载，返回 (是否成功, 状态码或失败原因)；内容类型不符视为失败并清理文件"""
    status = ""
    try:
        #print(f"[开始] {os.path.basename(save_path)}（{url}）")
        with session.get(url, headers=headers, timeout=TIMEOUT, stream=True) as resp:
            status = str(resp.status_code)
            if resp.status_code != 200:
                return False, status
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        _ = f.write(chunk)
    except Exception as e:
        _remove_partial(save_path)
        return False, f"{type(e).__name__}: {e}"

    # 校验下载内容类型（部分图床对异常请求返回 200 + HTML 错误页）
    with open(save_path, "rb") as f:
        head = f.read(16)
    if not head or not validator(head):
        _remove_partial(save_path)
        return False, f"内容非{kind}"
    return True, status


# 网络层异常关键字（超时/连接失败等多为暂时性，值得降级重试一次）
_NETWORK_ERR_HINTS = (
    "ConnectionError",
    "Timeout",
    "ReadTimeout",
    "ConnectTimeout",
    "Read timed out",
    "Max retries",
    "SSLError",
    "ProxyError",
    "RemoteDisconnected",
    "ChunkedEncodingError",
)


def _should_retry_without_referer(status: str) -> bool:
    """带 Referer 尝试失败后，判断是否值得不带 Referer 重试一次"""
    if status in ("403", "401", "429") or status.startswith("内容非"):
        return True
    return any(hint in status for hint in _NETWORK_ERR_HINTS)


def download_media(
    session: requests.Session,
    url: str,
    save_path: str,
    referer: str,
    headers: dict[str, str],
    validator: Callable[[bytes], bool],
    kind: str,
) -> bool:
    """
    通用下载核心：下载单个媒体文件（图片/视频），返回是否成功。
    第一次带 Referer 下载（多数图床防盗链要求）；
    若被拒（403/401/429、返回内容类型不符或网络层异常如超时），
    降级为不带 Referer 重试一次
    （目标站点媒体常带 referrerpolicy="no-referrer"，无 Referer 亦可能放行）。

    headers / validator / kind 由调用方（extract_images.download_image 等）提供：
        - 图片：IMG_HEADERS + extract_images._looks_like_image + "图片"
        - 视频：VIDEO_HEADERS + extract_videos._looks_like_video + "视频"
    """
    # 已存在且非空则跳过（断点续传）
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        print(f"[跳过] 已存在: {os.path.basename(save_path)},{url}")
        return True

    # 尝试 1：带 Referer
    headers1 = dict(headers)
    headers1["Referer"] = referer
    ok, status = _download_once(session, url, save_path, headers1, validator, kind)
    if ok:
        print(f"[完成] {os.path.basename(save_path)}（{os.path.getsize(save_path)} 字节,{url}）")
        return True

    # 尝试 2：不带 Referer（模拟 referrerpolicy="no-referrer"）
    if _should_retry_without_referer(status):
        ok, status2 = _download_once(session, url, save_path, dict(headers), validator, kind)
        if ok:
            print(f"[完成] {os.path.basename(save_path)}（{os.path.getsize(save_path)} 字节，无 Referer）,{url}")
            return True
        print(f"[失败] {os.path.basename(save_path)}（带 Referer: {status}；无 Referer: {status2}）,{url}")
        return False

    print(f"[失败] {os.path.basename(save_path)}（{status}）,{url}")
    return False
