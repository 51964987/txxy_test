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
from pathlib import Path
from typing import Any

# 小于该字节数的文件视为「空对象」，不值得备份
_BACKUP_MIN_SIZE = 2


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
    if backup and path.exists() and path.stat().st_size > _BACKUP_MIN_SIZE:
        try:
            shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
        except OSError:
            pass  # 备份失败不阻塞主写入，下一轮会再尝试
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
