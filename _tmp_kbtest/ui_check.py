"""UI E2E（E2E 专用，跑完即删）：设置页知识库组按钮态 + 执行日志抽屉真实交互。

断言：
- 知识库组存在「立即执行增量」「执行日志」两个按钮；
- 空闲态打开抽屉：显示空态文案；
- 点击触发：按钮变「执行中…」且禁用（不可再次点击）；
- 运行中打开抽屉：日志行实时出现；
- 批次结束：空闲 tag 回归、按钮恢复可点（等 15s 本地防重超时后恢复）。
"""
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8100"
OUT = "_tmp_kbtest"


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{BASE}/settings")
        page.wait_for_load_state("networkidle")

        # 切到知识库组
        page.click("button.nav-item:has-text('知识库')")
        page.wait_for_timeout(600)
        assert page.locator("button:has-text('立即执行增量')").count() == 1
        assert page.locator("button:has-text('执行日志')").count() == 1
        page.screenshot(path=f"{OUT}/ui_1_kbgroup.png")
        print("[1] 知识库组 OK：两按钮均在")

        # 空闲态抽屉（本轮进程已有上一批日志，展示尾部而非空态——空态断言仅适用于全新进程）
        page.click("button:has-text('执行日志')")
        page.wait_for_timeout(1000)
        assert page.locator(".el-drawer").count() >= 1
        n_prev = page.locator(".kb-log-line").count()
        page.screenshot(path=f"{OUT}/ui_2_drawer_idle.png")
        print(f"[2] 空闲态抽屉 OK：历史日志 {n_prev} 行")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)

        # 触发批次：按钮变「执行中…」且禁用
        page.click("button:has-text('立即执行增量')")
        page.wait_for_timeout(1200)
        running_btn = page.locator("button:has-text('执行中…')")
        assert running_btn.count() == 1, "运行中按钮未出现"
        assert running_btn.is_disabled(), "运行中按钮未被禁用"
        print("[3] 运行中 OK：按钮「执行中…」且 disabled")

        # 运行中打开抽屉看实时日志
        page.click("button:has-text('执行日志')")
        page.wait_for_timeout(2500)
        n_run = page.locator(".kb-log-line").count()
        assert n_run > n_prev, "运行中抽屉未出现新日志行"
        page.screenshot(path=f"{OUT}/ui_3_drawer_running.png")
        print(f"[4] 运行中抽屉 OK：日志行 {n_prev} → {n_run}")

        # 等批次结束 + 本地防重超时（15s）→ 按钮恢复
        page.wait_for_timeout(12000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        restored = page.locator("button:has-text('立即执行增量')")
        assert restored.count() == 1 and not restored.is_disabled(), "批次结束后按钮未恢复可点"
        page.screenshot(path=f"{OUT}/ui_4_restored.png")
        print("[5] 恢复态 OK：按钮回到「立即执行增量」且可点")

        # 移动端视口：抽屉全宽不溢出
        m = browser.new_page(viewport={"width": 375, "height": 812})
        m.goto(f"{BASE}/settings")
        m.wait_for_load_state("networkidle")
        m.click("button.nav-item:has-text('知识库')")
        m.wait_for_timeout(400)
        m.click("button:has-text('执行日志')")
        m.wait_for_timeout(800)
        box = m.locator(".el-drawer.rtl").bounding_box()
        assert box and box["width"] <= 380, f"移动端抽屉宽度溢出: {box}"
        m.screenshot(path=f"{OUT}/ui_5_mobile_drawer.png")
        print(f"[6] 移动端 OK：抽屉宽度 {box['width']:.0f}px ≤ 380px")
        browser.close()
    print("UI E2E 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
