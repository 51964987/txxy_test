"""E2E 断言（E2E 专用，跑完即删）：/api/kb/logs + /api/kb/run 全链路真实实测。

断言清单（先列全分支再写用例）：
- 初始态：running=False / lines=[] / last_seq=0 / progress=None；
- 触发：POST /kb/run 200 {"started": true}；运行中重复触发 → 409；
- 运行态：轮询窗口内捕获 running=True；after 游标增量只含新行且单调推进；
- 结束态：running=False / progress=None（finally 清空）；日志含「批次开始 / 批次结束」；
- 调度快照：schedule.kb.last 记录本次执行结果、kb.running=False。
"""
import json
import time

import requests

BASE = "http://127.0.0.1:8100/api"


def main() -> int:
    # 1) 初始态
    r = requests.get(f"{BASE}/kb/logs", timeout=5).json()
    assert set(r) == {"running", "lines", "last_seq", "progress"}, r
    assert r["running"] is False and r["lines"] == [] and r["last_seq"] == 0 and r["progress"] is None, r
    print("[1] 初始态 OK：running=False lines=0 last_seq=0 progress=None")

    # 2) 触发 + 运行中重复触发必须 409
    r = requests.post(f"{BASE}/kb/run", timeout=10)
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json() == {"started": True}, r.text
    print("[2] POST /kb/run OK：started=True")
    r2 = requests.post(f"{BASE}/kb/run", timeout=10)
    print(f"[2b] 运行中重复触发：HTTP {r2.status_code} detail={r2.json().get('detail', '')}")
    assert r2.status_code == 409, r2.status_code

    # 3) 运行态轮询：running=True + 增量游标推进
    seen_running = False
    last_seq = 0
    total_lines = 0
    deadline = time.time() + 40
    while time.time() < deadline:
        time.sleep(0.5)
        r = requests.get(f"{BASE}/kb/logs", params={"after": last_seq}, timeout=5).json()
        if r["running"]:
            seen_running = True
        if r["lines"]:
            assert all(l["seq"] > last_seq for l in r["lines"]), "增量必须只含 after 之后的新行"
            assert r["lines"][-1]["seq"] == r["last_seq"], "last_seq 必须等于最后一行 seq"
            last_seq = r["last_seq"]
            total_lines += len(r["lines"])
        if not r["running"] and r["progress"] is None and total_lines > 0:
            break
    assert seen_running, "轮询窗口内未捕获 running=True"
    print(f"[3] 运行态 OK：running=True 已捕获；增量拉取 {total_lines} 行，游标推进至 seq={last_seq}")

    # 4) 结束态
    r = requests.get(f"{BASE}/kb/logs", params={"after": 0}, timeout=5).json()
    assert r["running"] is False, r["running"]
    assert r["progress"] is None, r["progress"]
    texts = [l["text"] for l in r["lines"]]
    assert any("批次开始" in t for t in texts), texts[:3]
    assert any("批次结束" in t for t in texts), texts[-3:]
    print("[4] 结束态 OK：running=False progress=None，日志含 批次开始/批次结束")
    print("---- 日志尾部 ----")
    for t in texts[-8:]:
        print("   ", t)

    # 5) 调度快照
    r = requests.get(f"{BASE}/schedule", timeout=5).json()
    kb = r["kb"]
    print("[5] schedule.kb.last =", json.dumps(kb.get("last"), ensure_ascii=False))
    assert kb["running"] is False
    assert kb.get("last", {}).get("action") in ("done", "failed")
    print("全部断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
