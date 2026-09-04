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
import hashlib
import json
import math
import os
import shutil
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from atomicfile import write_json_atomic
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


# ---- 受控路径解析（限定 downloads/ 内） ----
# 越界校验此前在 resolve_safe / open_folder / resolve_safe_dir 里各写一遍，
# 同一份防护抄三份——将来补边界条件（软链接、大小写等）漏改一处就是路径穿越漏洞。
# 收敛为 _resolve_within，只做「限定在 downloads/ 内」这一件事；
# 各公开函数再叠加自己的文件/目录与根自身判定。
def _resolve_within(rel: str) -> Path | None:
    """把相对路径解析为 downloads/ 内的绝对路径；越界 / 非法 / 解析失败返回 None。

    防路径穿越：resolve 后要求目标位于 downloads/ 之内（root 自身或其后代）。
    拒绝即安全，不做任何降级。
    """
    rel = (rel or "").strip().replace("\\", "/").strip("/")
    if not rel or rel.lower().startswith(("http://", "https://")):
        return None
    root = config.DOWNLOADS_DIR.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    return target


def _startfile(target: Path, action: str) -> None:
    """调起系统默认程序打开本地路径（仅 Windows 的 os.startfile 可用）。

    非 Windows 抛 OSError，由接口层统一转 501。
    """
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError(f"当前系统不支持{action}（仅 Windows 支持）")
    startfile(str(target))


# ---- B5 图片预览：受控路径解析（限定 downloads/ 内 + 图片扩展名白名单） ----
def resolve_safe(rel: str) -> Path | None:
    """文件版受控路径解析：限定 downloads/ 内且为已存在文件。"""
    target = _resolve_within(rel)
    return target if target is not None and target.is_file() else None


# ---- B8 打开所在目录：调起系统文件管理器（仅 Windows 生效） ----
def open_folder(rel: str) -> bool:
    """用系统文件管理器打开 downloads/ 下的目录（rel 为空时打开 downloads/ 根目录）。

    路径同样限定在 downloads/ 内；非 Windows 平台无 os.startfile，抛 OSError 由接口层转 501。
    """
    if (rel or "").strip():
        target = _resolve_within(rel)
        if target is None:
            return False
    else:
        target = config.DOWNLOADS_DIR.resolve()
    if not target.is_dir():
        return False
    _startfile(target, "打开本地目录")
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
    target = _resolve_within(rel)
    if target is None or not target.is_dir():
        return None
    # 与 open_folder 不同：这里不允许 downloads/ 根自身
    # （「打开 downloads 根目录」是 open_folder 的专属入口）
    return None if target == config.DOWNLOADS_DIR.resolve() else target


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
    write_json_atomic(_TRASH_INDEX, {"items": items}, indent=2)


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


# ================= 类型兼容操作（2026-09-01） =================
# 此前只有图片（预览）与视频（播放）有操作入口，文本 / 种子 / 其他类型只能复制路径或删除。
# 这里补齐受控的「查看文本 / 解析种子 / 用系统默认程序打开」三类能力，
# 路径校验统一复用 resolve_safe()（限定 downloads/ 内）。

# 文本查看上限：超过则只返回前 N 字节并标记截断，避免大日志拖垮接口
TEXT_VIEW_LIMIT = 512 * 1024
# 种子文件清单最多返回条数（防超大种子把响应撑爆）
TORRENT_FILES_LIMIT = 200


def read_text(rel: str) -> dict[str, Any] | None:
    """受控读取文本文件（编码兜底 + 大小限制）。

    UTF-8 优先，GB18030 兜底——中文 Windows 生成的 txt 多为 GBK 系编码，
    若只按 UTF-8 解码会直接失败或出乱码。返回 { text, encoding, size, truncated }。
    """
    target = resolve_safe(rel)
    if target is None or target.suffix.lower() not in TEXT_EXTS:
        return None
    try:
        size = target.stat().st_size
        with open(target, "rb") as f:
            raw = f.read(TEXT_VIEW_LIMIT)
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return {
                "text": raw.decode(enc),
                "encoding": enc,
                "size": size,
                "truncated": size > TEXT_VIEW_LIMIT,
            }
        except UnicodeDecodeError:
            continue
    return None


def _bdecode(data: bytes, i: int = 0) -> tuple[Any, int]:
    """极简 bencode 解码（int / bytes / list / dict），仅够解析 .torrent 元信息。

    项目约束不引第三方依赖，故自建最小实现；解析失败由调用方捕获后降级为 None。
    """
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":
        i += 1
        out: list[Any] = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out_d: dict[Any, Any] = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out_d[k] = v
        return out_d, i + 1
    j = data.index(b":", i)
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def _bencode(obj: Any) -> bytes:
    """bencode 编码（int / bytes / str / list / dict），用于重编 info 字典计算 infohash。

    注意：bencode 规范要求字典键按字节序排列，否则算出的 infohash 与客户端不一致。
    """
    if isinstance(obj, bool):
        raise TypeError("bencode 不支持布尔值")
    if isinstance(obj, int):
        return b"i" + str(obj).encode() + b"e"
    if isinstance(obj, (bytes, bytearray)):
        raw_b = bytes(obj)
        return str(len(raw_b)).encode() + b":" + raw_b
    if isinstance(obj, str):
        raw_s = obj.encode("utf-8")
        return str(len(raw_s)).encode() + b":" + raw_s
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        items = sorted(
            obj.items(),
            key=lambda kv: kv[0] if isinstance(kv[0], bytes) else str(kv[0]).encode(),
        )
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(f"不支持的 bencode 类型: {type(obj)}")


def _bstr(v: Any) -> str:
    """bencode 字符串 → str（UTF-8 优先，GB18030 兜底，最后 latin-1 兜底）"""
    if isinstance(v, bytes):
        for enc in ("utf-8", "gb18030", "latin-1"):
            try:
                return v.decode(enc)
            except UnicodeDecodeError:
                continue
        return ""
    return str(v or "")


def parse_torrent(rel: str) -> dict[str, Any] | None:
    """解析 .torrent：返回名称、infohash、磁链与文件清单。

    infohash = sha1(bencode(info))，必须重编 info 字典（不能直接用原始字节切片之外的
    任何改动），否则磁链与其他客户端算出的不一致，等于无效的磁链。
    """
    target = resolve_safe(rel)
    if target is None or target.suffix.lower() not in TORRENT_EXTS:
        return None
    try:
        with open(target, "rb") as f:
            meta, _ = _bdecode(f.read())
    except (OSError, ValueError, IndexError):
        return None
    if not isinstance(meta, dict):
        return None
    info = meta.get(b"info")
    if not isinstance(info, dict):
        return None

    try:
        infohash = hashlib.sha1(_bencode(info)).hexdigest()
    except TypeError:
        return None

    name = _bstr(info.get(b"name")) or target.stem
    files: list[dict[str, Any]] = []
    flist = info.get(b"files")
    if isinstance(flist, list):
        for f in flist:
            if not isinstance(f, dict):
                continue
            parts = f.get(b"path") or []
            if isinstance(parts, list):
                p = "/".join(_bstr(x) for x in parts)
            else:
                p = _bstr(parts)
            try:
                size = int(f.get(b"length") or 0)
            except (TypeError, ValueError):
                size = 0
            files.append({"path": p, "size": size})
    else:
        try:
            size = int(info.get(b"length") or 0)
        except (TypeError, ValueError):
            size = 0
        files.append({"path": name, "size": size})

    dn = urllib.parse.quote(name)
    return {
        "name": name,
        "infohash": infohash,
        "magnet": f"magnet:?xt=urn:btih:{infohash}&dn={dn}",
        "total_size": sum(f["size"] for f in files),
        "file_count": len(files),
        "files": files[:TORRENT_FILES_LIMIT],
        "files_truncated": len(files) > TORRENT_FILES_LIMIT,
    }


def open_file(rel: str) -> bool:
    """用系统默认程序打开 downloads/ 下的文件（仅 Windows 的 os.startfile 可用）。

    与 open_folder 一样属「执行类」操作，路径校验复用 resolve_safe；
    非 Windows 抛 OSError，由接口层转 501。
    """
    target = resolve_safe(rel)
    if target is None:
        return False
    _startfile(target, "打开本地文件")
    return True
