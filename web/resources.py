"""资源管理：扫描 downloads/ 目录（只读），按文件夹分组返回文件清单。

增量缓存：目录签名（顶层文件夹名 + 各自 mtime）未变化时直接复用上次扫描结果，
避免高频请求下对大型目录反复 rglob 全量扫描。

配套能力（B1/B5/B8 资源管理页优化）：
- source_lookup：目录名（= 帖子页面标题）匹配 posts 表，回溯资源来源帖（只读查询）；
- resolve_safe / PREVIEW_TYPES：受控文件路径解析（限定 downloads/ 内 + 图片扩展名白名单），
  供预览接口 inline 返回图片；
- open_folder：调起系统文件管理器打开 downloads/ 下的目录（仅 Windows 生效）。
"""
import os
import time
from pathlib import Path
from typing import Any

import config
import db

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".flv", ".mkv", ".avi", ".mov", ".m4v", ".ts", ".m3u8"}
TORRENT_EXTS = {".torrent"}
TEXT_EXTS = {".txt", ".md", ".log"}

# 预览接口允许 inline 返回的扩展名 → Content-Type（仅图片，B5）
PREVIEW_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}


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
_cache_payload: dict[str, Any] | None = None


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


def scan() -> dict[str, Any]:
    root = config.DOWNLOADS_DIR
    if not root.is_dir():
        return {"count": 0, "total_files": 0, "total_size": 0, "items": []}

    global _cache_signature, _cache_payload
    sig = _signature(root)
    if _cache_payload is not None and sig == _cache_signature:
        return _cache_payload

    items: list[dict[str, Any]] = []
    total_files = 0
    total_size = 0
    for folder in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not folder.is_dir():
            continue
        files: list[dict[str, Any]] = []
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


# ---- B1 来源回溯：目录名（= 帖子页面标题）匹配 posts 表（只读查询，不写库） ----
def source_lookup(name: str) -> dict[str, Any]:
    """按目录名回溯来源帖：精确命中优先（title 主键索引），未命中再做双向模糊匹配。

    目录名即下载时的页面标题，正常场景精确即可命中；目录名可能经标题清理
    （特殊字符被替换），故补充「库内标题含目录名 / 目录名含库内标题」双向 LIKE 兜底，
    多条命中时取入库时间最新一条。仅展示用途，模糊匹配不做转义特判。
    """
    name = (name or "").strip()
    if not name:
        return {"matched": False}

    rows = db.query(
        "SELECT title, fid, date, url, author, created_at FROM posts WHERE title = ? LIMIT 1",
        (name,),
    )
    if not rows:
        like = f"%{name}%"
        rows = db.query(
            "SELECT title, fid, date, url, author, created_at FROM posts" +
            " WHERE title LIKE ? ESCAPE '\\' OR ? LIKE ('%' || title || '%')" +
            " ORDER BY created_at DESC LIMIT 1",
            (like, name),
        )
    if not rows:
        return {"matched": False}

    r = rows[0]
    return {
        "matched": True,
        "title": r["title"],
        "fid": r["fid"],
        "fid_name": config.fid_name(r["fid"]),
        "date": r["date"],
        "author": r["author"] or "",
        "url": db.normalize_url(r["url"]),
    }


# ---- B5 图片预览：受控路径解析（限定 downloads/ 内 + 图片扩展名白名单） ----
def resolve_safe(rel: str) -> Path | None:
    """把相对路径解析为 downloads/ 内的绝对路径；越界 / 不存在 / 非文件返回 None。

    防路径穿越：resolve 后要求目标位于 downloads/ 之内（root 自身或其后代）。
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel.lower().startswith(("http://", "https://")):
        return None
    root = config.DOWNLOADS_DIR.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    return target if target.is_file() else None


# ---- B8 打开所在目录：调起系统文件管理器（仅 Windows 生效） ----
def open_folder(rel: str) -> bool:
    """用系统文件管理器打开 downloads/ 下的目录（rel 为空时打开 downloads/ 根目录）。

    路径同样限定在 downloads/ 内；非 Windows 平台无 os.startfile，抛 OSError 由接口层转 501。
    """
    rel = (rel or "").strip().replace("\\", "/").lstrip("/")
    root = config.DOWNLOADS_DIR.resolve()
    if rel:
        try:
            target = (root / rel).resolve()
        except OSError:
            return False
        if target != root and root not in target.parents:
            return False
    else:
        target = root
    if not target.is_dir():
        return False
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("当前系统不支持打开本地目录（仅 Windows 支持）")
    startfile(str(target))
    return True
