"""前端展示服务配置（只读访问现有数据，不改库）。

路径与展示域名均可通过环境变量覆盖：
  POSTS_DB        数据库文件路径（默认 db/posts.db）
  OUTPUTS_DIR     outputs 目录（默认 outputs/）
  DOWNLOADS_DIR   downloads 目录（默认 downloads/）
  PUBLIC_ROOT     展示层 URL 归一化用的公开域名（默认 https://txxy.com）
  TXXY_WEB_HOST   监听地址（默认 127.0.0.1，局域网访问设 0.0.0.0）
  TXXY_WEB_PORT   监听端口（默认 8080）
  TXXY_ENABLE_AUTO_REFRESH  是否启用数据总览自动刷新（默认 0/关闭，设为 1 开启）
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

# 数据总览【自动刷新】总开关：默认开启（Header 显示自动刷新开关并启动轮询，
# 抓取过程中 KPI 准实时更新）。如需关闭可设环境变量 TXXY_ENABLE_AUTO_REFRESH=0。
# 前端 /api/config 读取该值，为 False 时不显示自动刷新开关、不启动轮询。
ENABLE_AUTO_REFRESH = os.environ.get("TXXY_ENABLE_AUTO_REFRESH", "1").strip().lower() in ("1", "true", "yes", "on")

# 版块名称映射（与 run_batch.SECTIONS 保持一致；未知 fid 显示为 版块<n>）
FID_NAMES: dict[str, str] = {
    "2": "版块2",
    "4": "版块4",
    "5": "版块5",
    "7": "版块7",
    "8": "版块8",
    "15": "版块15",
    "16": "版块16",
    "20": "版块20",
    "21": "版块21",
    "22": "版块22",
    "25": "版块25",
    "26": "版块26",
    "28": "版块28",
}


def fid_name(fid: str) -> str:
    return FID_NAMES.get(fid, f"版块{fid}")
