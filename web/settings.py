"""页内参数设置：白名单定义、范围钳制、原子落盘与来源标注。

定位：
- 只覆盖「允许页内调整」的运行时参数（并发 / 节流 / 保留期 / 缓存 TTL / 自动刷新），
  端口、路径、域名等部署配置一律不进白名单，仍走环境变量（业界同：属部署配置）；
- 默认值**不在这里重复定义**——各参数默认值仍在其归属模块（config.py / download_files.py /
  resources.py），本模块只存「用户覆盖值」，缺失即回落；
- 落盘复用 atomicfile（临时文件 + 原子替换），损坏时回落默认值。

生效范围口径（与前端设置页展示一致）：
- immediate：下一次调用即生效；
- next_task：下一个下载任务生效（正在跑的任务不受影响，与 aria2 变更选项同口径）；
- frontend：保存后前端直接应用。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any

from atomicfile import write_json_atomic
import config
import download_files  # 项目根模块：默认下载间隔/重试次数在此，避免默认值两份

WHITELIST: dict[str, dict[str, Any]] = {
    "download_concurrency": {
        "label": "单任务内并行下载数",
        "min": 1,
        "max": 8,
        "scope": "next_task",
        "desc": "一个任务里同时下载的链接数；过高易触发源站限流/封禁",
    },
    "download_task_concurrency": {
        "label": "任务间并行数",
        "min": 1,
        "max": 4,
        "scope": "immediate",
        "desc": "全局 worker 线程数；1=任务串行。调小后多余线程在当前任务结束后退出",
    },
    "download_max_batch": {
        "label": "单次提交链接上限",
        "min": 10,
        "max": 200,
        "scope": "immediate",
        "desc": "防止误操作一次性提交过量下载请求",
    },
    "download_task_max_keep": {
        "label": "任务历史保留条数",
        "min": 50,
        "max": 1000,
        "scope": "immediate",
        "desc": "超出后按创建时间从旧到新裁剪（仅删终态任务）",
    },
    "download_interval": {
        "label": "单文件下载间隔（秒）",
        "min": 0,
        "max": 2,
        "scope": "next_task",
        "desc": "同一任务内逐个文件下载之间的等待，避免请求过快",
    },
    "download_max_retries": {
        "label": "请求重试次数",
        "min": 1,
        "max": 5,
        "scope": "next_task",
        "desc": "临时性错误（网络异常 / 5xx / 429）的总尝试次数",
    },
    "download_retry_delay": {
        "label": "重试等待（秒）",
        "min": 1,
        "max": 10,
        "scope": "next_task",
        "desc": "首次重试等待，后续按次数递增",
    },
    "trash_keep_days": {
        "label": "回收站保留天数",
        "min": 1,
        "max": 30,
        "scope": "immediate",
        "desc": "删除的资源在回收站内保留的天数，到期可回收",
    },
    "resources_scan_ttl": {
        "label": "资源扫描缓存 TTL（秒）",
        "min": 2,
        "max": 60,
        "scope": "immediate",
        "desc": "签名未变时距上次扫描超过该值即重扫；越小越实时、越大越省 IO",
    },
    "enable_auto_refresh": {
        "label": "数据总览自动刷新",
        "type": "bool",
        "scope": "frontend",
        "desc": "关闭后数据总览不显示自动刷新开关、不启动轮询",
    },
    "carousel_interval": {
        "label": "演示轮播停留时长（秒）",
        "min": 3,
        "max": 60,
        "scope": "frontend",
        "desc": "投屏演示模式下每个板块停留在屏幕上的秒数；超出范围自动收敛",
    },
    "carousel_sections": {
        "label": "演示轮播板块序列",
        "type": "array",
        "scope": "frontend",
        "options": [
            {"value": "overview", "label": "总览首屏（KPI 指标）"},
            {"value": "trend", "label": "每日发布趋势 + 分版块趋势"},
            {"value": "ranks", "label": "活跃作者 / 活跃 fid 榜"},
            {"value": "boards", "label": "热门榜（点赞/回复/最新最热/本月最热）"},
        ],
        "desc": "演示轮播依次切换的板块与顺序：勾选即纳入、上下移动调整顺序",
    },
}

SETTINGS_FILE = Path(
    os.environ.get("TXXY_WEB_SETTINGS_FILE", str(config.BASE_DIR / "outputs" / "web_settings.json"))
)

_lock = threading.Lock()
_values: dict[str, Any] | None = None

# 演示轮播默认板块序列（与前端 SECTION_IDS 的键对齐）；顺序即轮播切换顺序
CAROUSEL_SECTIONS_DEFAULT: list[str] = ["overview", "trend", "ranks", "boards"]


def _load() -> dict[str, Any]:
    """读取设置文件；缺失/损坏返回空字典（回落默认值）"""
    global _values
    if _values is not None:
        return _values
    data: dict[str, Any] = {}
    try:
        if SETTINGS_FILE.is_file():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("items"), dict):
                data = {k: v for k, v in raw["items"].items() if k in WHITELIST}
    except (OSError, ValueError):
        data = {}
    _values = data
    return _values


def _env_or_default(key: str) -> Any:
    """白名单外的键一律回落：环境/默认取值（各参数归属模块的既有常量）"""
    if key == "download_concurrency":
        return config.DOWNLOAD_CONCURRENCY
    if key == "download_task_concurrency":
        return config.DOWNLOAD_TASK_CONCURRENCY
    if key == "download_max_batch":
        return config.DOWNLOAD_MAX_BATCH
    if key == "download_task_max_keep":
        return config.DOWNLOAD_TASK_MAX_KEEP
    if key == "trash_keep_days":
        return config.TRASH_KEEP_DAYS
    if key == "enable_auto_refresh":
        return config.ENABLE_AUTO_REFRESH
    # 以下默认值定义在项目根 download_files.py（CLI 与 Web 同源）
    if key == "download_interval":
        return download_files.DOWNLOAD_INTERVAL
    if key == "download_max_retries":
        return download_files.MAX_RETRIES
    if key == "download_retry_delay":
        return download_files.RETRY_DELAY
    if key == "carousel_interval":
        return 8
    if key == "carousel_sections":
        return CAROUSEL_SECTIONS_DEFAULT
    # resources_scan_ttl 的默认值由调用方传入（resources._CACHE_TTL）
    return None


def get(key: str, fallback: Any = None) -> Any:
    """取生效值：设置文件 > 环境/默认值（fallback 用于默认值不在映射表内的参数）"""
    if key not in WHITELIST:
        return fallback
    with _lock:
        data = _load()
        if key in data:
            return data[key]
    d = _env_or_default(key)
    return fallback if d is None else d


def get_int(key: str, fallback: int) -> int:
    try:
        return int(get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def get_float(key: str, fallback: float) -> float:
    try:
        return float(get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def get_bool(key: str, fallback: bool) -> bool:
    v = get(key, fallback)
    return bool(v)


def _clamp(key: str, value: Any) -> Any:
    spec = WHITELIST[key]
    t = spec.get("type", "int")
    if t == "bool":
        return bool(value)
    if t == "array":
        allowed = {o["value"] for o in spec.get("options", [])}
        if not isinstance(value, (list, tuple)):
            value = []
        seen: list[str] = []
        for v in value:
            if v in allowed and v not in seen:
                seen.append(v)
        return seen
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"「{spec['label']}」必须是数字")
    num = max(float(spec["min"]), min(float(spec["max"]), num))
    return int(num) if t == "int" else num


def snapshot() -> list[dict[str, Any]]:
    """全部白名单参数的当前状态（供 /api/config 与设置页回显）"""
    out = []
    for key, spec in WHITELIST.items():
        default = _env_or_default(key)
        with _lock:
            overridden = key in _load()
        value = get(key, default)
        out.append(
            {
                "key": key,
                "label": spec["label"],
                "desc": spec["desc"],
                "scope": spec["scope"],
                "type": spec.get("type", "int"),
                "min": spec.get("min"),
                "max": spec.get("max"),
                "options": spec.get("options"),
                "value": value,
                "default": default,
                "source": "file" if overridden else "default",
            }
        )
    return out


def update(items: dict[str, Any]) -> list[dict[str, Any]]:
    """批量保存（仅白名单内的键；范围钳制后原子落盘），返回保存后的快照"""
    global _values
    with _lock:
        data = dict(_load())
        for key, value in items.items():
            if key not in WHITELIST:
                raise ValueError(f"不支持设置的参数: {key}")
            data[key] = _clamp(key, value)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(SETTINGS_FILE, {"items": data}, indent=2)
        _values = data
    apply_runtime()
    return snapshot()


def reset(keys: list[str] | None = None) -> list[dict[str, Any]]:
    """恢复默认：keys 为空则清空全部覆盖值"""
    global _values
    with _lock:
        data = dict(_load())
        if not keys:
            data = {}
        else:
            for k in keys:
                data.pop(k, None)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(SETTINGS_FILE, {"items": data}, indent=2)
        _values = data
    apply_runtime()
    return snapshot()


def apply_runtime() -> None:
    """把「立即生效」类参数推进相关模块的运行态：
    - 下载节流/重试：写回 download_files 的运行态变量（默认值仍定义在该模块，此处只覆盖）；
    - 任务间并发：调整下载 worker 数量。
    其余参数由各调用点每次读取，无需推送。
    """
    download_files.configure(
        interval=get_float("download_interval", download_files.DOWNLOAD_INTERVAL),
        max_retries=get_int("download_max_retries", download_files.MAX_RETRIES),
        retry_delay=get_float("download_retry_delay", download_files.RETRY_DELAY),
    )
    # 延迟导入：download_tasks 依赖本模块（settings），模块级互相导入会成环
    import download_tasks

    download_tasks.manager.resize_workers(
        get_int("download_task_concurrency", config.DOWNLOAD_TASK_CONCURRENCY)
    )
