import requests
from bs4 import BeautifulSoup
import csv
import sqlite3
import time
import re
import os
import sys
import traceback
from datetime import datetime
from typing import TextIO

import file_logger
import run_recorder
import txxy_env
from http_headers import ACCEPT_HTML, build_headers

# ============ 配置区域 ============
# 唯一业务域名：抓取与入库/展示共用（默认值只在项目根 txxy_env.py 一处维护，
# 本文件不再写域名默认值）。本地 1024 代理仅影响「实际请求」这一层，
# 由 txxy_env.to_fetch_url 处理；业务 URL 与入库数据恒为公开域名/相对路径。
# 命令行 http(s) / --public 参数可显式覆盖业务域名，见 _apply_cli_args()
ROOT_URL = txxy_env.PUBLIC_DOMAIN    # 兼容旧变量名：业务抓取域名（= 公开域名）
PUBLIC_URL = txxy_env.PUBLIC_DOMAIN  # 入库拼接域名（已与抓取合并为同一个）
BASE_URL = ROOT_URL + "/thread0806.php"  # 基础地址（业务 URL）
FID = "2"                                # 版块ID
START_PAGE = 1                           # 起始页码
END_PAGE = 100                           # 结束页码（可自行修改）
AUTO_DETECT_END_PAGE = False             # 是否动态获取末页页码（False 时使用 END_PAGE 配置值）
FORCE_RESTART = False                    # 是否忽略断点进度强制重跑（--restart）

# 输出目录 & 文件（统一放在 outputs/日期/ 下：最外层 outputs，再到日期目录）
# 批次时间戳复用 file_logger 里的唯一定义（run_batch 通过环境变量注入，单跑时取当前时刻），
# 使同批次的 CSV / 进度 / 日志共享同一时间戳、落在同一日期目录——此前此处另算一份，
# 跨午夜时子进程产物会与批次本身分处两个日期目录，断点续传也读不到之前的进度。
_RUN_BATCH_TS = file_logger.run_batch_ts()
_OUTPUT_DATE = _RUN_BATCH_TS[:8]
OUTPUT_DIR = f"outputs/{_OUTPUT_DATE}"
# 文件名按「版块 + 日期」固定，不再带批次时间戳：
# 断点续传是按精确文件名读取进度的（get_last_page），此前每批一套新文件名，
# 旧进度永远读不到 → 续传形同虚设、每次都从第 1 页重抓，磁盘还堆一整套文件。
# 固定后同一天重复运行复用同一文件：进度能续上，CSV 也按 append 累积。
OUTPUT_FILE = f"{OUTPUT_DIR}/{FID}_output_{_OUTPUT_DATE}.csv"
PROGRESS_FILE = f"{OUTPUT_DIR}/{FID}_progress_{_OUTPUT_DATE}.txt"
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "posts.db")

# 请求头，模拟浏览器（与下载模块共用 http_headers 里唯一的 UA 定义）
HEADERS = build_headers(ACCEPT_HTML)

# 页间请求间隔自适应（秒）：站点响应顺利时逐渐逼近下限，请求重试/失败后立即放大，
# 降低被封风险；初始值沿用原固定 3s
REQUEST_INTERVAL_MIN = 1.0
REQUEST_INTERVAL_MAX = 10.0
REQUEST_INTERVAL_INIT = 3.0
REQUEST_INTERVAL_STEP = 0.5
# 单页请求最大重试次数（网络异常 / 超时 / 5xx / 429 时重试）
REQUEST_MAX_RETRIES = 3
# 重试基础等待秒数，实际等待 = 基础值 × 第几次重试（第1次重试等3s，第2次等6s）
RETRY_BASE_DELAY = 3
# 连续失败页数阈值，达到后判定站点不可用并停止抓取，避免空转
MAX_CONSECUTIVE_FAILURES = 3

# 每 N 页刷新 CSV 磁盘
BATCH_SIZE = 10
# SQLite 批量写入行数（积累到此阈值再一次性写入，减少 commit 次数）
SQLITE_BATCH_ROWS = 500

# ============ 核心逻辑 ============

BLOCKED_TEXT = "您沒有登錄或者您沒有權限訪問此頁面，可能有如下幾個原因"

# 请求间隔自适应运行状态
_current_interval = REQUEST_INTERVAL_INIT  # 当前页间间隔，随站点状态动态调整
_retried_this_page = False  # 本页请求是否发生过重试/失败


def _adjust_interval(retried: bool) -> float:
    """根据本页请求是否发生重试，调整下一页的请求间隔（秒）。

    - 重试过（站点压力大 / 异常）：间隔翻倍，上限 REQUEST_INTERVAL_MAX
    - 顺利：间隔按步长递减，下限 REQUEST_INTERVAL_MIN
    """
    global _current_interval
    if retried:
        _current_interval = min(_current_interval * 2, REQUEST_INTERVAL_MAX)
        print(f"[FID={FID}] [限速] 本页请求异常，请求间隔上调至 {_current_interval:.1f}s")
    else:
        _current_interval = max(_current_interval - REQUEST_INTERVAL_STEP, REQUEST_INTERVAL_MIN)
    return _current_interval


def fetch_page(page_num: int) -> str | None:
    """获取单页HTML内容，带重试。

    重试策略：
    - 网络异常（连接拒绝 / 超时等）与 408/429/5xx 状态码：按退避递增重试，最多 REQUEST_MAX_RETRIES 次
    - 其它状态码（4xx 等）：属于确定性失败，不重试，直接返回 None
    """
    global _retried_this_page
    params: dict[str, str | int] = {
        "fid": FID,
        "search": "",
        "page": page_num,
    }
    # 业务 URL 恒为公开域名；仅在此处按本地代理开关转换实际请求地址（传输层）。
    # 打印实际请求地址，代理环境可见 http://127.0.0.1:1024，直连环境见公开域名
    fetch_url = txxy_env.to_fetch_url(BASE_URL)
    print(f"[FID={FID}] 正在请求 版块第 {page_num} 页: {fetch_url}?fid={FID}&search=&page={page_num}")
    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            resp = requests.get(fetch_url, params=params, headers=HEADERS, timeout=15)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                if BLOCKED_TEXT in resp.text:
                    print(f"\n[终止] 检测到[FID={FID}] 版块权限拦截，当前账号无权访问，程序停止。")
                    sys.exit(1)
                return resp.text
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                # 服务端瞬时故障，值得重试
                _retried_this_page = True
                print(f"[FID={FID}] [警告] 第 {page_num} 页返回状态码 {resp.status_code}（第 {attempt}/{REQUEST_MAX_RETRIES} 次），稍后重试")
            else:
                # 4xx 等确定性失败，重试无意义
                print(f"[FID={FID}] [警告] 第 {page_num} 页返回状态码: {resp.status_code}，跳过")
                return None
        except Exception as e:
            _retried_this_page = True
            print(f"[FID={FID}] [错误] 第 {page_num} 页请求失败（第 {attempt}/{REQUEST_MAX_RETRIES} 次）: {e}")
        if attempt < REQUEST_MAX_RETRIES:
            time.sleep(RETRY_BASE_DELAY * attempt)
    return None


def parse_links(html: str) -> list[tuple[str, str, str, str, str, str]]:
    """从HTML中提取帖子标题、链接、点赞数、作者、回复数、发布时间戳。

    以列表行的标题链接 <a href="/htm_data/..." target="_blank" id="t..."> 为准，
    再定位其所在 <tr> 行，按列提取：
    - 点赞数：第 1 个 td 内 <span class="s3"> 的文本（无则取该 td 纯文本）
    - 作者：行内 <a class="bl"> 的文本
    - 回复数：第 4 个 td（索引 3）的纯文本
    - 发布时间戳：作者单元格 <div class="f12"> 下带 data-timestamp 属性的 <span>
      （如 "1787806292s"，去掉尾部 s 取 Unix 秒；span 可能带或不带 class="s3"，
      当日新帖才有高亮 class，故不限定 class）；同行末楼 <a class="f10"> 也带
      data-timestamp（是最后回复时间），必须限定从 div.f12 内取，避免误取回复时间；
      属性缺失或非法时回退当前时刻，保证 date/created_at 始终非空可排序
    目标行结构参考：
      <td><span class="s3">38</span></td>                     点赞
      <td class="tal"><h3><a ...>标题</a></h3></td>            标题
      <td><a class="bl">作者</a><div class="f12">
          <span class="s3" data-timestamp="1787806292s">7 小時</span></div></td>
                                                              作者 + 发布时间
      <td>24</td>                                              回复数
    结构不同或字段缺失时相应返回空字符串/回退值，不影响主数据。
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str, str, str, str, str]] = []

    # 匹配 <a href="/htm_data/..." target="_blank" id="t...">文字</a>
    for a_tag in soup.find_all("a", href=re.compile(r"^/htm_data/\d+/\d+/\d+\.html$"), target="_blank"):
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True)
        tag_id = str(a_tag.get("id", ""))
        if not (href and title and tag_id.startswith("t")):
            continue

        likes = author = replies = ""
        pub_ts = ""
        tr = a_tag.find_parent("tr")
        if tr is not None:
            tds = tr.find_all("td")
            if tds:
                # 点赞数：第 1 个 td 中的 <span class="s3">（如 <span class="s3">39</span>）
                like_span = tds[0].find("span", class_="s3")
                likes = like_span.get_text(strip=True) if like_span else tds[0].get_text(strip=True)
            # 发布时间戳：作者单元格 <div class="f12"> 下带 data-timestamp 属性的
            # <span>（如 "1787806292s"，去尾 s 得 Unix 秒）。注意该 span 可能带也可能
            # 不带 class="s3"（当日新帖才有高亮 class），故仅限定属性不限定 class；
            # 同行末楼 <a class="f10"> 的 data-timestamp 为最后回复时间，不可误取
            f12_div = tr.find("div", class_="f12")
            if f12_div is not None:
                ts_span = f12_div.find(attrs={"data-timestamp": True})
                if ts_span is not None:
                    raw_ts = str(ts_span.get("data-timestamp", ""))
                    candidate = raw_ts.rstrip("sS").strip()
                    if candidate.isdigit():
                        pub_ts = candidate
            # 作者：<a href="/thread0806.php?fid=..." class="bl">阿东虫</a>
            author_tag = tr.find("a", class_="bl")
            if author_tag:
                author = author_tag.get_text(strip=True)
            # 回复数：第 4 个 td 的纯文本（如 17）
            if len(tds) >= 4:
                replies = tds[3].get_text(strip=True)

        # 页面缺属性 / 结构异常时回退当前时刻，保证发布时间字段始终有效
        if not pub_ts:
            pub_ts = str(int(time.time()))
        results.append((str(title), str(href), str(likes), str(author), str(replies), pub_ts))

    return results


def get_end_page_from_html() -> tuple[int, str | None]:
    """从第一页 HTML 中提取末页页码，同时返回 HTML 避免重复请求"""
    html = fetch_page(1)
    if not html:
        return 0, None
    soup = BeautifulSoup(html, "html.parser")
    last_tag = soup.find("a", id="last")
    if not last_tag:
        return 0, html
    href = str(last_tag.get("href", ""))
    match = re.search(r"page=(\d+)", href)
    end = int(match.group(1)) if match else 0
    return end, html


def get_last_page() -> int:
    """读取进度文件，获取上次完成的页码（0 表示无进度）"""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return 0
    return 0


def save_progress(page: int) -> None:
    """保存当前已完成页码"""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        _ = f.write(str(page))


def save_to_sqlite(
    conn: sqlite3.Connection,
    fid: str,
    rows: list[tuple[str, str, str, str, str, str]],
    max_retries: int = 3,
) -> int:
    """带重试的批量写入 SQLite（处理多进程并发写入冲突）。

    连接已用 timeout=15 设置 busy_timeout（等锁最长 15 秒），绝大多数并发写冲突
    由 SQLite 内部等待消化；此处重试仅兜底极少数等锁超时仍失败的情况，
    避免单次写入失败直接丢数据。返回本次实际写入条数：title 已存在时覆盖更新
    （upsert），新增与覆盖均计 1 条。

    写入语义：
    - date / created_at 由提取的帖子真实发布时间戳（pub_ts，Unix 秒）同源派生；
    - upsert 冲突时将 date/created_at 一并覆盖为本次提取的真值（恒定事实，幂等），
      存量旧数据（ISO 入库串 / 抓取日）在被重抓到时自动修正为真实发布时间；
    - update_at/update_date 记录最近覆盖写入时刻，首次插入为空字符串。"""
    now = datetime.now()
    update_ts = now.isoformat()                      # 最近写入时刻（如 2026-08-27T13:06:03.123456）
    update_date = now.strftime("%Y-%m-%d")           # 最近写入日期（如 2026-08-27）
    data = [
        (
            fid,
            # 发布日期：由帖子真实发布时间戳派生（本机时区北京时间，与站点一致）
            datetime.fromtimestamp(int(pub_ts)).strftime("%Y-%m-%d"),
            title,
            url,
            likes,
            author,
            replies,
            pub_ts,                                  # created_at：帖子发布时间戳（Unix 秒字符串）
            update_ts,
            update_date,
        )
        for title, url, likes, author, replies, pub_ts in rows
    ]
    for attempt in range(1, max_retries + 1):
        try:
            before = conn.total_changes
            _ = conn.executemany(
                "INSERT INTO posts "
                + "(fid, date, title, url, likes, author, replies, update_at, update_date, created_at) "
                + "VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?) "
                + "ON CONFLICT(title) DO UPDATE SET "
                + "fid=excluded.fid, url=excluded.url, "
                + "likes=excluded.likes, author=excluded.author, "
                + "replies=excluded.replies, "
                # 发布信息为恒定真值：冲突时一并覆盖，存量旧数据重抓自动修正（幂等无害）
                + "date=excluded.date, created_at=excluded.created_at, "
                + "update_at=?, update_date=?",
                data,
            )
            conn.commit()
            return conn.total_changes - before
        except sqlite3.OperationalError as e:
            if attempt < max_retries and "locked" in str(e).lower():
                time.sleep(0.5 * attempt)
            else:
                raise
    return 0


def _flush_and_close(
    db_conn: sqlite3.Connection,
    csv_file: TextIO,
    sqlite_buffer: list[tuple[str, str, str, str, str, str]],
    fid: str,
) -> int:
    """
    强制刷新 CSV 和 SQLite 缓冲区并关闭连接。
    保证即使中途写入异常，数据也不丢失、连接始终关闭。
    返回最后一批 SQLite 实际插入行数。
    """
    inserted = 0
    # 1. 刷新 SQLite 缓冲区（优先，失败不阻断后续）
    if sqlite_buffer:
        try:
            inserted = save_to_sqlite(db_conn, fid, sqlite_buffer)
            sqlite_buffer.clear()
        except Exception as e:
            print(f"[FID={FID}] [警告] SQLite 剩余数据写入失败（{len(sqlite_buffer)} 条）: {e}", file=sys.stderr)

    # 2. 刷新 CSV 到磁盘
    try:
        csv_file.flush()
        os.fsync(csv_file.fileno())
    except OSError as e:
        print(f"[FID={FID}] [警告] CSV 刷新磁盘失败: {e}", file=sys.stderr)

    # 3. 关闭 SQLite 连接（无论前面是否异常，必须执行）
    try:
        db_conn.close()
    except Exception:
        pass
    return inserted


def _record_run(status: str, rows: int, db_rows: int, duration: int) -> None:
    """把本次 scraper 单跑结果写入 SQLite 运行记录表。

    run_batch 批量启动子进程时通过环境变量 SCRAPER_RECORD_RUN=0 关闭本记录，
    由 run_batch 统一汇总写入，避免同一批数据重复记录。
    """
    if os.environ.get("SCRAPER_RECORD_RUN", "1") != "1":
        return
    run_recorder.record_run(
        datetime.now().strftime("%Y%m%d"),
        "scraper",
        status,
        ok=1 if status == "ok" else 0,
        fail=1 if status != "ok" else 0,
        skip=0,
        csv=rows,
        sqlite=db_rows,
        duration=duration,
        restart=FORCE_RESTART,
        sections=[
            {
                "fid": FID,
                "name": f"版块{FID}",
                "status": "ok" if status == "ok" else "fail",
                "csv": rows,
                "sqlite": db_rows,
                "duration": duration,
            }
        ],
    )
    print(f"[FID={FID}] [入库] 运行记录已写入 SQLite（status={status}）")


def main() -> None:
    # 记录本次运行的起始时间（用于写入运行记录表的耗时字段）
    start_time = time.time()

    # --- 断点续写：读取上次进度 ---
    if FORCE_RESTART:
        # --restart：忽略断点，从起始页重跑；当天该版块已生成的 CSV/进度文件一并
        # 删除重新生成，避免旧数据行与新数据行重复（删除留痕，随日志输出）
        for _f in (OUTPUT_FILE, PROGRESS_FILE):
            if os.path.exists(_f):
                os.remove(_f)
                print(f"[FID={FID}] [重跑] 已删除旧文件，重新生成: {_f}")
        last_page = 0
        start_page = START_PAGE
        print(f"[FID={FID}] [重跑] 已指定 --restart，忽略断点进度，从第 {start_page} 页重新抓取\n")
    else:
        last_page = get_last_page()
        start_page = max(START_PAGE, last_page + 1)
        if last_page > 0:
            print(f"检测到[FID={FID}] 版块断点进度：已完成第 {last_page} 页，从第 {start_page} 页继续\n")

    # --- 动态获取末页页码（同时复用第 1 页 HTML） ---
    end_page = END_PAGE  # 默认使用配置值
    first_page_html: str | None = None
    if AUTO_DETECT_END_PAGE:
        detected, first_page_html = get_end_page_from_html()
        if detected > 0:
            if detected != END_PAGE:
                print(f"[FID={FID}] 版块末页已更新: {END_PAGE} → {detected}")
            end_page = detected
        else:
            print(f"[FID={FID}] [警告] 版块无法动态获取末页，使用配置值: {end_page}")
    else:
        print(f"[FID={FID}] 版块使用配置末页: {end_page}")

    total_pages = end_page - start_page + 1
    if total_pages <= 0:
        print(f"[FID={FID}] 版块所有页面已完成，无需重复抓取")
        # 批量模式（run_batch 子进程）：向本次运行记录上报该版块已完成（进度 100），
        # 避免 run_sections 该行一直停在 running/0%（且因未上报 total_pages 而被整体
        # 进度聚合排除，导致其它版块接近完成时整体进度提前显示 100%）。
        _batch_run_id = os.environ.get("SCRAPER_RUN_ID", "").strip()
        if _batch_run_id:
            try:
                _rid = int(_batch_run_id)
            except ValueError:
                _rid = 0
            if _rid:
                run_recorder.update_section(
                    _rid,
                    FID,
                    status="ok",
                    current_page=end_page,
                    total_pages=end_page - START_PAGE + 1,
                    csv=0,
                    sqlite=0,
                    duration=int(time.time() - start_time),
                )
        else:
            # 单跑模式：本次未实际抓取，但数据已完整，记一条成功记录
            _record_run("ok", 0, 0, int(time.time() - start_time))
        return

    # 版块全量页数（进度口径用）：从 START_PAGE 到末页的总页数。
    # 断点续传时 current_page 是绝对页码，若 total_pages 用“本次剩余页数”会导致
    # 进度一开跑就虚高（如上次抓到第 50 页、本次剩 50 页，第 51 页即 51/50≈100%）。
    # 统一用全量页数，进度从上次断点位置继续平滑增长、不回退。
    report_total_pages = end_page - START_PAGE + 1

    # --- 实时运行记录：运行中创建 running 记录，逐页上报进度 ---
    # 批量模式（run_batch 子进程）：环境变量 SCRAPER_RUN_ID 关联已创建的运行记录，
    # 本脚本只负责更新自己版块的明细行（进度/条数/状态）；
    # 单跑模式：自动创建 running 记录，结束时写最终汇总。
    run_id = 0
    _env_run_id = os.environ.get("SCRAPER_RUN_ID", "").strip()
    if _env_run_id:
        try:
            run_id = int(_env_run_id)
        except ValueError:
            run_id = 0
    elif os.environ.get("SCRAPER_RECORD_RUN", "1") == "1":
        run_id = run_recorder.start_run(
            datetime.now().strftime("%Y%m%d"),
            "scraper",
            [{"fid": FID, "name": f"版块{FID}", "total_pages": report_total_pages}],
        )

    print(f"开始抓取[FID={FID}] 版块，共 {total_pages} 页（第 {start_page} ~ {end_page} 页）\n")

    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 连接共享 SQLite 数据库（表及 WAL 模式已由 init_db.py 一次性初始化）
    # timeout=15：多进程并发写时用 SQLite 内置 busy_timeout 等锁（最多 15 秒），
    # 连续等待远优于原先手动 sleep 退避的断续轮询
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    db_conn = sqlite3.connect(DB_FILE, timeout=15)

    # 判断是否需要写入 CSV 表头
    write_header = not os.path.exists(OUTPUT_FILE)
    csv_header = ["标题", "地址", "点赞数", "作者", "回复数"]
    if not write_header:
        # 校验已有 CSV 表头是否匹配当前字段，避免新旧结构混写造成列错位
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8-sig", errors="replace") as _f:
                first_line = _f.readline().strip()
            if first_line != ",".join(csv_header):
                print(
                    f"[FID={FID}] [警告] 已有 CSV 表头与当前字段不一致: {first_line!r}，"
                    + f"后续追加将按新表头 {','.join(csv_header)} 写入，可能出现列错位；"
                    + f"如需完整数据建议删除 {OUTPUT_FILE} 后重新抓取",
                    file=sys.stderr,
                )
        except OSError as e:
            print(f"[FID={FID}] [警告] 无法读取已有 CSV 表头: {e}", file=sys.stderr)

    # 以追加模式打开 CSV，解析一条写一条
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(csv_header)

        batch_count = 0
        total_rows = 0
        db_rows = 0  # SQLite 实际写入条数（新增或覆盖）
        last_saved_page = start_page - 1  # 上一次已成功写入的页码
        sqlite_buffer: list[tuple[str, str, str, str, str, str]] = []  # SQLite 批量写入缓冲区（末位为发布时间戳）
        run_status = "ok"  # ok / cancelled / error，用于运行记录落库

        try:
            consecutive_failures = 0
            for page in range(start_page, end_page + 1):
                # 复用获取末页时已请求的第 1 页 HTML，避免重复请求
                if page == start_page and page == 1 and first_page_html:
                    html = first_page_html
                else:
                    html = fetch_page(page)

                if html:
                    consecutive_failures = 0
                    links = parse_links(html)
                    print(f"[FID={FID}] 版块 第 {page} 页提取到 {len(links)} 条数据")

                    # 入库 / 写 CSV 只存相对路径（/htm_data/...，经 to_storage_path 规范化）：
                    # 数据与域名解耦，换域名/换环境零成本；展示层渲染时再拼公开域名。
                    # pub_ts（发布时间戳）不写 CSV，仅随缓冲区入 SQLite
                    for title, href, likes, author, replies, pub_ts in links:
                        full_url = href if href.startswith("http") else f"{PUBLIC_URL}{href}"
                        store_url = txxy_env.to_storage_path(full_url)
                        writer.writerow([title, store_url, likes, author, replies])
                        sqlite_buffer.append((title, store_url, likes, author, replies, pub_ts))
                        total_rows += 1

                    # SQLite 批量写入：积累到阈值时一次性提交
                    if len(sqlite_buffer) >= SQLITE_BATCH_ROWS:
                        db_rows += save_to_sqlite(db_conn, FID, sqlite_buffer)
                        print(f"[FID={FID}] 版块[已保存] 前 {page} 页数据SQLite 实际入库 {db_rows} 条（标题重复已覆盖更新）\n")
                        sqlite_buffer.clear()

                    # 数据已写入 CSV 缓冲区，更新已保存页码
                    last_saved_page = page
                    save_progress(page)
                    # 实时上报版块进度与条数（批量/单跑共用），失败页不推进进度。
                    # csv 为已写入 CSV 的累计行数；sqlite 为已入库 + 缓冲区待入库行数，
                    # 保证运行中“CSV/SQLite 条数”与进度同步实时刷新，而非等运行结束才一次性写入
                    if run_id:
                        run_recorder.update_section(
                            run_id,
                            FID,
                            current_page=page,
                            total_pages=report_total_pages,
                            csv=total_rows,
                            sqlite=db_rows + len(sqlite_buffer),
                        )
                else:
                    # 重试后仍失败：不推进进度，下次运行会重抓该页，避免漏数据
                    consecutive_failures += 1
                    print(f"[FID={FID}] 版块 第 {page} 页重试后仍失败，进度停留在第 {last_saved_page} 页（下次运行将重抓）")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        print(f"[FID={FID}] [终止] 连续 {consecutive_failures} 页请求失败，疑似站点不可用，停止本次抓取")
                        break
                batch_count += 1

                # 每 BATCH_SIZE 页强制刷新 CSV 磁盘
                if batch_count >= BATCH_SIZE:
                    f.flush()
                    print(f"[FID={FID}] 版块[已保存] 前 {page} 页数据已写入磁盘（累计 {total_rows} 条）\n")
                    batch_count = 0

                # 页间延时（自适应：本页发生过重试则拉大间隔，否则逐步收敛到下限）
                if page < end_page:
                    global _retried_this_page
                    interval = _adjust_interval(_retried_this_page)
                    _retried_this_page = False
                    time.sleep(interval)

        except KeyboardInterrupt:
            run_status = "cancelled"
            print("\n[中断] 用户手动终止 (Ctrl+C)")
        except SystemExit:
            # BLOCKED_TEXT 权限拦截等场景 sys.exit(1)：记录为 error 后继续向外抛出
            run_status = "error"
            raise
        except Exception as e:
            run_status = "error"
            print(f"\n[异常] 程序发生错误: {e}", file=sys.stderr)
            traceback.print_exc()
        finally:
            # 无论何种退出方式，都强制刷新数据到磁盘
            db_rows += _flush_and_close(db_conn, f, sqlite_buffer, FID)
            # 使用 last_saved_page（实际写入成功的页码），而非发生异常时的 current_page
            save_progress(last_saved_page)
            print(f"\n[FID={FID}] 版块[已保存] 共写入 {total_rows} 条数据到 {OUTPUT_FILE}（进度至第 {last_saved_page} 页）")
            print(f"[FID={FID}] 版块 SQLite 实际入库 {db_rows} 条（标题重复已覆盖更新）")
            # 机器汇总行：raw 模式不加时间戳，保持可解析格式
            with file_logger.raw():
                print(f"__SUMMARY__ fid={FID} rows={total_rows} db_rows={db_rows} pages={last_saved_page}")
            # 运行记录落库：有 run_id 时更新版块明细并（单跑）写运行汇总；
            # 否则退回原一次性记录（仅单跑且无可用记录时触发）
            _elapsed = int(time.time() - start_time)
            if run_id:
                run_recorder.update_section(
                    run_id,
                    FID,
                    status="ok" if run_status == "ok" else "fail",
                    current_page=last_saved_page,
                    total_pages=report_total_pages,
                    csv=total_rows,
                    sqlite=db_rows,
                    duration=_elapsed,
                )
                # 单跑模式：记录由本脚本创建，结束运行并写汇总；
                # 批量模式由 run_batch 统一汇总，此处不重复写入
                if os.environ.get("SCRAPER_RECORD_RUN", "1") == "1":
                    run_recorder.finish_run(
                        run_id,
                        run_status,
                        ok=1 if run_status == "ok" else 0,
                        fail=1 if run_status == "error" else 0,
                        skip=0,
                        csv=total_rows,
                        sqlite=db_rows,
                        duration=_elapsed,
                    )
            else:
                _record_run(run_status, total_rows, db_rows, _elapsed)


def _apply_cli_args() -> None:
    """从命令行参数覆盖模块级配置。

    参数宽松解析：
    - 第 1 个参数：版块ID（必填）
    - 其后数字参数依次作为 [起始页] [结束页]（可省略）
    - 其后 http(s) 参数作为业务域名（可省略，且位置不限）
    - --public <域名>：同义参数，与上面 http(s) 参数等价（保留仅为兼容旧调用习惯）；
      两者都只覆盖唯一业务域名，入库恒为相对路径，本地镜像地址不会入库
    - --restart：忽略断点进度，从起始页强制重跑；当天该版块已生成的 CSV/进度文件
      会被删除重新生成（适用于提示"所有页面已完成，无需重复抓取"后仍想重抓的场景）
    示例:
      python scraper.py 2
      python scraper.py 2 1 50
      python scraper.py 2 https://xx.com
      python scraper.py 2 1 100 https://xx.com
      python scraper.py 2 --public https://xx.com
      python scraper.py 2 --restart
    """
    global FID, START_PAGE, END_PAGE, ROOT_URL, PUBLIC_URL, BASE_URL, OUTPUT_DIR, OUTPUT_FILE, PROGRESS_FILE, FORCE_RESTART
    FORCE_RESTART = False  # 每次解析前重置，仅 --restart 会置为 True  # pyright: ignore[reportConstantRedefinition]
    args = sys.argv[1:]
    if not args:
        print("用法: python scraper.py <版块ID> [起始页] [结束页] [根地址] [--public <域名>] [--restart]")
        print("示例: python scraper.py 2                 # 抓取版块2，第1页~第10页")
        print("      python scraper.py 2 1 50            # 抓取版块2，第1页~第50页")
        print("      python scraper.py 2 https://xx.com  # 仅指定实际域名（根地址），页数用默认值")
        print("      python scraper.py 2 1 100 https://xx.com  # 指定域名 + 抓取范围")
        print("      python scraper.py 2 --public https://xx.com  # 入库链接用该公开域名")
        print("      python scraper.py 2 --restart       # 忽略断点，强制重跑该版块")
        sys.exit(1)
    FID = args[0]  # pyright: ignore[reportConstantRedefinition]
    page_args: list[int] = []
    root_url: str | None = None
    public_url: str | None = None
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--public":
            if i + 1 >= len(args):
                print("[错误] --public 后缺少域名参数", file=sys.stderr)
                sys.exit(1)
            public_url = args[i + 1]
            i += 2
        elif arg == "--restart":
            FORCE_RESTART = True  # pyright: ignore[reportConstantRedefinition]
            print("[配置] 已指定 --restart：忽略断点进度，强制从头重跑")
            i += 1
        elif arg.lower().startswith(("http://", "https://")):
            if root_url is not None:
                print(f"[错误] 根地址重复指定: {root_url} 与 {arg}", file=sys.stderr)
                sys.exit(1)
            root_url = arg
            i += 1
        else:
            try:
                page_args.append(int(arg))
            except ValueError:
                print(f"[错误] 无法识别的参数: {arg!r}（应为数字页码、http(s) 根地址、--public <域名> 或 --restart）", file=sys.stderr)
                print("用法: python scraper.py <版块ID> [起始页] [结束页] [根地址] [--public <域名>] [--restart]", file=sys.stderr)
                sys.exit(1)
            i += 1
    if page_args:
        START_PAGE = page_args[0]  # pyright: ignore[reportConstantRedefinition]
        if START_PAGE < 1:
            print(f"[错误] 起始页必须 >= 1，收到: {START_PAGE}", file=sys.stderr)
            sys.exit(1)
    if len(page_args) >= 2:
        END_PAGE = page_args[1]  # pyright: ignore[reportConstantRedefinition]
        if END_PAGE < START_PAGE:
            print(f"[错误] 结束页 {END_PAGE} 小于起始页 {START_PAGE}", file=sys.stderr)
            sys.exit(1)
    if len(page_args) > 2:
        print(f"[警告] 忽略多余的页码参数: {page_args[2:]}", file=sys.stderr)
    if root_url is not None:
        # 覆盖业务域名（如 https://xx.com）：抓取与入库同源
        txxy_env.PUBLIC_DOMAIN = root_url.rstrip("/")
        print(f"[配置] 已指定业务域名: {txxy_env.PUBLIC_DOMAIN}")
    if public_url is not None:
        if not public_url.lower().startswith(("http://", "https://")):
            print(f"[错误] --public 参数必须是 http(s) 开头的完整域名: {public_url!r}", file=sys.stderr)
            sys.exit(1)
        # 域名已统一：--public 与 http(s) 根地址等价，均写入唯一业务域名
        # （保留该参数仅为兼容 run_batch / 旧调用习惯）
        txxy_env.PUBLIC_DOMAIN = public_url.rstrip("/")
        print(f"[配置] 已指定业务域名: {txxy_env.PUBLIC_DOMAIN}")
    ROOT_URL = txxy_env.PUBLIC_DOMAIN  # pyright: ignore[reportConstantRedefinition]
    PUBLIC_URL = ROOT_URL  # pyright: ignore[reportConstantRedefinition]
    BASE_URL = ROOT_URL + "/thread0806.php"  # pyright: ignore[reportConstantRedefinition]
    OUTPUT_DIR = f"outputs/{_OUTPUT_DATE}"  # pyright: ignore[reportConstantRedefinition]
    OUTPUT_FILE = f"{OUTPUT_DIR}/{FID}_output_{_OUTPUT_DATE}.csv"  # pyright: ignore[reportConstantRedefinition]
    PROGRESS_FILE = f"{OUTPUT_DIR}/{FID}_progress_{_OUTPUT_DATE}.txt"  # pyright: ignore[reportConstantRedefinition]


if __name__ == "__main__":
    _apply_cli_args()
    # 启用日志：所有打印同时输出到 outputs/<日期>/scraper_<FID>_<日期>.log
    _ = file_logger.setup(f"scraper_{FID}")
    print(f"[FID={FID}] 版块{START_PAGE}-{END_PAGE}页 开始抓取")
    main()
