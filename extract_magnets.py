"""
磁力链接（magnet）相关模块：磁力地址提取 + 磁力清单 TXT 导出（提取 / 写入全部集中于此）

由 download_files.py 调用，保持下载主流程只关注页面访问与编排：
    from extract_magnets import (
        extract_magnet_links,
        save_magnets_txt,
    )

提取逻辑：正则匹配 HTML 中所有 magnet: 链接地址（还原 &amp; HTML 实体），
按完整链接去重保序，TXT 中只保留 magnet: 开头的地址信息，每行一条。
"""
import os
import re

# ============ 磁力链接识别 ============
# 匹配 HTML 中任意 magnet: 链接地址（到空白 / 引号 / 尖括号 / 标签边界为止）
_MAGNET_RE = re.compile(r"magnet:[^\s\"'<>]+", re.IGNORECASE)
# TXT 文件名（输出到页面标题目录下）
MAGNETS_FILENAME = "magnets.txt"


def extract_magnet_links(html: str) -> list[str]:
    """
    匹配 HTML 中所有 magnet: 链接地址，按完整链接去重保序。
    返回 [磁力链接, ...]；&amp; 等 HTML 实体已还原为 &。
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _MAGNET_RE.finditer(html or ""):
        magnet = m.group(0)
        # 去除尾部标点（半角 / 全角，部分页面磁力链接后紧跟标点）
        magnet = magnet.rstrip(".,;:!?)]}。，；：！？、")
        # HTML 实体 &amp; 还原为 &（部分页面磁力链接的 & 被转义）
        magnet = magnet.replace("&amp;", "&")
        if magnet not in seen:
            seen.add(magnet)
            found.append(magnet)
    return found


def save_magnets_txt(
    magnets: list[str],
    save_dir: str,
    filename: str = MAGNETS_FILENAME,
) -> str | None:
    """
    将磁力清单写入 <save_dir>/<filename>，返回文件路径或 None。
    内容只保留 magnet: 开头的地址信息，每行一条，无其它内容；
    UTF-8（含 BOM）编码，方便记事本直接查看。
    """
    if not magnets:
        return None
    try:
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, filename)
        with open(path, "w", encoding="utf-8-sig") as f:
            _ = f.write("\n".join(magnets) + "\n")
        print(f"已保存: {path}（{len(magnets)} 条磁力链接）")
        return path
    except Exception as e:
        print(f"  [错误] 保存磁力清单失败: {e}")
        return None
