"""本地镜像（web.exe @ 127.0.0.1:1024）的同源中继 —— 让手机等设备也能打开站点页面。

为什么必须有这一层（2026-09-17 实测）：web.exe 只监听回环地址
（`netstat` 为 `TCP 127.0.0.1:1024 LISTENING`，其安装目录里也没有可改监听地址的配置文件），
所以「手机访问 http://<桌面IP>:1024/...」在任何配置下都不可能成功；而看板进程本身在
局域网可达（TXXY_WEB_HOST=0.0.0.0）。于是在看板端口上加一条同源中继：

    手机 → http://<桌面IP>:8088/mirror/htm_data/xxx.html → 转发 → 127.0.0.1:1024/htm_data/xxx.html

行为约定（三条）：

1. 上游恒为 txxy_env.LOCAL_PROXY（唯一配置源），**不接受调用方传入地址**——否则本路由
   就成了任意转发器（SSRF 入口）。
2. 镜像连不上 / 返回 5xx（也就是「1024 访问不了」）→ 302 到业务域名同一路径；本环境未
   配置本地镜像（Docker / 离线 Linux）时同样直接 302。这就是需求里的「降级用
   PUBLIC_DOMAIN 打开」。4xx 不透传降级：那是镜像给出的真实答案（如页面确实不存在）。
3. 只对 HTML 做两处必要重写，其余一律原样透传（含 Range，视频拖进度条靠它）：
   - 根相对属性 `href/src/action="/x"` → `/mirror/x`。镜像帖子页实测有 5 处这类导航
     （/profile.php、/message.php、/search.php、/notice.php），不重写会被看板 SPA 的
     兜底路由吞掉（返回 index.html），表现为「点了跳到看板首页」；
   - 3xx 的 Location 指回中继前缀，否则浏览器会去访问它自己打不开的 127.0.0.1:1024。

不加限流：一次页面加载会并发十几次子资源请求，按 (client_ip, 路径) 计数的固定窗口限流
必然误伤（与图片预览当初踩的坑同源）；本中继只读、且仅本机/局域网可达（单人自用、
无鉴权是既有前提），暴露面与看板其余接口一致。
"""
from collections.abc import Iterator
import re
import sys

import requests
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse

import config

# 项目根加入 sys.path：复用全项目唯一 UA（http_headers），不在本模块另写一份请求头
if str(config.BASE_DIR) not in sys.path:
    sys.path.insert(0, str(config.BASE_DIR))

import http_headers  # noqa: E402

router = APIRouter()

# 上游响应头透传白名单：只转发与「内容语义」相关的头；逐跳头（Connection / Keep-Alive /
# Transfer-Encoding）与安全类头一律不透传，由看板自身负责。
_PASS_RESP_HEADERS = frozenset({
    "content-type", "content-length", "content-range", "accept-ranges",
    "last-modified", "etag", "content-encoding", "cache-control", "expires",
    "content-disposition", "vary",
})
# 请求头透传：Range 保留视频拖动；条件请求保留 304 缓存；Cookie 保留桌面端在镜像上的登录态
# （Cookie 按 host 归属、与端口无关，故浏览器访问 8088 时同样会带上 127.0.0.1 的镜像 cookie）
_PASS_REQ_HEADERS = (
    ("range", "Range"),
    ("if-none-match", "If-None-Match"),
    ("if-modified-since", "If-Modified-Since"),
    ("cookie", "Cookie"),
)
# 根相对属性：`href="/x"` 命中，`href="//host/x"`（协议相对）与 `href="#"` 不命中
_ROOT_ATTR_RE = re.compile(r"""(\b(?:href|src|action)\s*=\s*["'])/(?!/)""", re.I)
_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.I)


def _fallback_url(path: str, query: str) -> str:
    """降级目标：业务域名 + 同一路径（查询串原样带上，避免二次编码）"""
    url = f"{config.PUBLIC_DOMAIN}/{path.lstrip('/')}"
    return f"{url}?{query}" if query else url


def _upstream_headers(request: Request, upstream: str) -> dict[str, str]:
    """构造发往镜像的请求头。

    UA 恒用全项目唯一 UA（不透传浏览器 UA，保持「全项目一份 UA」的既有约定）；
    Accept 透传浏览器原值——图片 / HTML / 视频各不相同，透传最贴近直连镜像的行为。
    """
    accept = request.headers.get("accept") or http_headers.ACCEPT_HTML
    headers = http_headers.build_headers(accept)
    for src, dst in _PASS_REQ_HEADERS:
        value = request.headers.get(src)
        if value:
            headers[dst] = value
    # 同源来源：镜像页面的子资源请求若校验防盗链，直连时 Referer 也是镜像自身
    headers["Referer"] = upstream + "/"
    return headers


def _rewrite_location(location: str, upstream: str) -> str:
    """镜像返回的 Location → 中继前缀（相对 Location 与站外跳转原样返回）"""
    if location.startswith(upstream):
        return config.MIRROR_PREFIX + location[len(upstream):]
    return location


def _rewrite_html(text: str) -> str:
    """HTML 重写：根相对属性指回中继前缀 + 追加一段点击时兜底的链接修正脚本。

    两处都要，缺一不可（2026-09-17 实测）：
    - 属性重写：镜像返回的静态 HTML 里有 5~7 处根相对链接（/profile.php 等导航），
      不重写会被看板 SPA 的兜底路由吞掉（返回 index.html）；
    - 脚本兜底：页面自带的 JS 会**运行时生成**根相对链接（实测相关帖 a[href="/htm_data/..."],
      服务端重写看不到它们），点击瞬间改写到中继前缀，避免用户点了落到看板首页。
      该脚本还顺带覆盖将来新增的同类链接，比逐个路径打补丁可靠。
    注：本模块不透传上游的 CSP 等安全头（见 _PASS_RESP_HEADERS），故内联脚本可执行。
    """
    out = _ROOT_ATTR_RE.sub(lambda m: f"{m.group(1)}{config.MIRROR_PREFIX}/", text)
    script = (
        "<script>(function(){document.addEventListener('click',function(e){"
        "var t=e.target;var a=t&&t.closest?t.closest('a[href]'):null;if(!a)return;"
        "var h=a.getAttribute('href')||'';"
        "if(h.charAt(0)!=='/'||h.charAt(1)==='/')return;"
        "if(h.indexOf('" + config.MIRROR_PREFIX + "/')===0)return;"
        "a.setAttribute('href','" + config.MIRROR_PREFIX + "'+h);},true);})();</script>"
    )
    if re.search(r"</body>", out, re.I):
        return re.sub(r"</body>", script + "</body>", out, count=1, flags=re.I)
    return out + script


def _iter_raw(up: requests.Response, chunk: int = 64 * 1024) -> Iterator[bytes]:
    """逐块产出**原始**字节（不自动解压）。

    必须绕开 iter_content：它默认 decode_content=True 会解压响应体，而我们要透传上游的
    Content-Encoding / Content-Length（大文件与 Range 依赖它们准确），解压后再带上原头会
    让浏览器解析失败。requests 以 decode_content=False 打开连接，故 raw.read 即原始字节。
    """
    try:
        while True:
            piece = up.raw.read(chunk) if up.raw is not None else b""
            if not piece:
                break
            yield piece
    finally:
        up.close()


@router.api_route(
    config.MIRROR_PREFIX + "/{path:path}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
def mirror(path: str, request: Request) -> Response:
    """转发到本地镜像；镜像访问不了时降级到业务域名同一路径。"""
    query = request.url.query
    upstream = config.MIRROR_UPSTREAM.rstrip("/")
    if not upstream:  # 本环境没有本地镜像（Docker / 离线 Linux）：直接交给业务域名
        return RedirectResponse(_fallback_url(path, query), status_code=302)

    target = f"{upstream}/{path}"
    if query:
        target = f"{target}?{query}"
    # 上游对 HEAD 不支持（实测 web.exe 对 HEAD 返回 404）：统一用 GET 取头，
    # 下面按 HEAD 语义「只回头、不读体」，既避免 404 又不多耗带宽
    upstream_method = "GET" if request.method == "HEAD" else request.method
    try:
        up = requests.request(
            upstream_method,
            target,
            headers=_upstream_headers(request, upstream),
            stream=True,
            timeout=(3, 30),      # 连接 3s：本机回环服务，连不上就是没在跑
            allow_redirects=False,  # 自行处理 Location 重写
        )
    except requests.RequestException:
        # web.exe 未运行 / 已退出：这就是「1024 访问不了」，降级公开域名
        return RedirectResponse(_fallback_url(path, query), status_code=302)

    status = up.status_code
    ctype = up.headers.get("content-type", "")
    location = up.headers.get("location", "")

    if 300 <= status < 400 and location:
        up.close()
        return RedirectResponse(_rewrite_location(location, upstream), status_code=status)
    if status >= 500:
        # 镜像坏了（而非「这个路径不存在」）：同样降级，比把 500 透给用户更有用
        up.close()
        return RedirectResponse(_fallback_url(path, query), status_code=302)

    passthrough = {k: v for k, v in up.headers.items() if k.lower() in _PASS_RESP_HEADERS}
    set_cookies = up.raw.headers.getlist("set-cookie") if up.raw is not None else []

    if request.method == "HEAD":
        up.close()  # 只回头：上游是按 GET 取的，这里不读响应体
        resp: Response = Response(status_code=status, headers=passthrough)
    elif "text/html" in ctype.lower():
        body = up.content  # 缓冲（自动解压）后重写；随后 content-length / content-encoding 失效
        up.close()
        charset = _CHARSET_RE.search(ctype)
        text = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
        out = _rewrite_html(text).encode("utf-8")
        heads = {k: v for k, v in passthrough.items()
                 if k.lower() not in ("content-length", "content-encoding", "content-type")}
        resp = Response(content=out, status_code=status, headers=heads,
                        media_type=ctype or "text/html; charset=utf-8")
    else:
        resp = StreamingResponse(_iter_raw(up), status_code=status, headers=passthrough)

    for cookie in set_cookies:
        resp.headers.append("set-cookie", cookie)
    return resp
