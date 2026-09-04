"""TXT 清单导出（磁力 / 云盘等共用，零依赖）。

历史问题：extract_magnets.save_magnets_txt 与 extract_clouds.save_clouds_txt
函数体逐行相同，仅文件名常量与「磁力链接 / 云盘链接」的提示文案不同——
改编码、改目录创建逻辑或改异常处理，都要动两处且容易漏。

收敛为 save_lines_txt()：各模块只传自己的文件名与日志标签，对外仍保留
save_magnets_txt / save_clouds_txt 的原有签名，调用方无需改动。
"""
import os


def save_lines_txt(
    lines: list[str],
    save_dir: str,
    filename: str,
    label: str,
) -> str | None:
    """把字符串列表写入 <save_dir>/<filename>，每行一条，返回文件路径或 None。

    - lines 为空则不创建文件，直接返回 None（避免产出空清单）
    - UTF-8（含 BOM）编码，方便记事本直接查看
    - label 用于日志文案（如「磁力链接」「云盘链接」）
    - 写盘失败只打印错误并返回 None，不向上抛（清单是附带产物，不应中断下载主流程）
    """
    if not lines:
        return None
    try:
        os.makedirs(save_dir, exist_ok=True)
        path = os.path.join(save_dir, filename)
        with open(path, "w", encoding="utf-8-sig") as f:
            _ = f.write("\n".join(lines) + "\n")
        print(f"已保存: {path}（{len(lines)} 条{label}）")
        return path
    except Exception as e:
        print(f"  [错误] 保存{label}清单失败: {e}")
        return None
