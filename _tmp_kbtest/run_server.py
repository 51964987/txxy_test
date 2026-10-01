"""隔离实例启动器（E2E 专用，验证后删除）：全部持久化路径指向 _tmp_kbtest/。

规则依据（原 47）：写操作实测必须用隔离实例（独立端口 + 临时持久化文件）；
路径类变量 POSTS_DB / OUTPUTS_DIR 无 TXXY_ 前缀。.env 不覆盖已注入环境变量，
此处显式 update 即为最高优先级。
"""
import os
import sys

ROOT = r"d:\biancheng\otherProject\txxy_test"
TMP = os.path.join(ROOT, "_tmp_kbtest")

os.environ.update({
    "POSTS_DB": os.path.join(TMP, "db", "posts.db"),
    "OUTPUTS_DIR": os.path.join(TMP, "outputs"),
    "TXXY_WEB_HOST": "127.0.0.1",
    "TXXY_WEB_PORT": "8100",
    "TXXY_VAULT_ROOT": os.path.join(TMP, "vault"),
    "TXXY_KB_STATE_ROOT": os.path.join(TMP, "kb_state"),
    "TXXY_WEB_SETTINGS_FILE": os.path.join(TMP, "outputs", "web_settings.json"),
    "TXXY_KB_SCHEDULE_STATE_FILE": os.path.join(TMP, "outputs", "kb_schedule_state.json"),
    "TXXY_SCRAPE_SCHEDULE_STATE_FILE": os.path.join(TMP, "outputs", "scrape_schedule_state.json"),
    "TXXY_PRECIPITATE_SCHEDULE_STATE_FILE": os.path.join(TMP, "outputs", "precipitate_schedule_state.json"),
    "TXXY_DOWNLOAD_TASKS_FILE": os.path.join(TMP, "outputs", "download_tasks.json"),
    "TXXY_DOWNLOAD_HISTORY_FILE": os.path.join(TMP, "outputs", "download_history.json"),
    "TXXY_TRASH_DIR": os.path.join(TMP, "outputs", "trash"),
    "TXXY_SCRAPE_SCHEDULE_ENABLED": "false",   # 隔离实例不跑抓取调度
    "TXXY_KB_EXPORT_TIMES": "03:00",           # 计划时刻远离当前时间，定时入口不误触发
    "TXXY_SCRAPE_MAX_RETRIES": "1",            # 帖子页抓取最多 1 次尝试，限定批次时长
})

sys.path.insert(0, os.path.join(ROOT, "web"))
sys.path.insert(0, ROOT)
os.chdir(os.path.join(ROOT, "web"))

import app  # noqa: E402

app.main()
