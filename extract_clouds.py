"""
云盘链接相关模块：网盘（夸克 / 百度 / 迅雷 / UC 等）链接提取 + 云盘清单 TXT 导出（提取 / 写入全部集中于此）

由 download_files.py 调用，保持下载主流程只关注页面访问与编排：
    from extract_clouds import (
        extract_cloud_links,
        save_clouds_txt,
    )

提取逻辑：匹配 HTML 中所有 /2023.redircdn.com/? 中转的网盘链接，还原真实地址：
  - 去掉链接中的 /2023.redircdn.com/? 前缀
  - 将 ______ 替换为 .（域名分隔符，如 pan______quark______cn → pan.quark.cn）
  - 还原 &amp; HTML 实体为 &
  - 过滤含 action=image&url= 的图片中转占位链接（如整页图床中转页）
按完整链接去重保序，TXT 中每行一条。
"""
import re

from txt_export import save_lines_txt

# ============ 云盘链接识别 ============
# 匹配 /2023.redircdn.com/? 中转的网盘链接（到空白 / 引号 / 尖括号 / 标签边界为止）
_CLOUD_RELAY_RE = re.compile(r"/2023\.redircdn\.com/\?[^\s\"'<>]+", re.IGNORECASE)
# 中转前缀（提取时剥离）
_CLOUD_RELAY_PREFIX = "/2023.redircdn.com/?"
# TXT 文件名（输出到页面标题目录下）
CLOUDS_FILENAME = "clouds.txt"


def extract_cloud_links(html: str) -> list[str]:
    """
    匹配 HTML 中所有 redircdn 中转的网盘链接，还原真实地址，去重保序。
    还原规则：去掉 /2023.redircdn.com/? 前缀；______ → .；&amp; → &。
    返回 [网盘链接, ...]。
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _CLOUD_RELAY_RE.finditer(html or ""):
        raw = m.group(0)
        # 1. 去掉中转前缀 /2023.redircdn.com/?
        link = raw[len(_CLOUD_RELAY_PREFIX):]
        # 2. 域名分隔符 ______ → .（pan______quark______cn → pan.quark.cn）
        link = link.replace("______", ".")
        # 去除尾部标点（部分页面链接后紧跟标点）
        link = link.rstrip(".,;:!?)]}。，；：！？、")
        # 3. HTML 实体 &amp; 还原为 &（部分页面链接的 & 被转义）
        link = link.replace("&amp;", "&")
        # 4. 过滤 action=image&url= 的图片中转占位链接（非真实网盘地址），不计入输出
        if "action=image&url=" in link:
            continue
        if link and link not in seen:
            seen.add(link)
            found.append(link)
    return found


def save_clouds_txt(
    links: list[str],
    save_dir: str,
    filename: str = CLOUDS_FILENAME,
) -> str | None:
    """
    将云盘链接清单写入 <save_dir>/<filename>，返回文件路径或 None。
    内容为还原后的网盘地址，每行一条，无其它内容；
    UTF-8（含 BOM）编码，方便记事本直接查看。
    """
    return save_lines_txt(links, save_dir, filename, "云盘链接")
