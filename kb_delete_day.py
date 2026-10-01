"""kb_delete_day.py — 按发布日回滚知识库沉淀批次（一次性维护工具）。

用途：把某一天（posts.date = 发布日）已沉淀的批次产物从 vault 与 kb_state 中
干净移除，使这些帖恢复到「从未处理」状态（重跑批次会重新抓取与判定）。

清理范围（与 kb_export.py 的产物一一对应）：
- vault/raw/<版块>/<文件>.html + vault/wiki/sources/<版块>/<文件>.md；
- progress.json：done / failed / permanent 中该日条目；
- meta.json：registry 标题映射 + gen_hash 笔记/聚合页 hash；
- concept_index.json：forward / entity_forward 正向段整条移除，
  reverse / entity_reverse 反向段按帖摘除，引用清空后连页删除；
- magnet_index.json：该日帖的磁力引用摘除，空组删除；
- llm_todo.json：该日待办摘除；
- authors / sections 确定性实体页：涉及到的直接删页（全库口径可再生，
  下一批 touched 时自动重建，避免残留指向已删笔记的悬空链接）。

不清理（显式留痕）：其他日期笔记里指向被删笔记的 See also 互链会成为
Obsidian 未解析链接，属预期现象，不做跨帖改写。

用法：
  python kb_delete_day.py --dry-run                 # 预览今天要删什么，不写盘
  python kb_delete_day.py --date 2026-10-01         # 删除指定发布日
  python kb_delete_day.py --date 2026-10-01 --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---- 路径自举（与 kb_export.py 同口径：项目根 + web/）----
BASE_DIR = Path(__file__).resolve().parent
for _p in (str(BASE_DIR), str(BASE_DIR / "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import kb_export as kb  # noqa: E402  复用状态读写 / 批次锁 / 读库 / _log 唯一实现
import txxy_env  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台中文输出：显式 UTF-8（与 kb_export.main 同口径，原 69）
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(description="按发布日回滚知识库沉淀批次（维护工具）")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="目标发布日 YYYY-MM-DD（默认今天，绑定 posts.date）")
    parser.add_argument("--vault", default=str(kb.vault_root()), help="vault 根目录（默认 outputs/vault/）")
    parser.add_argument("--dry-run", action="store_true", help="只输出将删除的清单与统计，不写盘")
    args = parser.parse_args(argv)

    # 仅 CLI 入口启用统一日志：file_logger 双写控制台 + outputs/<日期>/kb_delete_<日期>.log
    #（本工具为纯 CLI、无模块导入复用场景，无劫持 web 进程 stdout 的顾虑）
    import file_logger  # noqa: PLC0415

    _ = file_logger.setup("kb_delete")

    kb.set_vault_root(Path(args.vault))
    target_date = args.date

    # ---- 批次互斥：与真实批次共用同一把文件锁，防止边删边沉淀 ----
    lock = kb.BatchLock(kb.KB_STATE_ROOT / "kb_batch.lock")
    if not lock.acquire():
        kb._log("另一知识库批次正在执行，拒绝回滚（稍后再试）")
        return 2

    try:
        progress = kb.load_progress()
        meta = kb.load_meta()
        try:
            concept_index = kb.load_concept_index()
        except RuntimeError as e:
            kb._log(str(e))
            return 2
        magnet_index = kb.load_magnet_index()

        # ---- 定位目标帖：库内按发布日取行（库不删行，rowid 去重口径与批次入口一致）----
        conn = kb._open_db()
        rows_by_title = {r["title"]: r for r in kb.load_all_rows(conn)}
        conn.close()

        involved = {
            t
            for t in (*progress["done"].keys(), *progress["failed"].keys(), *progress["permanent"])
            if t in rows_by_title and rows_by_title[t]["date"] == target_date
        }
        if not involved:
            kb._log(f"{target_date} 无任何断点记录（done/failed/permanent 均无），无需回滚")
            return 0

        summary: dict[str, int] = {
            "notes_deleted": 0, "raw_deleted": 0, "registry_removed": 0,
            "done_removed": 0, "failed_removed": 0, "permanent_removed": 0,
            "concept_pages_deleted": 0, "entity_pages_deleted": 0,
            "entity_hubs_deleted": 0, "magnet_refs_removed": 0,
        }
        touched_fids: set[str] = set()
        touched_authors: set[str] = set()

        def _unlink(path: Path, counter: str) -> None:
            """删文件并计数；目标不存在时静默（状态清理照做）。"""
            if path.exists():
                if not args.dry_run:
                    path.unlink()
                summary[counter] += 1

        for idx, title in enumerate(sorted(involved)):
            row = rows_by_title[title]
            fid_name = txxy_env.fid_name(row["fid"])
            fname = meta["registry"].get(title)
            stem = Path(fname).stem if fname else None
            # 逐帖进度（与 kb_export 同口径，_log 双写控制台与日志文件）：
            # 删除动辄数百帖，「在跑还是挂了」必须有现场判据，不再攒到循环末统一打印
            kb._log(
                f"({idx + 1}/{len(involved)}) {'待删除' if args.dry_run else '已删除'}："
                f"[{row['date']}] {title[:50]}（fid={row['fid']}，registry={'有' if fname else '无'}）"
            )

            # 1) 笔记 + 原始件 + registry + gen_hash（笔记 rel 键）
            if fname and stem:
                _unlink(kb.vault_root() / "wiki" / "sources" / fid_name / (stem + ".md"), "notes_deleted")
                _unlink(kb.vault_root() / "raw" / fid_name / fname, "raw_deleted")
                meta["registry"].pop(title, None)
                meta["gen_hash"].pop(kb.note_relpath(row["fid"], stem + ".md"), None)
                summary["registry_removed"] += 1
            # 2) 断点三段
            if progress["done"].pop(title, None) is not None:
                summary["done_removed"] += 1
            if progress["failed"].pop(title, None) is not None:
                summary["failed_removed"] += 1
            if title in progress["permanent"]:
                progress["permanent"].remove(title)
                summary["permanent_removed"] += 1
            # 3) 概念 / 实体：正向段整条移除，反向段按帖摘除，清空后连页删除
            for c in concept_index["forward"].pop(title, []):
                entry = concept_index["reverse"].get(c)
                if not entry:
                    continue
                if title in entry["titles"]:
                    entry["titles"].remove(title)
                entry["summaries"].pop(title, None)
                if not entry["titles"]:
                    if entry.get("file"):
                        _unlink(kb.vault_root() / "wiki" / "concepts" / entry["file"], "concept_pages_deleted")
                        meta["gen_hash"].pop(f"wiki/concepts/{entry['file']}", None)
                        meta["registry"].pop("concept:" + c + kb.PROMPT_VERSION, None)
                    concept_index["reverse"].pop(c, None)
            for e in concept_index["entity_forward"].pop(title, []):
                name = e["name"]
                entry = concept_index["entity_reverse"].get(name)
                if not entry:
                    continue
                if title in entry["titles"]:
                    entry["titles"].remove(title)
                entry["summaries"].pop(title, None)
                if not entry["titles"]:
                    if entry.get("file"):
                        _unlink(kb.vault_root() / "wiki" / "entities" / "derived" / entry["file"], "entity_pages_deleted")
                        meta["gen_hash"].pop(f"wiki/entities/derived/{entry['file']}", None)
                        meta["registry"].pop("entity:" + name + kb.PROMPT_VERSION, None)
                    concept_index["entity_reverse"].pop(name, None)
            # 4) 磁力索引按帖摘除，空组删除
            for h in list(magnet_index.keys()):
                group = magnet_index[h]
                keep = [g for g in group if g["title"] != title]
                if len(keep) != len(group):
                    summary["magnet_refs_removed"] += len(group) - len(keep)
                    if keep:
                        magnet_index[h] = keep
                    else:
                        magnet_index.pop(h, None)
            # 5) 确定性实体页（全库口径可再生）：直接删页，gen_hash 一并摘除，
            #    下批 touched 时按新全库口径重建，不留悬空链接
            if row["author"]:
                touched_authors.add(row["author"])
                _unlink(kb.vault_root() / "wiki" / "entities" / "authors" / f"{row['author']}.md", "entity_hubs_deleted")
                meta["gen_hash"].pop(f"wiki/entities/authors/{row['author']}.md", None)
            touched_fids.add(row["fid"])
            _unlink(kb.vault_root() / "wiki" / "entities" / "sections" / f"{fid_name}.md", "entity_hubs_deleted")
            meta["gen_hash"].pop(f"wiki/entities/sections/{fid_name}.md", None)

        # 6) llm 段待办摘除
        todo = kb.load_llm_todo()
        todo = [t for t in todo if t not in involved]
        if not args.dry_run:
            kb._save_todo(todo)
            kb._save_meta(meta)
            kb._save_magnets(magnet_index)
            kb._save_concepts(concept_index)
            kb._save_progress(progress)

        kb._log(f"目标发布日：{target_date}，涉及 {len(involved)} 帖" + ("（dry-run，未写盘）" if args.dry_run else ""))
        kb._log("；".join(f"{k}={v}" for k, v in summary.items()))
        if not args.dry_run:
            kb._log("回滚完成。这些帖已恢复「从未处理」状态：重跑批次 / 定时增量会重新沉淀。")
            kb._log("注意：其他日期笔记中指向被删笔记的 See also 互链成为未解析链接，属预期。")
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
