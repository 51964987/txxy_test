"""原子写文件（web 端共用，全项目唯一的「临时文件 + 原子替换」实现）。

历史问题：api.py（榜单 NEW 快照）、resources.py（回收站清单）、download_tasks.py
（下载任务持久化）各写了一份「先写临时文件再 os.replace」的 JSON 落盘，
仅缩进与是否轮转 .bak 不同。将来要加统一行为（如统一备份、统一编码、写后校验）
就得改三处，漏一处就会出现「有的文件有备份、有的没有」这种不一致。

差异通过参数表达；失败时向上抛 OSError——是否吞掉由调用方按业务决定
（清单类可忽略并重试，任务历史类捕获后继续运行）。
"""
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

# 小于该字节数的文件视为「空对象」，不值得备份
_BACKUP_MIN_SIZE = 2

# os.replace 撞上目标被 Obsidian / 编辑器瞬时占用的短重试参数（修订 25 拍板：3 次 × 0.5s）。
# 短重试只针对 PermissionError（Windows 文件占用），其余异常照旧直接向上抛。
_REPLACE_RETRIES = 3
_REPLACE_RETRY_DELAY = 0.5


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """os.replace 的占用短重试包装（唯一实现，JSON / 文本 / 二进制三个入口共用）。

    Windows 下 Obsidian 等编辑器可能短暂持有目标文件句柄，首次 replace 抛
    PermissionError 属瞬时冲突——按 3 次 × 0.5s 重试后仍失败才向上抛，
    调用方计数进批次汇总（kb_export 的写盘护栏语义）。
    """
    for attempt in range(1, _REPLACE_RETRIES + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES:
                raise
            time.sleep(_REPLACE_RETRY_DELAY)


def _backup_existing(path: Path, backup: bool) -> None:
    """写盘前把现有非空文件轮转为 <原名>.bak（backup=True 时）。"""
    if backup and path.exists() and path.stat().st_size > _BACKUP_MIN_SIZE:
        try:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass  # 备份失败不阻塞主写入，下一轮会再尝试


def write_json_atomic(
    path: Path,
    obj: Any,
    *,
    indent: int | None = None,
    backup: bool = False,
) -> None:
    """原子写入 JSON：先写同目录 .tmp 再 os.replace，避免写到一半损坏原文件。

    - indent：缩进层级（None 为紧凑单行，2 便于人工查看）
    - backup：写盘前把现有非空文件轮转为 <原名>.bak，保留上一代
      （曾发生：服务异常重启时持久化文件被写空，导致任务历史丢失）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_existing(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    _replace_with_retry(tmp, path)


def write_text_atomic(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    backup: bool = False,
) -> None:
    """原子写入文本文件（修订 25 泛化入口）：kb_export 落 `.md` 笔记等纯文本用。

    与 write_json_atomic 同一套「tmp + replace」骨架，禁止另写第二份 tmp+replace；
    占用冲突走 _replace_with_retry 短重试。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_existing(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
    _replace_with_retry(tmp, path)


def write_bytes_atomic(path: Path, data: bytes, *, backup: bool = False) -> None:
    """原子写入二进制文件（修订 26）：kb 原始件 `<标题>.html` 按**字节**存盘专用。

    禁止把非 UTF-8 页面先解码成文本再落盘——那样落盘即转码，
    「重转时按记录编码解码」将对已转码文件用错编码。占用冲突走短重试。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _backup_existing(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    _replace_with_retry(tmp, path)
