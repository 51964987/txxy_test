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

# ============ 配置区域 ============
# 根地址：默认本地代理 127.0.0.1:1024；
# 也可由命令行 http(s) 参数指定实际域名根地址（如 https://xx.com，位置不限），见 _apply_cli_args()
ROOT_URL = "http://127.0.0.1:1024"
# 入库链接使用的公开域名根地址：默认与 ROOT_URL 相同；
# 本地代理（127.0.0.1:1024）只用于抓取，写入数据库/CSV 的链接应使用真实域名，
# run_batch 始终以 --public <域名> 传入，见 _apply_cli_args()
PUBLIC_URL = ROOT_URL
BASE_URL = ROOT_URL + "/thread0806.php"  # 基础地址
FID = "2"                                # 版块ID
START_PAGE = 1                           # 起始页码
END_PAGE = 50                            # 结束页码（可自行修改）
AUTO_DETECT_END_PAGE = False             # 是否动态获取末页页码（False 时使用 END_PAGE 配置值）

# 输出目录 & 文件（统一放在 outputs/日期/ 下：最外层 outputs，再到日期目录）
_OUTPUT_DATE = datetime.now().strftime("%Y%m%d")
OUTPUT_DIR = f"outputs/{_OUTPUT_DATE}"
OUTPUT_FILE = f"{OUTPUT_DIR}/{FID}_output_{_OUTPUT_DATE}.csv"
PROGRESS_FILE = f"{OUTPUT_DIR}/{FID}_progress.txt"
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "posts.db")

# 请求头，模拟浏览器
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 请求间隔（秒），避免请求过快被封
REQUEST_INTERVAL = 3
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


def fetch_page(page_num: int) -> str | None:
    """获取单页HTML内容，带重试。

    重试策略：
    - 网络异常（连接拒绝 / 超时等）与 408/429/5xx 状态码：按退避递增重试，最多 REQUEST_MAX_RETRIES 次
    - 其它状态码（4xx 等）：属于确定性失败，不重试，直接返回 None
    """
    params: dict[str, str | int] = {
        "fid": FID,
        "search": "",
        "page": page_num,
    }
    url = BASE_URL
    print(f"[FID={FID}] 正在请求 版块第 {page_num} 页: {url}?fid={FID}&search=&page={page_num}")
    for attempt in range(1, REQUEST_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            resp.encoding = "utf-8"
            if resp.status_code == 200:
                if BLOCKED_TEXT in resp.text:
                    print(f"\n[终止] 检测到[FID={FID}] 版块权限拦截，当前账号无权访问，程序停止。")
                    sys.exit(1)
                return resp.text
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                # 服务端瞬时故障，值得重试
                print(f"[FID={FID}] [警告] 第 {page_num} 页返回状态码 {resp.status_code}（第 {attempt}/{REQUEST_MAX_RETRIES} 次），稍后重试")
            else:
                # 4xx 等确定性失败，重试无意义
                print(f"[FID={FID}] [警告] 第 {page_num} 页返回状态码: {resp.status_code}，跳过")
                return None
        except Exception as e:
            print(f"[FID={FID}] [错误] 第 {page_num} 页请求失败（第 {attempt}/{REQUEST_MAX_RETRIES} 次）: {e}")
        if attempt < REQUEST_MAX_RETRIES:
            time.sleep(RETRY_BASE_DELAY * attempt)
    return None


def parse_links(html:str):
    """从HTML中提取符合格式的标题和链接"""
    soup = BeautifulSoup(html, "html.parser")
    results: list[tuple[str, str]] = []

    # 匹配 <a href="/htm_data/..." target="_blank" id="t...">文字</a>
    for a_tag in soup.find_all("a", href=re.compile(r"^/htm_data/\d+/\d+/\d+\.html$"), target="_blank"):
        href = a_tag.get("href", "")
        title = a_tag.get_text(strip=True)
        tag_id = str(a_tag.get("id", ""))
        if href and title and tag_id.startswith("t"):
            results.append((str(title), str(href)))

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
    date: str,
    rows: list[tuple[str, str]],
    max_retries: int = 3,
) -> int:
    """带重试的批量写入 SQLite（处理多进程并发写入冲突）。

    连接已用 timeout=15 设置 busy_timeout（等锁最长 15 秒），绝大多数并发写冲突
    由 SQLite 内部等待消化；此处重试仅兜底极少数等锁超时仍失败的情况，
    避免单次写入失败直接丢数据。返回实际插入行数（INSERT OR IGNORE 跳过已存在标题）。"""
    now = datetime.now().isoformat()
    data = [(fid, date, title, url, now) for title, url in rows]
    for attempt in range(1, max_retries + 1):
        try:
            before = conn.total_changes
            _ = conn.executemany(
                "INSERT OR IGNORE INTO posts (fid, date, title, url, created_at) VALUES (?, ?, ?, ?, ?)",
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
    sqlite_buffer: list[tuple[str, str]],
    fid: str,
    date: str,
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
            inserted = save_to_sqlite(db_conn, fid, date, sqlite_buffer)
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


def main() -> None:
    # --- 断点续写：读取上次进度 ---
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
        return

    print(f"开始抓取[FID={FID}] 版块，共 {total_pages} 页（第 {start_page} ~ {end_page} 页）\n")

    # 确保输出目录存在
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 连接共享 SQLite 数据库（表及 WAL 模式已由 init_db.py 一次性初始化）
    # timeout=15：多进程并发写时用 SQLite 内置 busy_timeout 等锁（最多 15 秒），
    # 连续等待远优于原先手动 sleep 退避的断续轮询
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    db_conn = sqlite3.connect(DB_FILE, timeout=15)
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 判断是否需要写入 CSV 表头
    write_header = not os.path.exists(OUTPUT_FILE)

    # 以追加模式打开 CSV，解析一条写一条
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["标题", "地址"])

        batch_count = 0
        total_rows = 0
        db_rows = 0  # SQLite 实际插入行数（去重后）
        last_saved_page = start_page - 1  # 上一次已成功写入的页码
        sqlite_buffer: list[tuple[str, str]] = []  # SQLite 批量写入缓冲区

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

                    # 补全 URL 并写入 CSV：链接拼接使用公开域名（PUBLIC_URL），
                    # 保证入库链接离开本机仍可访问；本地代理 127.0.0.1:1024 仅用于抓取
                    for title, href in links:
                        full_url = href if href.startswith("http") else f"{PUBLIC_URL}{href}"
                        writer.writerow([title, full_url])
                        sqlite_buffer.append((title, full_url))
                        total_rows += 1

                    # SQLite 批量写入：积累到阈值时一次性提交
                    if len(sqlite_buffer) >= SQLITE_BATCH_ROWS:
                        db_rows += save_to_sqlite(db_conn, FID, today_str, sqlite_buffer)
                        print(f"[FID={FID}] 版块[已保存] 前 {page} 页数据SQLite 实际入库 {db_rows} 条（标题去重后）\n")
                        sqlite_buffer.clear()

                    # 数据已写入 CSV 缓冲区，更新已保存页码
                    last_saved_page = page
                    save_progress(page)
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

                # 页间延时
                if page < end_page:
                    time.sleep(REQUEST_INTERVAL)

        except KeyboardInterrupt:
            print("\n[中断] 用户手动终止 (Ctrl+C)")
        except Exception as e:
            print(f"\n[异常] 程序发生错误: {e}", file=sys.stderr)
            traceback.print_exc()
        finally:
            # 无论何种退出方式，都强制刷新数据到磁盘
            db_rows += _flush_and_close(db_conn, f, sqlite_buffer, FID, today_str)
            # 使用 last_saved_page（实际写入成功的页码），而非发生异常时的 current_page
            save_progress(last_saved_page)
            print(f"\n[FID={FID}] 版块[已保存] 共写入 {total_rows} 条数据到 {OUTPUT_FILE}（进度至第 {last_saved_page} 页）")
            print(f"[FID={FID}] 版块 SQLite 实际入库 {db_rows} 条（标题去重后）")
            # 机器汇总行：raw 模式不加时间戳，保持可解析格式
            with file_logger.raw():
                print(f"__SUMMARY__ fid={FID} rows={total_rows} db_rows={db_rows} pages={last_saved_page}")


def _apply_cli_args() -> None:
    """从命令行参数覆盖模块级配置。

    参数宽松解析：
    - 第 1 个参数：版块ID（必填）
    - 其后数字参数依次作为 [起始页] [结束页]（可省略）
    - 其后 http(s) 参数作为根地址（抓取访问的实际域名，可省略，且位置不限）
    - --public <域名>：指定入库链接使用的公开域名根地址（仅影响写入数据库/CSV 的
      链接拼接，不影响抓取根地址；run_batch 始终以 --public 传入真实域名，
      避免本地代理地址 127.0.0.1:1024 入库）
    示例:
      python scraper.py 2
      python scraper.py 2 1 50
      python scraper.py 2 https://xx.com
      python scraper.py 2 1 100 https://xx.com
      python scraper.py 2 --public https://xx.com
    """
    global FID, START_PAGE, END_PAGE, ROOT_URL, PUBLIC_URL, BASE_URL, OUTPUT_DIR, OUTPUT_FILE, PROGRESS_FILE
    args = sys.argv[1:]
    if not args:
        print("用法: python scraper.py <版块ID> [起始页] [结束页] [根地址] [--public <域名>]")
        print("示例: python scraper.py 2                 # 抓取版块2，第1页~第10页")
        print("      python scraper.py 2 1 50            # 抓取版块2，第1页~第50页")
        print("      python scraper.py 2 https://xx.com  # 仅指定实际域名（根地址），页数用默认值")
        print("      python scraper.py 2 1 100 https://xx.com  # 指定域名 + 抓取范围")
        print("      python scraper.py 2 --public https://xx.com  # 入库链接用该公开域名")
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
                print(f"[错误] 无法识别的参数: {arg!r}（应为数字页码、http(s) 根地址或 --public <域名>）", file=sys.stderr)
                print("用法: python scraper.py <版块ID> [起始页] [结束页] [根地址] [--public <域名>]", file=sys.stderr)
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
        # 覆盖根地址（如 https://xx.com）：BASE_URL / 抓取请求均依赖此值
        ROOT_URL = root_url.rstrip("/")  # pyright: ignore[reportConstantRedefinition]
        print(f"[配置] 已指定实际域名根地址: {ROOT_URL}")
    if public_url is not None:
        if not public_url.lower().startswith(("http://", "https://")):
            print(f"[错误] --public 参数必须是 http(s) 开头的完整域名: {public_url!r}", file=sys.stderr)
            sys.exit(1)
        # 覆盖入库链接用的公开域名：仅影响 full_url 拼接，抓取仍走 ROOT_URL
        PUBLIC_URL = public_url.rstrip("/")  # pyright: ignore[reportConstantRedefinition]
        print(f"[配置] 入库链接使用公开域名: {PUBLIC_URL}")
    BASE_URL = ROOT_URL + "/thread0806.php"  # pyright: ignore[reportConstantRedefinition]
    OUTPUT_DIR = f"outputs/{datetime.now().strftime('%Y%m%d')}"  # pyright: ignore[reportConstantRedefinition]
    OUTPUT_FILE = f"{OUTPUT_DIR}/{FID}_output_{datetime.now().strftime('%Y%m%d')}.csv"  # pyright: ignore[reportConstantRedefinition]
    PROGRESS_FILE = f"{OUTPUT_DIR}/{FID}_progress.txt"  # pyright: ignore[reportConstantRedefinition]


if __name__ == "__main__":
    _apply_cli_args()
    # 启用日志：所有打印同时输出到 outputs/<日期>/scraper_<FID>_<日期>.log
    _ = file_logger.setup(f"scraper_{FID}")
    print(f"[FID={FID}] 版块{START_PAGE}-{END_PAGE}页 开始抓取")
    main()
