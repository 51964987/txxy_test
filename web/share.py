"""文件分享服务（独立端口，完全隔离于前端 SPA）。

设计要点：
- 不挂载任何前端静态资源，/ 也不返回看板，天然不暴露 8088 内的管理界面。
- 一个分享链接 = 一组文件（业界「分享文件夹 / 多选分享」同思路）：token 映射
  outputs/share_index.json 中的 rels（相对路径列表），由独立分享服务渲染为画廊式
  预览页（图片/视频内联预览、其他类型占位，不含下载按钮）。
- 索引文件：主应用负责写入（原子写 + 跨进程排他锁），分享服务只读取。
- 路由（均不依赖前端 SPA）：
    GET /share/{token}            画廊式预览页（目录名 + 文件网格 + 沉浸式 1:1 滑动查看 + 底部缩略图条跳转；筛选约束查看器、图片可缩放、视频打开自动播放、缩略图非黑屏、媒体带主题蓝边框）
    GET /share/{token}/file?i=N   内联返回第 N 个文件（图片/视频浏览器预览）
  支持 Range（Starlette FileResponse 原生支持 206），视频可拖动进度条。
"""
import html
import json
import os
import secrets
import sys
import time
from pathlib import Path

# 自举（单一来源）：保证 web/ 与项目根都在 sys.path，使 config / resources /
# settings / download_files 等可导入。无论从本文件直接运行（python web/share.py）、
# 被 share_server.py 导入，还是被主应用的 app.py 懒加载导入都有效。
_ROOT = Path(__file__).resolve().parent.parent
_WEB = _ROOT / "web"
for _p in (str(_WEB), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config
import resources
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

# ---- 分享索引（token → 相对路径列表 + 过期时间）----
INDEX_FILE = config.OUTPUTS_DIR / "share_index.json"
LOCK_FILE = config.OUTPUTS_DIR / "share_index.lock"

# 可选过期档位（小时）
TTL_HOURS = {"1h": 1, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}
DEFAULT_TTL = "7d"

# 类型 → 中文占位（分享页非媒体类型用）
CATEGORY_CN = {"image": "图片", "video": "视频", "text": "文本", "torrent": "种子", "other": "其他"}


def _load() -> dict:
    if not INDEX_FILE.is_file():
        return {"items": []}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def _save(data: dict) -> None:
    # 原子替换：先写临时文件再 os.replace，读方（分享服务）无锁读取也安全
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_FILE)


def _with_lock(fn):
    """跨进程排他锁：用 O_EXCL 创建锁文件，忙等至超时（单用户并发极低，5s 足够）。"""
    deadline = time.time() + 5
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise RuntimeError("获取分享索引锁超时")
            time.sleep(0.05)
    try:
        return fn()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def create_share(rel_paths: list[str], ttl: str = DEFAULT_TTL) -> dict:
    """创建分享：把一组 downloads/ 内相对路径合并为一个分享链接（一个 token → 多个文件）。

    入参 rel_paths 必须都存在于 downloads/ 内（越界 / 不存在则整体失败），返回 token 与元信息。
    """
    rels: list[str] = []
    for rp in rel_paths or []:
        rel = (rp or "").strip().replace("\\", "/").strip("/")
        if not rel:
            continue
        if resources.resolve_safe(rel) is None:
            raise FileNotFoundError(f"文件不存在或路径越界: {rel}")
        rels.append(rel)
    if not rels:
        raise ValueError("至少需要一个有效文件")
    hours = TTL_HOURS.get(ttl, TTL_HOURS[DEFAULT_TTL])
    token = secrets.token_urlsafe(16)
    now = time.time()
    expire_at = now + hours * 3600

    def _do() -> dict:
        data = _load()
        data.setdefault("items", []).append(
            {
                "token": token,
                "rels": rels,
                "ttl": ttl,
                "created_at": now,
                "expire_at": expire_at,
            }
        )
        _save(data)
        return {
            "token": token,
            "rels": rels,
            "count": len(rels),
            "name": _common_dir(rels),
            "expire_at": expire_at,
        }

    return _with_lock(_do)


def resolve_token(token: str):
    """解析分享 token：过期或任一文件已删除返回 None；否则返回 (meta, metas)。"""
    data = _load()
    for it in data.get("items", []):
        if it.get("token") != token:
            continue
        if it.get("expire_at", 0) < time.time():
            return None
        rels = it.get("rels")
        if not rels:
            return None
        metas = []
        for rel in rels:
            target = resources.resolve_safe(rel)
            if target is None:
                return None
            metas.append(
                {
                    "rel": rel,
                    "name": target.name,
                    "size": target.stat().st_size,
                    "category": resources.category_of(target.name),
                }
            )
        return it, metas
    return None


def _common_dir(rels: list[str]) -> str:
    """取所有相对路径的最深公共目录，作为分享页标题（如 a/b/c.jpg 与 a/b/d.jpg → a/b）。"""
    dirs = [rel.rsplit("/", 1)[0] for rel in rels if "/" in rel]
    if not dirs:
        return "根目录"
    segs = [d.split("/") for d in dirs]
    common = segs[0]
    for s in segs[1:]:
        i = 0
        while i < len(common) and i < len(s) and common[i] == s[i]:
            i += 1
        common = common[:i]
        if not common:
            break
    return "/".join(common) if common else "多个目录"


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if u == "B":
            return f"{int(f)} B"
        if f < 1024:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


# 画廊式预览页模板：用占位符替换，避免 f-string 与 CSS/JS 大括号冲突
_GALLERY_TPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>__TITLE__ · 文件分享</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 20px; background: #f5f7fa; color: #1f2d3d; }
  .head { max-width: 1100px; margin: 0 auto 12px; }
  h1 { font-size: 18px; margin: 0 0 4px; word-break: break-all; }
  .meta { color: #909399; font-size: 13px; }
  /* 工具栏：类型筛选 + 名称搜索（与「浏览」列表同思路） */
  .bar { max-width: 1100px; margin: 0 auto 14px; display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  .filters { display: flex; gap: 8px; flex-wrap: wrap; }
  .chip { border: 1px solid #dcdfe6; background: #fff; color: #606266; border-radius: 16px;
          padding: 5px 14px; font-size: 13px; cursor: pointer; transition: all .15s; }
  .chip:hover { color: #409eff; border-color: #c6e2ff; }
  .chip.active { background: #409eff; border-color: #409eff; color: #fff; }
  .search { flex: 1; min-width: 180px; border: 1px solid #dcdfe6; border-radius: 8px; padding: 7px 12px;
            font-size: 13px; outline: none; transition: border-color .15s; }
  .search:focus { border-color: #409eff; }
  .grid { max-width: 1100px; margin: 0 auto; display: grid;
          grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 14px; }
  .cell { background: #fff; border-radius: 10px; overflow: hidden; cursor: pointer;
          box-shadow: 0 1px 6px rgba(0,0,0,.08); transition: transform .12s; }
  .cell:hover { transform: translateY(-2px); }
  .thumb-wrap { height: 150px; background: #eef1f6; display: flex; align-items: center;
                justify-content: center; overflow: hidden; }
  .thumb { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb.ph { color: #909399; font-size: 14px; }
  .thumb.vthumb { background: #000; }
  .cname { padding: 8px 10px 0; font-size: 13px; line-height: 1.3; word-break: break-all;
           display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .csize { padding: 2px 10px 10px; color: #909399; font-size: 12px; }
  /* 分页：与「浏览」列表同样的客户端分页思路 */
  .pager { max-width: 1100px; margin: 18px auto 0; display: flex; gap: 6px; flex-wrap: wrap;
           justify-content: center; align-items: center; }
  .pager button { border: 1px solid #dcdfe6; background: #fff; color: #606266; border-radius: 6px;
                  padding: 6px 11px; font-size: 13px; cursor: pointer; transition: all .15s; }
  .pager button:hover:not(:disabled) { color: #409eff; border-color: #c6e2ff; }
  .pager button.active { background: #409eff; border-color: #409eff; color: #fff; }
  .pager button:disabled { opacity: .5; cursor: default; }
  .pager .info { color: #909399; font-size: 13px; margin: 0 6px; }
  /* 沉浸式 1:1 滑动查看器：全屏黑底，一次一片，手指/指针 1:1 跟手横滑翻页 */
  #lb { display: none; position: fixed; inset: 0; background: #000; z-index: 999; overflow: hidden; }
  #lb.open { display: block; }
  /* 顶部信息条：沉浸式下自动隐藏，交互时浮现（关闭按钮始终保持可点） */
  #lb-bar { position: fixed; top: 0; left: 0; right: 0; z-index: 1002;
            display: flex; align-items: center; gap: 12px; padding: 10px 14px;
            color: #fff; font-size: 14px; pointer-events: none;
            background: linear-gradient(rgba(0,0,0,.55), rgba(0,0,0,0));
            transition: opacity .3s; }
  #lb-bar .fname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #lb-bar .counter { color: #c0c4cc; font-size: 13px; flex: none; }
  #lb-bar .spacer { flex: 1; min-width: 8px; }
  #lb-close { pointer-events: auto; background: rgba(255,255,255,.16); color: #fff; border: none;
              border-radius: 8px; padding: 6px 14px; font-size: 14px; cursor: pointer; flex: none; }
  #lb-close:hover { background: rgba(255,255,255,.3); }
  /* 滑动轨道：等量 slide 横排，transform 平移切页；touch-action:none 交由 JS 统一处理手势；
     底部预留缩略图条高度，避免内容被遮挡 */
  #lb-track { position: fixed; inset: 0; z-index: 1001; display: flex; flex-wrap: nowrap;
              will-change: transform; touch-action: none; padding-bottom: 72px; }
  .lb-slide { flex: 0 0 100%; width: 100%; height: 100%; display: flex;
              align-items: center; justify-content: center; overflow: hidden; }
  .lb-media { max-width: 100%; max-height: 100%; object-fit: contain;
              -webkit-user-select: none; user-select: none;
              /* 边框 + 投影：纯黑背景上明显区分当前资源边界，便于辨认图片/视频范围 */
              border: 2px solid #409eff; border-radius: 4px;
              box-shadow: 0 0 0 1px rgba(0,0,0,.45), 0 0 12px rgba(64,158,255,.35), 0 10px 34px rgba(0,0,0,.5);
              background: #000; }
  /* 视频播放页默认最大化填充（保持比例，不拉伸变形） */
  .lb-slide video.lb-media { width: 100%; height: 100%; }
  .lb-other { color: #fff; text-align: center; font-size: 15px; }
  .lb-other .lb-hint { display: block; color: #c0c4cc; font-size: 13px; margin-top: 6px; }
  /* 底部缩略图条（Filmstrip）：横滑浏览全部文件，当前项高亮，点击即跳转到该资源；
     常驻显示，作为「① 任意跳转」的入口，与上方沉浸式滑动（②）并存 */
  #lb-strip { position: fixed; left: 0; right: 0; bottom: 0; z-index: 1003;
              display: flex; gap: 8px; padding: 8px 12px; overflow-x: auto; overflow-y: hidden;
              background: rgba(0,0,0,.55); -webkit-overflow-scrolling: touch; touch-action: pan-x; }
  #lb-strip .strip-thumb { flex: 0 0 auto; width: 84px; height: 54px; border-radius: 6px; overflow: hidden;
              position: relative; cursor: pointer; border: 2px solid transparent; opacity: .6;
              transition: opacity .15s, border-color .15s; }
  #lb-strip .strip-thumb.active { opacity: 1; border-color: #409eff; }
  #lb-strip .strip-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  #lb-strip .strip-thumb video { width: 100%; height: 100%; object-fit: cover; display: block; background: #000; }
  #lb-strip .strip-thumb .ph { width: 100%; height: 100%; display: flex; align-items: center;
              justify-content: center; background: #eef1f6; color: #909399; font-size: 11px; }
  /* 操作提示移到顶栏下方，避免与底部缩略图条重叠 */
  #lb-hint { position: fixed; top: 54px; bottom: auto; left: 50%; transform: translateX(-50%);
             z-index: 1002; color: #c0c4cc; font-size: 12px; background: rgba(0,0,0,.4);
             padding: 4px 14px; border-radius: 12px; pointer-events: none; opacity: .85; }
  @media (max-width: 640px) {
    #lb-bar { padding: 8px 10px; gap: 8px; }
  }
</style>
</head>
<body>
  <div class="head">
    <h1>__TITLE__</h1>
    <div class="meta" id="lb-meta">共 __TOTAL__ 个文件 · __SIZE__ · 有效期至 __EXP__</div>
  </div>
  <div class="bar">
    <div class="filters" id="lb-filters">
      <button class="chip active" data-cat="all">全部</button>
      <button class="chip" data-cat="image">图片</button>
      <button class="chip" data-cat="video">视频</button>
      <button class="chip" data-cat="text">文本</button>
      <button class="chip" data-cat="torrent">种子</button>
      <button class="chip" data-cat="other">其他</button>
    </div>
    <input id="lb-search" class="search" type="search" placeholder="搜索文件名…" />
  </div>
  <div class="grid">
__GRID__
  </div>
  <div class="pager" id="lb-pager"></div>

  <div id="lb">
    <div id="lb-bar">
      <button id="lb-close" aria-label="关闭">关闭</button>
      <span class="spacer"></span>
      <span class="fname" id="lb-name"></span>
      <span class="counter" id="lb-idx"></span>
    </div>
    <div id="lb-track"></div>
    <div id="lb-strip"></div>
    <div id="lb-hint">滑动切换 · 点缩略图跳转 · 滚轮/双指缩放图片 · Esc 关闭 · 下滑退出</div>
  </div>

  <script>
    var token = "__TOKEN__";
    var absTotal = __TOTAL__;
    var sizeText = "__SIZE__";
    var PAGE = 60;
    var lb = document.getElementById('lb');
    var track = document.getElementById('lb-track');
    var bar = document.getElementById('lb-bar');
    var strip = document.getElementById('lb-strip');
    var stripThumbs = [];
    var cur = 0;
    // view：当前「可见（经筛选）」文件的全局下标序列；查看器与缩略图条均只在这之中导航，
    // 这样筛选「视频」后，查看器与缩略图条也只覆盖视频，前后翻页不会跳到非视频文件
    var view = [];
    function gidx(k) { return view[k]; }
    // 图片缩放状态（仅图片支持：滚轮 / 双指捏合 / 双击切换，缩放后可拖动平移）
    var zoom = 1, panX = 0, panY = 0;
    function esc(s) {
      return String(s).replace(/[&<>"]/g, function (c) {
        return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];
      });
    }
    // ---------- 沉浸式 1:1 滑动查看器 ----------
    function catOf(i) { var c = document.querySelector('.cell[data-i="' + i + '"]'); return c ? c.getAttribute('data-cat') : 'other'; }
    function nameOf(i) { var c = document.querySelector('.cell[data-i="' + i + '"]'); return c ? c.querySelector('.cname').textContent : ''; }

    // 等量 slide 占位：按筛选后的 view 搭骨架，媒体按「当前 ±2」窗口懒加载，避免大批量请求/内存占用
    function buildSlides() {
      var html = '';
      for (var k = 0; k < view.length; k++) html += '<div class="lb-slide" data-k="' + k + '"></div>';
      track.innerHTML = html;
      loadWindow();
    }
    function loadWindow() {
      var sibs = track.children;
      for (var k = 0; k < sibs.length; k++) {
        var d = k - cur;
        if (d < -2 || d > 2) continue;                 // 只加载紧邻的 5 张
        var s = sibs[k];
        if (s.getAttribute('data-loaded') === '1') continue;
        var gi = gidx(k), cat = catOf(gi), inner;
        if (cat === 'image') {
          inner = '<img class="lb-media" src="/share/' + token + '/file?i=' + gi + '" alt=""/>';
        } else if (cat === 'video') {
          // 当前张为视频时默认自动播放（基于用户点击打开的手势，浏览器允许带声自动播放；切走会自动暂停）
          inner = '<video class="lb-media" controls preload="none" playsinline' + (k === cur ? ' autoplay' : '') + ' src="/share/' + token + '/file?i=' + gi + '"></video>';
        } else {
          inner = '<div class="lb-other">' + esc(nameOf(gi)) + '<span class="lb-hint">该类型暂不支持在线预览</span></div>';
        }
        s.innerHTML = inner;
        s.setAttribute('data-loaded', '1');
      }
    }
    // 底部缩略图条（Filmstrip）：只渲染筛选后的 view；视频缩略图进入视口才取首帧（懒加载）
    function buildStrip() {
      if (!strip) return;
      var html = '';
      view.forEach(function (gi, k) {
        var cat = catOf(gi), inner;
        if (cat === 'image') {
          inner = '<img loading="lazy" src="/share/' + token + '/file?i=' + gi + '" alt=""/>';
        } else if (cat === 'video') {
          inner = '<video class="vthumb" preload="metadata" muted playsinline data-src="/share/' + token + '/file?i=' + gi + '"></video>';
        } else {
          inner = '<div class="ph">' + (cat === 'text' ? '文本' : cat === 'torrent' ? '种子' : '文件') + '</div>';
        }
        html += '<div class="strip-thumb" data-k="' + k + '">' + inner + '</div>';
      });
      strip.innerHTML = html;
      stripThumbs = strip.querySelectorAll('.strip-thumb');
      observeVideos(strip);
      stripThumbs.forEach(function (t) {
        t.addEventListener('click', function () { show(parseInt(t.getAttribute('data-k'), 10)); });
      });
    }
    // 同步缩略图条：高亮当前项并滚动到可视区（按索引直接定位，避免全量扫描）
    function syncStrip() {
      if (!strip || !stripThumbs.length) return;
      for (var k = 0; k < stripThumbs.length; k++) stripThumbs[k].classList.toggle('active', k === cur);
      var act = stripThumbs[cur];
      if (act) act.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' });
    }
    function slideW() { var first = track.children[0]; return first ? first.getBoundingClientRect().width : window.innerWidth; }
    function setTrack(px) { track.style.transform = 'translate3d(' + px + 'px,0,0)'; }
    function snap() { track.style.transition = 'transform .32s cubic-bezier(.22,.61,.36,1)'; setTrack(-cur * slideW()); }
    function updateBar() {
      document.getElementById('lb-name').textContent = nameOf(gidx(cur));
      document.getElementById('lb-idx').textContent = (cur + 1) + ' / ' + view.length;
      syncStrip();
    }
    // 显式播放当前张视频：首开查看器时当前张已带 autoplay；但从缩略图条跳到「已懒加载的相邻页」时，
    // 该 slide 当初作为相邻页被预建、未带 autoplay 且已 data-loaded 不再重建，故必须在此显式播放。
    function playCurrentVideo() {
      var s = track.children[cur];
      if (!s) return;
      var v = s.querySelector('video.lb-media');
      if (v) { var p = v.play(); if (p && p.catch) p.catch(function () {}); }
    }

    function show(k) {
      cur = (k % view.length + view.length) % view.length;
      zoom = 1; panX = 0; panY = 0;                  // 切换资源时复位缩放
      track.querySelectorAll('img.lb-media').forEach(function (im) { im.style.transform = ''; });
      pauseAllVideos();                          // 切页时先暂停上一视频，避免后台继续发声
      if (!lb.classList.contains('open')) {
        lb.classList.add('open');                      // 先显示再定位，确保 slide 有真实宽度
        buildSlides();                                // 按当前筛选结果重建轨道
        buildStrip();                                 // 按当前筛选结果重建缩略图条
        track.style.transition = 'none';
        setTrack(-cur * slideW());
        void track.offsetWidth;                        // 强制重排，消除首屏跳动
        track.style.transition = '';
      } else {
        snap();
      }
      loadWindow(); updateBar(); showBar(); playCurrentVideo();
    }
    function go(d) { if (cur + d < 0 || cur + d >= view.length) { snap(); return; } show(cur + d); }

    // 网格点击进入查看器：把全局下标映射到当前 view 中的位置
    document.querySelectorAll('.cell').forEach(function (el) {
      el.addEventListener('click', function () {
        var gi = parseInt(el.getAttribute('data-i'), 10);
        var k = view.indexOf(gi);
        if (k >= 0) show(k);
      });
    });

    // 1:1 跟手横滑；双指捏合缩放图片、缩放后单指拖动平移、下拉退出
    var dragging = false, sx = 0, sy = 0, dx = 0, dy = 0, axis = null, pid = null;
    var pointers = {};
    var pinching = false, pinchDist = 0, pinchZoom = 1;
    function curImg() { var s = track.children[cur]; return s ? s.querySelector('img.lb-media') : null; }
    function applyZoom() { var img = curImg(); if (img) img.style.transform = 'translate(' + panX + 'px,' + panY + 'px) scale(' + zoom + ')'; }
    track.addEventListener('pointerdown', function (e) {
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(pointers);
      if (ids.length >= 2) {                      // 双指：进入捏合缩放
        pinching = true; dragging = false;
        var p0 = pointers[ids[0]], p1 = pointers[ids[1]];
        pinchDist = Math.hypot(p0.x - p1.x, p0.y - p1.y) || 1;
        pinchZoom = zoom;
        try { track.setPointerCapture(e.pointerId); } catch (_) {}
        return;
      }
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      dragging = true; sx = e.clientX; sy = e.clientY; dx = 0; dy = 0; axis = null; pid = e.pointerId;
      track.style.transition = 'none';
    });
    track.addEventListener('pointermove', function (e) {
      if (pointers[e.pointerId]) pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      if (pinching) {                             // 捏合缩放图片
        var ids = Object.keys(pointers);
        if (ids.length < 2) return;
        var p0 = pointers[ids[0]], p1 = pointers[ids[1]];
        var dist = Math.hypot(p0.x - p1.x, p0.y - p1.y) || 1;
        zoom = Math.min(5, Math.max(1, pinchZoom * dist / pinchDist));
        applyZoom();
        return;
      }
      if (!dragging) return;
      dx = e.clientX - sx; dy = e.clientY - sy;
      if (axis === null && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
        axis = zoom > 1 ? 'pan' : (Math.abs(dx) > Math.abs(dy) ? 'x' : 'y');   // 已放大则拖动=平移
        if (axis === 'x' || axis === 'pan') { try { track.setPointerCapture(pid); } catch (_) {} }
      }
      if (axis === 'x') setTrack(-cur * slideW() + dx);
      else if (axis === 'pan') { panX = dx; panY = dy; applyZoom(); }
    });
    function endDrag(e) {
      delete pointers[e.pointerId];
      if (pinching) { if (Object.keys(pointers).length < 2) pinching = false; return; }
      if (!dragging) return;
      dragging = false; track.style.transition = '';
      pauseAllVideos();                         // 滑动切换同样先暂停正在播放的视频
      if (axis === 'x') {
        var w = slideW();
        if (dx <= -w * 0.18 && cur < view.length - 1) cur++;
        else if (dx >= w * 0.18 && cur > 0) cur--;
        zoom = 1; panX = 0; panY = 0;           // 翻页后复位缩放
        loadWindow(); updateBar(); snap();
      } else if (axis === 'y') {
        if (dy > window.innerHeight * 0.22) { stopLb(); return; }   // 下滑退出
        snap();
      } else if (axis === 'pan') {
        // 仅平移，不切页
      } else {
        snap();
      }
      axis = null;
    }
    track.addEventListener('pointerup', endDrag);
    track.addEventListener('pointercancel', function (e) {
      delete pointers[e.pointerId];
      if (!dragging) return;
      dragging = false; track.style.transition = ''; snap();
    });

    // 暂停轨道内所有视频，避免切走后仍在后台发声（解决「切到图片后原视频仍出声」）
    function pauseAllVideos() {
      track.querySelectorAll('video').forEach(function (v) { try { v.pause(); } catch (_) {} });
    }
    // 关闭：暂停所有视频即可停止后台发声（不卸载 src，避免重新打开需重载）
    function stopLb() {
      pauseAllVideos();
      lb.classList.remove('open'); hideBar();
    }
    document.getElementById('lb-close').onclick = function () { stopLb(); };

    // 桌面端：滚轮 / 方向键切换，Esc 关闭
    var wheelLock = false;
    // 滚轮：悬停图片=缩放；悬停视频/文本或缩略图条/顶栏=翻页或放行
    lb.addEventListener('wheel', function (e) {
      if (bar.contains(e.target) || strip.contains(e.target)) return;
      if (curImg()) {
        e.preventDefault();
        zoom = Math.min(5, Math.max(1, zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
        if (zoom === 1) { panX = 0; panY = 0; }
        applyZoom();
        return;
      }
      var d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      if (Math.abs(d) < 8) return;
      e.preventDefault();
      if (wheelLock) return;
      wheelLock = true; setTimeout(function () { wheelLock = false; }, 320);
      go(d > 0 ? 1 : -1);
    }, { passive: false });
    // 双击图片：在 1x 与 2.5x 间切换
    track.addEventListener('dblclick', function () {
      var img = curImg(); if (!img) return;
      if (zoom > 1) { zoom = 1; panX = 0; panY = 0; }
      else { zoom = 2.5; panX = 0; panY = 0; }
      applyZoom();
    });
    document.addEventListener('keydown', function (e) {
      if (!lb.classList.contains('open')) return;
      if (e.key === 'Escape') stopLb();
      else if (e.key === 'ArrowLeft') go(-1);
      else if (e.key === 'ArrowRight') go(1);
    });

    // 沉浸式顶栏自动隐藏，交互（移动/点击）时浮现；点空白处亦可唤出关闭按钮
    var barTimer = null;
    var controls = [bar, strip].filter(Boolean);
    function showBar() {
      controls.forEach(function (c) { c.style.opacity = '1'; c.style.pointerEvents = 'auto'; });
      if (barTimer) clearTimeout(barTimer);
      barTimer = setTimeout(hideBar, 2600);
    }
    function hideBar() { controls.forEach(function (c) { c.style.opacity = '0'; c.style.pointerEvents = 'none'; }); }
    lb.addEventListener('pointermove', showBar);
    lb.addEventListener('click', function () { showBar(); });

    // 视频缩略图首帧：懒加载 + 取首帧画到 canvas（跨端一致）。
    // 业界做法（YouTube/Netflix/Vimeo 均为服务端抽帧做 poster）：绝不渲染「无 poster 的裸 video」（即黑屏）；
    // 且首帧常为黑场（片头淡入），故取 5% 处代表帧。移动端/iOS 最可靠的做法是「静音 inline 播放 → playing
    // 事件保证解码器已吐出真实帧 → 取帧后暂停替换」；单纯 seek 在 iOS 上 seeked 不触发、drawImage 画到黑帧。
    function hydrateVideo(v) {
      if (!v || v.dataset.hydrated) return;
      v.dataset.hydrated = '1';
      v.preload = 'metadata';                        // 让浏览器愿意加载元数据（preload=none 时一直黑屏）
      v.muted = true;                               // 移动端/iOS：必须静音+inline 才能免手势播放取帧
      v.playsInline = true;
      if (v.dataset.src && !v.src) v.src = v.dataset.src;
      var done = false;
      function drawNow() {
        if (done) return true;
        if (!v.videoWidth || v.readyState < 2) return false;  // 还没真实解码帧，先放一放
        done = true;
        try {
          var c = document.createElement('canvas');
          c.width = v.videoWidth; c.height = v.videoHeight;
          c.getContext('2d').drawImage(v, 0, 0);
          var url = c.toDataURL('image/jpeg', 0.8);  // jpeg 体积更小
          var img = new Image();
          img.className = v.className; img.src = url; img.alt = '';
          if (v.parentNode) v.parentNode.replaceChild(img, v);
          return true;
        } catch (_) { return false; }
      }
      // 主路径（移动端/iOS 最可靠）：静音播放，playing 事件保证已有真实解码帧，取帧后暂停并替换，绝不黑屏
      v.addEventListener('playing', function () {
        var n = 0;
        var tick = function () {
          if (drawNow()) { try { v.pause(); } catch (_) {} return; }   // 取到帧即暂停，避免缩略图持续播放
          if (++n < 4) setTimeout(tick, 100);                          // 未就绪再重试几次
        };
        setTimeout(tick, 60);
      });
      function playForFrame() {                        // 兜底：桌面端 seek 失败（移动端常见）时改用「播放取帧」
        try {
          var t = 0;
          try { if (v.duration && isFinite(v.duration)) t = Math.min(Math.max(v.duration * 0.05, 0.1), Math.max(0, v.duration - 0.05)); } catch (_) {}
          try { v.currentTime = t; } catch (_) {}
          var p = v.play();
          if (p && p.catch) p.catch(function () {});
        } catch (_) {}
      }
      // 桌面端 seek 路径（无需真正播放）：seek 到 5% 取代表帧
      function onMeta() {
        var t = 0;
        try { if (v.duration && isFinite(v.duration)) t = Math.min(Math.max(v.duration * 0.05, 0.1), Math.max(0, v.duration - 0.05)); } catch (_) {}
        try { v.currentTime = t; } catch (_) {}
        if ('requestVideoFrameCallback' in v) {       // 现代浏览器：seek 到目标帧后由浏览器回调，保证拿到真实解码帧
          try { v.requestVideoFrameCallback(function () { drawNow(); }); } catch (_) {}
        }
        setTimeout(function () { drawNow(); }, 700);  // 兜底：seeked/rVFC 均未触发时也能取帧
      }
      v.addEventListener('loadedmetadata', onMeta, { once: true });
      v.addEventListener('seeked', function () { setTimeout(drawNow, 0); }, { once: true });
      if (v.readyState >= 1) onMeta();
      setTimeout(function () { if (!done) playForFrame(); }, 400);   // 保底：未取到帧则改用播放取帧（移动端必走此路）
      setTimeout(function () { if (!done) playForFrame(); }, 1500);  // 二次保底：首次播放被拦截时再试
    }
    var ioV = ('IntersectionObserver' in window)
      ? new IntersectionObserver(function (entries) {
          entries.forEach(function (e) { if (e.isIntersecting) { hydrateVideo(e.target); ioV.unobserve(e.target); } });
        }, { root: null, rootMargin: '200px' })
      : null;
    function observeVideos(rootEl) {
      if (!rootEl) return;
      rootEl.querySelectorAll('.vthumb').forEach(function (v) { if (ioV) ioV.observe(v); else hydrateVideo(v); });
    }

    // ---------- 类型筛选 + 名称搜索 + 前端分页（与「浏览」列表同思路）----------
    var cells = Array.prototype.slice.call(document.querySelectorAll('.cell'));
    var fCat = 'all', fKw = '', fPage = 1;
    applyFilter();                 // 先算出 view（筛选后的全局下标）
    buildSlides();                 // 按 view 构建轨道占位
    buildStrip();                  // 按 view 构建缩略图条
    observeVideos(document.querySelector('.grid'));
    function renderPager(n, pages) {
      var p = document.getElementById('lb-pager');
      if (n === 0) { p.innerHTML = '<span class="info">无匹配文件</span>'; return; }
      var html = '<button data-pg="prev"' + (fPage <= 1 ? ' disabled' : '') + '>上一页</button>';
      var nums = [];
      if (pages <= 11) {
        for (var i = 1; i <= pages; i++) nums.push(i);
      } else {
        nums.push(1);
        var s = Math.max(2, fPage - 2), e = Math.min(pages - 1, fPage + 2);
        if (s > 2) nums.push('…');
        for (var j = s; j <= e; j++) nums.push(j);
        if (e < pages - 1) nums.push('…');
        nums.push(pages);
      }
      nums.forEach(function (i) {
        if (i === '…') { html += '<span class="info">…</span>'; return; }
        html += '<button data-pg="' + i + '"' + (i === fPage ? ' class="active"' : '') + '>' + i + '</button>';
      });
      html += '<button data-pg="next"' + (fPage >= pages ? ' disabled' : '') + '>下一页</button>';
      html += '<span class="info">共 ' + n + ' 个 · 第 ' + fPage + '/' + pages + ' 页</span>';
      p.innerHTML = html;
      p.querySelectorAll('button[data-pg]').forEach(function (b) {
        b.addEventListener('click', function () {
          var v = b.getAttribute('data-pg');
          if (v === 'prev') fPage = Math.max(1, fPage - 1);
          else if (v === 'next') fPage = Math.min(pages, fPage + 1);
          else fPage = parseInt(v, 10);
          applyFilter();
          window.scrollTo({ top: 0, behavior: 'smooth' });
        });
      });
    }
    function applyFilter() {
      var kw = fKw.trim().toLowerCase();
      var matched = cells.filter(function (el) {
        var okCat = fCat === 'all' || el.getAttribute('data-cat') === fCat;
        var okKw = !kw || (el.getAttribute('data-name') || '').toLowerCase().indexOf(kw) !== -1;
        return okCat && okKw;
      });
      var pages = Math.max(1, Math.ceil(matched.length / PAGE));
      if (fPage > pages) fPage = pages;
      var start = (fPage - 1) * PAGE;
      cells.forEach(function (el) { el.style.display = 'none'; });
      matched.slice(start, start + PAGE).forEach(function (el) { el.style.display = ''; });
      renderPager(matched.length, pages);
      // 查看器与缩略图条只覆盖筛选后的集合（位置→全局下标映射）
      view = matched.map(function (el) { return parseInt(el.getAttribute('data-i'), 10); });
      var meta = document.getElementById('lb-meta');
      var txt = '共 ' + matched.length + ' 个文件';
      if (fCat !== 'all' || kw) txt += '（已筛选，原 ' + absTotal + '）';
      txt += ' · ' + sizeText + ' · 有效期至 __EXP__';
      meta.textContent = txt;
    }
    document.getElementById('lb-filters').querySelectorAll('.chip').forEach(function (c) {
      c.addEventListener('click', function () {
        document.getElementById('lb-filters').querySelectorAll('.chip').forEach(function (x) { x.classList.remove('active'); });
        c.classList.add('active');
        fCat = c.getAttribute('data-cat'); fPage = 1; applyFilter();
      });
    });
    document.getElementById('lb-search').addEventListener('input', function (e) {
      fKw = e.target.value; fPage = 1; applyFilter();
    });
    applyFilter();
  </script>
</body>
</html>"""


def _render_gallery(title: str, metas: list[dict], token: str, expire_at: float) -> str:
    """画廊式预览页：目录名 + 文件网格（图片/视频内联预览）+ 沉浸式 1:1 滑动查看 + 底部缩略图条跳转，不含下载按钮。"""
    esc_title = html.escape(title)
    total = len(metas)
    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(expire_at))
    size_text = _human_size(sum(m["size"] for m in metas))
    cards = []
    for i, m in enumerate(metas):
        cat = m["category"]
        esc_name = html.escape(m["name"])
        size_s = _human_size(m["size"])
        if cat == "image":
            thumb = f'<img class="thumb" loading="lazy" src="/share/{token}/file?i={i}" alt="{esc_name}"/>'
        elif cat == "video":
            thumb = f'<video class="thumb vthumb" preload="metadata" muted playsinline data-src="/share/{token}/file?i={i}"></video>'
        else:
            thumb = f'<div class="thumb ph">{CATEGORY_CN.get(cat, "文件")}</div>'
        cards.append(
            f'<div class="cell" data-i="{i}" data-cat="{cat}" data-name="{esc_name}">'
            f'<div class="thumb-wrap">{thumb}</div>'
            f'<div class="cname" title="{esc_name}">{esc_name}</div>'
            f'<div class="csize">{size_s}</div>'
            f"</div>"
        )
    grid = "\n".join(cards)
    return (
        _GALLERY_TPL.replace("__TITLE__", esc_title)
        .replace("__TOKEN__", token)
        .replace("__TOTAL__", str(total))
        .replace("__EXP__", exp)
        .replace("__SIZE__", size_text)
        .replace("__GRID__", grid)
    )


# ================= 独立分享服务（不挂载 SPA） =================
share_app = FastAPI(title="文件分享")


@share_app.get("/health")
def health():
    return {"ok": True, "service": "share"}


def _resolve_or_410(token: str):
    r = resolve_token(token)
    if r is None:
        raise HTTPException(status_code=410, detail="分享链接已过期或不存在")
    return r  # (meta, metas)


@share_app.get("/share/{token}", response_class=HTMLResponse)
def preview(token: str):
    _meta, metas = _resolve_or_410(token)
    title = _common_dir([m["rel"] for m in metas])
    # no-store：分享页是动态模板，禁止浏览器缓存，确保代码更新后立即生效（避免旧版箭头重叠等问题滞留）
    return HTMLResponse(_render_gallery(title, metas, token, _meta["expire_at"]),
                        headers={"Cache-Control": "no-store"})


@share_app.get("/share/{token}/file")
def inline_file(token: str, i: int = 0):
    _meta, metas = _resolve_or_410(token)
    if i < 0 or i >= len(metas):
        raise HTTPException(status_code=404, detail="文件序号越界")
    m = metas[i]
    target = resources.resolve_safe(m["rel"])
    if target is None:
        raise HTTPException(status_code=410, detail="文件已删除")
    return FileResponse(target, filename=m["name"], content_disposition_type="inline")
