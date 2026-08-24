"""
统一日志输出模块：把程序的所有 print 输出同时写入控制台和日志文件。

日志文件位于 outputs/<日期>/ 目录（与 scraper.py 的 OUTPUT_DIR 结构一致），
文件名形如 <程序名>_<日期>.log，UTF-8 编码、追加模式、每行立即落盘。

日志保留策略：cleanup_old_logs() 删除 outputs/ 下超过 RETENTION_DAYS
（默认 3 天）天的过期日期目录——连同目录内的 *.log、CSV、progress 等
文件整体删除（超出保留期的历史数据不再保留），避免日志与数据无限累积。
清理时机由调用方控制：建议在批次任务成功完成后调用（如 run_batch 在全部
版块抓取正常结束后执行），异常退出（中断/崩溃）时不清理，保留现场便于排查。

时间戳与服务标签：非空行自动添加 "[YYYY-MM-DD HH:MM:SS] [<服务名>]" 前缀
（日志文件与终端控制台均生效，<服务名> 由 setup("<程序名>") 指定，
如 run_batch / scraper_2 / download_files，便于区分每条日志所属的 Python 服务）。
执行汇总等无需时间戳的输出，用 raw() 上下文管理器包裹：
    with file_logger.raw():
        print("执行汇总: ...")
非终端管道（子进程输出转发，如 run_batch 的 __SUMMARY__ 解析）保持原样，
不添加时间戳与标签，保证机器可读输出不被破坏。

用法（在 import 后、任何打印之前调用一次即可）：
    import file_logger
    file_logger.setup("scraper_2")   # 程序名自定义，用于区分日志文件

原理：将 sys.stdout / sys.stderr 替换为 Tee 包装，原样转发到控制台，
同时写入日志文件。因此脚本内所有 print（含 file=sys.stderr、traceback
异常栈）都会自动落盘，无需改动任何打印语句。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TextIO

# 日志根目录（与各脚本输出目录保持一致）
LOG_ROOT = "outputs"

# 日志保留天数：cleanup_old_logs() 删除超过该天数的旧日志文件（默认保留最近 3 天）
RETENTION_DAYS = 3

# 日期目录名（YYYYMMDD）匹配，用于定位 outputs/ 下的按日目录
_DIR_RE = re.compile(r"^\d{8}$")


def log_dir() -> str:
    """返回当天日志目录 outputs/YYYYMMDD/"""
    return os.path.join(LOG_ROOT, datetime.now().strftime("%Y%m%d"))


class _Tee:
    """同时写入原始流（控制台/管道）与日志文件的流包装"""

    _stream: TextIO
    _fh: TextIO | None

    def __init__(self, stream: TextIO, fh: TextIO) -> None:
        self._stream = stream
        self._fh = fh

    def write(self, data: str) -> int:
        # 逐行添加时间戳；汇总(raw)模式或空行不加
        timestamped = _add_timestamps(data)
        # 日志文件（UTF-8）优先写入，确保打印信息不丢失；
        # 若日志写入失败（磁盘满/目录只读等），降级为仅控制台输出，不中断业务
        if self._fh is not None:
            try:
                _ = self._fh.write(timestamped)
                self._fh.flush()
            except Exception:
                try:
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
                try:
                    _ = self._stream.write(
                        "[警告] 日志文件写入失败，后续仅输出到控制台\n"
                    )
                    self._stream.flush()
                except Exception:
                    pass
        # 控制台：终端（tty）按时间戳输出；非终端（管道/重定向，如
        # run_batch 的子进程 __SUMMARY__ 转发）保持原样，避免破坏机器解析
        console_data = timestamped if self._is_tty() else data
        try:
            _ = self._stream.write(console_data)
            self._stream.flush()
        except UnicodeEncodeError:
            enc = getattr(self._stream, "encoding", None) or "utf-8"
            _ = self._stream.write(console_data.encode(enc, "replace").decode(enc))
            self._stream.flush()
        return len(data)

    def _is_tty(self) -> bool:
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass
        if self._fh is not None:
            try:
                self._fh.flush()
            except Exception:
                pass

    def fileno(self) -> int:
        return self._stream.fileno()

    def isatty(self) -> bool:
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass

    def __enter__(self) -> "_Tee":
        return self

    def __exit__(self, *args: object) -> None:
        self.flush()

    def __getattr__(self, name: str) -> Any:  # pyright: ignore[reportAny, reportExplicitAny]
        # 其他属性（encoding 等）动态转发给原始流
        return getattr(self._stream, name)  # pyright: ignore[reportAny]


# 汇总模式开关：True 时输出不加时间戳（执行汇总等无需时间的信息）
_raw_mode = False

# 当前服务名标签（setup() 时设置；未启用前为空，日志行不带 [服务名] 前缀）
_program = ""

# 本次运行（批次）起始时间戳：格式 YYYYMMDD_HHMMSS，用于日志/数据文件名
# 贯穿整个进程，保证同一次运行中所有文件共享同一批次时间（而非各自取实时时间）
_run_batch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")


def _add_timestamps(data: str) -> str:
    """逐行添加时间戳与服务标签前缀 [YYYY-MM-DD HH:MM:SS] [<服务名>]；
    汇总模式或空行保持原样"""
    if not data or _raw_mode:
        return data
    tag = f"[{_program}] " if _program else ""
    ts = datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
    lines = data.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        out.append(ts + tag + line if line.strip() else line)
    return "".join(out)


@contextmanager
def raw() -> Generator[None, None, None]:
    """上下文管理器：块内的输出不加时间戳（用于执行汇总等输出）"""
    global _raw_mode
    _raw_mode = True
    try:
        yield
    finally:
        _raw_mode = False


_installed: set[str] = set()


def cleanup_old_logs(retention_days: int = RETENTION_DAYS) -> int:
    """清理 outputs/<日期>/ 下超过 retention_days 天的过期日期目录。

    目录名必须为 YYYYMMDD 格式（避免误删非日期目录）；过期目录连同其中
    的 *.log、CSV/progress 等数据文件整体删除（超出保留期的历史数据不再
    保留）。每次删除操作都会输出日志留痕（记录于当前进程的日志文件）。
    返回删除的日志文件数。目录整体删除失败（如文件被占用）时回退为仅删除
    其中的日志文件，数据文件留待下次启动再清，不影响当前程序运行。
    """
    if retention_days < 0:
        return 0
    removed = 0
    today = datetime.now().date()
    try:
        entries = os.listdir(LOG_ROOT)
    except OSError:
        return 0
    for name in entries:
        if not _DIR_RE.fullmatch(name):
            continue
        try:
            dir_date = datetime.strptime(name, "%Y%m%d").date()
        except ValueError:
            continue
        # 保留"最近 N 天"：目录距今 ≤ retention_days 天都保留（含正好 N 天前），
        # 只删除严格超过 N 天的，避免 off-by-one 误删最近一天的历史日志
        if (today - dir_date).days <= retention_days:
            continue
        # 保护：跳过当天及未来日期目录，避免 retention_days=0 或系统时钟
        # 异常时误删当前正在写入的日志
        if dir_date >= today:
            continue
        dir_path = os.path.join(LOG_ROOT, name)
        try:
            files = os.listdir(dir_path)
        except OSError:
            continue
        # 统计日志数（用于返回值），随后整个目录一并删除（含 CSV/progress 等数据）
        log_count = sum(1 for fn in files if fn.lower().endswith(".log"))
        try:
            shutil.rmtree(dir_path)
            removed += log_count
            print(
                "[日志清理] 已删除过期目录: " + dir_path
                + f"（共 {len(files)} 个文件，其中日志 {log_count} 个）"
            )
        except OSError:
            # 目录删除失败（如文件被占用）：回退为仅删除日志文件，
            # 数据文件保留，留待下次启动再清
            deleted_in_dir = 0
            for fn in files:
                if fn.lower().endswith(".log"):
                    file_path = os.path.join(dir_path, fn)
                    try:
                        os.remove(file_path)
                        removed += 1
                        deleted_in_dir += 1
                        print(f"[日志清理] 已删除过期日志文件: {file_path}")
                    except OSError:
                        pass
            if deleted_in_dir:
                print(
                    f"[日志清理] 目录 {dir_path} 整体删除失败（可能被占用），"
                    + f"已删除其中 {deleted_in_dir} 个日志文件"
                )
    return removed


def _log_path(program: str) -> str:
    return os.path.join(log_dir(), f"{program}_{_run_batch_ts}.log")


def setup(program: str) -> str:
    """启用日志输出（每个进程调用一次即可），返回日志文件路径。

    仅负责启用日志，不清理过期日志：清理由调用方在批次任务成功完成后
    显式调用 cleanup_old_logs()（异常退出时保留日志现场，便于排查）。
    """
    global _program
    if program in _installed:
        return _log_path(program)
    _installed.add(program)
    _program = program

    path = _log_path(program)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a", encoding="utf-8", errors="replace")

    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)

    # 会话开始行已含完整日期时间，raw 包裹避免重复添加 [HH:MM:SS] 前缀
    with raw():
        _ = sys.stdout.write(
            f"\n===== 会话开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n"
        )
    return path
