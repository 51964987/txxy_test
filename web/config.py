"""前端展示服务配置（只读访问现有数据，不改库）。

路径与展示域名均可通过环境变量覆盖：
  POSTS_DB        数据库文件路径（默认 db/posts.db）
  OUTPUTS_DIR     outputs 目录（默认 outputs/）
  DOWNLOADS_DIR   downloads 目录（默认 downloads/）
  PUBLIC_ROOT     展示层 URL 归一化用的公开域名（默认 https://txxy.com）
  TXXY_WEB_HOST   监听地址（默认 127.0.0.1，局域网访问设 0.0.0.0）
  TXXY_WEB_PORT   监听端口（默认 8080）
  TXXY_ENABLE_AUTO_REFRESH  是否启用数据总览自动刷新（默认 0/关闭，设为 1 开启）
  以下为下载中心（URL 批量下载）配置，均可通过环境变量覆盖：
  TXXY_DOWNLOAD_CONCURRENCY  单任务内并行下载的 URL 数（默认 2）
  TXXY_DOWNLOAD_MAX_BATCH    单次批量提交的 URL 数量上限（默认 50）
  TXXY_DOWNLOAD_TASKS_FILE   下载任务历史持久化文件（默认 outputs/download_tasks.json）
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # txxy_test/

DB_FILE = Path(os.environ.get("POSTS_DB", str(BASE_DIR / "db" / "posts.db")))
OUTPUTS_DIR = Path(os.environ.get("OUTPUTS_DIR", str(BASE_DIR / "outputs")))
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", str(BASE_DIR / "downloads")))

# 公开域名：展示层 URL 归一化用，与 run_batch.REMOTE_ROOT_URL 保持一致
PUBLIC_ROOT = os.environ.get("PUBLIC_ROOT", "http://127.0.0.1:1024").rstrip("/")
# 旧数据中可能出现的本地代理前缀（--public 修复前入库）
LOCAL_PROXY_PREFIX = "http://127.0.0.1:1024"

HOST = os.environ.get("TXXY_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("TXXY_WEB_PORT", "8088"))

# ---------------- 下载中心（URL 批量下载） ----------------
# 单任务内并行下载的 URL 数：并发过高易触发源站限流/封禁，默认 2 保守取值。
DOWNLOAD_CONCURRENCY = int(os.environ.get("TXXY_DOWNLOAD_CONCURRENCY", "2"))
# 任务间并行数（全局 worker 线程数）：默认 1 = 任务严格串行（最保守，防源站反爬）；
# 调大后多个任务可同时执行，每个任务内部仍按 DOWNLOAD_CONCURRENCY 并行下载。
DOWNLOAD_TASK_CONCURRENCY = int(os.environ.get("TXXY_DOWNLOAD_TASK_CONCURRENCY", "1"))
# 任务历史保留条数上限：持久化时超出部分按创建时间从旧到新裁剪（仅删终态任务），
# 防止 download_tasks.json 无限膨胀。
DOWNLOAD_TASK_MAX_KEEP = int(os.environ.get("TXXY_DOWNLOAD_TASK_MAX_KEEP", "200"))
# 单次批量提交的 URL 数量上限：防止误操作一次性提交过量下载请求。
DOWNLOAD_MAX_BATCH = int(os.environ.get("TXXY_DOWNLOAD_MAX_BATCH", "50"))
# 下载任务历史持久化文件：服务重启后任务列表/状态不丢失。
DOWNLOAD_TASKS_FILE = Path(
    os.environ.get("TXXY_DOWNLOAD_TASKS_FILE", str(BASE_DIR / "outputs" / "download_tasks.json"))
)

# ---------------- 资源管理：回收站（软删除） ----------------
# 删除的资源先移入回收站，保留期到期后可彻底清理；永久删除入口在回收站内提供
TRASH_DIR = Path(os.environ.get("TXXY_TRASH_DIR", str(BASE_DIR / "outputs" / "trash")))
TRASH_KEEP_DAYS = int(os.environ.get("TXXY_TRASH_KEEP_DAYS", "7"))

# 数据总览【自动刷新】总开关：默认开启（Header 显示自动刷新开关并启动轮询，
# 抓取过程中 KPI 准实时更新）。如需关闭可设环境变量 TXXY_ENABLE_AUTO_REFRESH=0。
# 前端 /api/config 读取该值，为 False 时不显示自动刷新开关、不启动轮询。
ENABLE_AUTO_REFRESH = os.environ.get("TXXY_ENABLE_AUTO_REFRESH", "1").strip().lower() in ("1", "true", "yes", "on")

# 版块名称映射（与 run_batch.SECTIONS 保持一致；未知 fid 显示为 版块<n>）
FID_NAMES: dict[str, str] = {
    "2": "亞洲無MA原創區",
    "4": "歐美原創區",
    "5": "動漫原創區",
    "7": "技術討論區",
    "8": "新時代的我們",
    "15": "亞洲有MA原創區",
    "16": "達蓋爾的旗幟",
    "20": "CR文學交流區",
    "21": "HTTP下載區",
    "22": "在綫CR影院",
    "25": "國產原創區",
    "26": "中字原創區",
    "28": "AI破解原創區",
}


def fid_name(fid: str) -> str:
    return FID_NAMES.get(fid, f"版块{fid}")
