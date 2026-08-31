"""资源管理：扫描 downloads/ 目录（只读），按文件夹分组返回文件清单。

增量缓存：目录签名（顶层文件夹名 + 各自 mtime）未变化时直接复用上次扫描结果，
避免高频请求下对大型目录反复 rglob 全量扫描。

配套能力（B1/B5/B8 资源管理页优化）：
- source_lookup：目录名（= 帖子页面标题）匹配 posts 表，回溯资源来源帖（只读查询）；
- resolve_safe / PREVIEW_TYPES：受控文件路径解析（限定 downloads/ 内 + 图片扩展名白名单），
  供预览接口 inline 返回图片；
- open_folder：调起系统文件管理器打开 downloads/ 下的目录（仅 Windows 生效）；
- PLAYABLE_TYPES：视频播放白名单（覆盖 VIDEO_EXTS，Range 由 FileResponse 原生支持）；
- 回收站（软删除）：move_to_trash / list_trash / restore_trash / purge_trash，
  删除的资源移入 outputs/trash/ 保留 TRASH_KEEP_DAYS 天，不写库。
"""
import json
import math
import os
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
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


# ---- 视频播放：受控路径解析（限定 downloads/ 内 + 视频扩展名白名单） ----
# 覆盖 VIDEO_EXTS 全部格式；浏览器能否解码取决于编码（H.264/AAC 最稳），
# 不可播时由前端提示改用本地播放器。
PLAYABLE_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
    ".m3u8": "application/vnd.apple.mpegurl",
}


def resolve_safe_dir(rel: str) -> Path | None:
    """目录版受控路径解析：限定 downloads/ 内且为已存在目录；downloads/ 根自身不允许。"""
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    if not rel or rel.lower().startswith(("http://", "https://")):
        return None
    root = config.DOWNLOADS_DIR.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if target == root or root not in target.parents:
        return None
    return target if target.is_dir() else None


def invalidate_cache() -> None:
    """主动失效扫描缓存。

    签名只取顶层目录名 + mtime，而删除子文件不保证触发顶层目录 mtime 变化，
    因此删除 / 恢复 / 彻底删除后必须显式失效，避免列表继续显示已删除项。
    """
    global _cache_signature, _cache_payload
    _cache_signature = ""
    _cache_payload = None


# ---- 回收站（软删除）：移入 outputs/trash/<id>/，保留 TRASH_KEEP_DAYS 天 ----
_TRASH_INDEX = config.TRASH_DIR / "index.json"
_trash_lock = Lock()


def _load_trash() -> list[dict[str, Any]]:
    """读取回收站清单；文件缺失或损坏时返回空列表"""
    if not _TRASH_INDEX.is_file():
        return []
    try:
        with open(_TRASH_INDEX, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("items", []))
    except (OSError, ValueError):
        return []


def _save_trash(items: list[dict[str, Any]]) -> None:
    """原子写入回收站清单（先写临时文件再替换，避免中断损坏）"""
    config.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _TRASH_INDEX.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": items}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _TRASH_INDEX)


def _path_size(p: Path) -> int:
    """文件或目录总大小（统计失败按 0 计）"""
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                continue
    return total


def move_to_trash(rel: str, is_dir: bool) -> dict[str, Any]:
    """把 downloads/ 下的文件或目录移入回收站（软删除，保留期内可恢复）"""
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    target = resolve_safe_dir(rel) if is_dir else resolve_safe(rel)
    root = config.DOWNLOADS_DIR.resolve()
    if target is None:
        return {"ok": False, "reason": "路径不存在或越界"}
    if target == root:
        return {"ok": False, "reason": "不允许删除 downloads 根目录"}

    size = _path_size(target)
    item_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    dest = config.TRASH_DIR / item_id / Path(rel).name
    with _trash_lock:
        items = _load_trash()
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(dest))
        except OSError as e:
            return {"ok": False, "reason": f"移动失败，文件可能被占用: {e}"}
        items.append(
            {
                "id": item_id,
                "rel": rel,
                "name": Path(rel).name,
                "is_dir": is_dir,
                "size": size,
                "deleted_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        _save_trash(items)
    invalidate_cache()
    return {"ok": True, "id": item_id, "rel": rel, "size": size}


def list_trash() -> dict[str, Any]:
    """回收站清单：含每项是否已过期与剩余保留天数"""
    keep = config.TRASH_KEEP_DAYS
    now = datetime.now()
    out: list[dict[str, Any]] = []
    for it in _load_trash():
        try:
            deleted = datetime.fromisoformat(it["deleted_at"])
            age_days = (now - deleted).total_seconds() / 86400.0
        except (ValueError, KeyError):
            age_days = float(keep)
        out.append({**it, "expired": age_days > keep, "remain_days": max(0, math.ceil(keep - age_days))})
    return {"items": out, "keep_days": keep, "total_size": sum(int(i.get("size") or 0) for i in out)}


def restore_trash(item_id: str) -> dict[str, Any]:
    """从回收站恢复到 downloads/ 原路径；目标已存在时拒绝，避免覆盖现有文件"""
    with _trash_lock:
        items = _load_trash()
        hit = next((i for i in items if i.get("id") == item_id), None)
        if hit is None:
            return {"ok": False, "reason": "回收站中不存在该项"}
        src = config.TRASH_DIR / str(hit["id"]) / Path(str(hit["rel"])).name
        if not src.exists():
            return {"ok": False, "reason": "回收站中的文件已丢失"}
        dest = config.DOWNLOADS_DIR / str(hit["rel"])
        if dest.exists():
            return {"ok": False, "reason": "原路径已存在同名文件，请先处理后再恢复"}
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as e:
            return {"ok": False, "reason": f"恢复失败: {e}"}
        _save_trash([i for i in items if i.get("id") != item_id])
    # 恢复后 trash/<id> 目录可能已空，清理空壳
    try:
        shutil.rmtree(config.TRASH_DIR / str(hit["id"]))
    except OSError:
        pass
    invalidate_cache()
    return {"ok": True, "rel": str(hit["rel"])}


def purge_trash(item_id: str = "") -> dict[str, Any]:
    """永久删除：指定 id 时删该项，id 为空时清空回收站全部条目"""
    with _trash_lock:
        items = _load_trash()
        if item_id:
            targets = [i for i in items if i.get("id") == item_id]
            if not targets:
                return {"ok": False, "reason": "回收站中不存在该项"}
            remain = [i for i in items if i.get("id") != item_id]
        else:
            targets = list(items)
            remain = []
        count = 0
        for it in targets:
            d = config.TRASH_DIR / str(it.get("id") or "")
            if d.is_dir():
                try:
                    shutil.rmtree(d)
                except OSError:
                    continue
            count += 1
        _save_trash(remain)
    invalidate_cache()
    return {"ok": True, "count": count}
