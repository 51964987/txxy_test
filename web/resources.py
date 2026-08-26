"""资源管理：扫描 downloads/ 目录（只读），按文件夹分组返回文件清单。

增量缓存：目录签名（顶层文件夹名 + 各自 mtime）未变化时直接复用上次扫描结果，
避免高频请求下对大型目录反复 rglob 全量扫描。
"""
import time
from pathlib import Path

import config

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".flv", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".m3u8"}
TORRENT_EXTS = {".torrent"}
TEXT_EXTS = {".txt", ".md", ".log"}


def category_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in TORRENT_EXTS:
        return "torrent"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


# ---- 增量缓存：签名 = 顶层文件夹名 + 各自 mtime（新增/删除/覆盖文件都会引起目录 mtime 变化） ----
_cache_signature = ""
_cache_payload: dict | None = None


def _signature(root: Path) -> str:
    parts: list[tuple[str, float]] = []
    try:
        for p in root.iterdir():
            if p.is_dir():
                try:
                    parts.append((p.name, p.stat().st_mtime))
                except OSError:
                    parts.append((p.name, -1.0))
    except OSError:
        pass
    parts.sort(key=lambda t: t[0])
    return "|".join(f"{n}:{m:.3f}" for n, m in parts)


def scan() -> dict:
    root = config.DOWNLOADS_DIR
    if not root.is_dir():
        return {"count": 0, "total_files": 0, "total_size": 0, "items": []}

    global _cache_signature, _cache_payload
    sig = _signature(root)
    if _cache_payload is not None and sig == _cache_signature:
        return _cache_payload

    items: list[dict] = []
    total_files = 0
    total_size = 0
    for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not folder.is_dir():
            continue
        files: list[dict] = []
        folder_size = 0
        try:
            for f in sorted(folder.rglob("*"), key=lambda p: str(p).lower()):
                if f.is_file():
                    try:
                        size = f.stat().st_size
                    except OSError:
                        size = 0
                    folder_size += size
                    files.append(
                        {
                            "name": f.name,
                            "rel_path": str(f.relative_to(root)).replace("\\", "/"),
                            "size": size,
                            "category": category_of(f.name),
                        }
                    )
        except OSError:
            continue
        if not files:
            continue
        try:
            mtime = folder.stat().st_mtime
        except OSError:
            mtime = 0
        total_files += len(files)
        total_size += folder_size
        items.append(
            {
                "name": folder.name,
                "file_count": len(files),
                "total_size": folder_size,
                "mtime": mtime,
                "files": files,
            }
        )

    payload = {
        "count": len(items),
        "total_files": total_files,
        "total_size": total_size,
        "items": items,
    }
    _cache_signature = sig
    _cache_payload = payload
    return payload
