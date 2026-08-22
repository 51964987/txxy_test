"""资源管理：扫描 downloads/ 目录（只读），按文件夹分组返回文件清单。"""
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


def scan() -> dict:
    root = config.DOWNLOADS_DIR
    if not root.is_dir():
        return {"count": 0, "total_files": 0, "total_size": 0, "items": []}

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

    return {
        "count": len(items),
        "total_files": total_files,
        "total_size": total_size,
        "items": items,
    }
