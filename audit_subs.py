#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材对齐审计 —— 把源站每一节课次与本地文件逐条对照。**只读，不触发任何导出。**

用途：回答「素材都齐了吗」这类问题。
`--dry-run` 只告诉你「还差几份要下载」，看不出**本地没有、源站也没有**的课次；
本脚本把源站的**全部**课次（含未结课的未来课次）铺出来，逐条标状态，
所以能确认「零缺失」是不是真的——而不是被过滤条件悄悄跳过。

用法：
    python3 audit_subs.py               # 审计 config.json 里的全部课程
    python3 audit_subs.py 程序分析 GPU  # 只看指定课程（标题关键字，可多个）
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import icourse_sync as ics  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="源站课次 vs 本地课件 逐条审计（只读）")
    ap.add_argument("course", nargs="*", help="只看标题含这些关键字的课程")
    args = ap.parse_args()

    cookie, why = ics.read_cookie()
    if not cookie:
        print("[登录态] %s" % why)
        return 2
    subs, err = ics.api_subs(cookie)
    if not subs:
        print("[取课次] %s" % err)
        return 2

    cfg_path = ROOT / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    dest = Path(cfg.get("dest") or ics.DEFAULT_DEST).expanduser()
    exc = [k.lower() for k in ((cfg.get("courses") or {}).get("exclude") or [])]
    kws = [k.lower() for k in args.course]
    now = int(time.time())

    groups, excluded = {}, {}
    for s in subs:
        title = s.get("course_title") or "(无标题)"
        if any(k in title.lower() for k in exc):
            excluded[title] = excluded.get(title, 0) + 1
            continue
        if kws and not any(k in title.lower() for k in kws):
            continue
        groups.setdefault(title, []).append(s)

    print("源站课次总数：%d    参与审计的课程：%d 门（另排除 %d 门共 %d 条）"
          % (len(subs), len(groups), len(excluded), sum(excluded.values())))
    for k, v in sorted(excluded.items()):
        print("     [排除] %s  (%d 条)" % (k, v))
    print("目标目录：%s\n" % dest)

    total_all = total_have = total_gap = 0
    for title in sorted(groups):
        lst = sorted(groups[title], key=lambda x: str(x.get("sub_title") or ""))
        print("─" * 68)
        print("【%s】源站 %d 条课次" % (title, len(lst)))
        for s in lst:
            st = s.get("sub_type") or "?"
            try:
                over = int(float(s.get("class_over") or 0))
            except Exception:
                over = 0
            sub_title = s.get("sub_title") or ""
            p = ics.target_path(dest, title, sub_title)
            have = p.exists()
            info = ics.inspect_docx(p) if have else {
                "ok": False, "ts": 0, "cjk": 0, "img": 0, "size": 0}
            stype_ok = st in ics.SUB_TYPES
            ended = over != 0 and over <= now

            if ended and stype_ok:
                total_all += 1
                if have and info["ok"]:
                    total_have += 1
                    mark = "✓ 已有"
                else:
                    total_gap += 1
                    mark = "✗ 缺失"
            else:
                mark = "· 不需要"

            why2 = []
            if not stype_ok:
                why2.append("类型=%s" % st)
            if over == 0:
                why2.append("未结课无回放")
            elif over > now:
                why2.append("未到结课时间")
            extra = ("   [%s]" % "，".join(why2)) if why2 else ""
            stat = ("size=%.1fMB ts=%d cjk=%d img=%d"
                    % (info["size"] / 1e6, info["ts"], info["cjk"], info["img"])) \
                if have else "—"
            print("  %-7s %-22s %-10s %s%s"
                  % (mark, sub_title[:22], st[:10], stat, extra))
        print()

    print("=" * 68)
    print("应同步课次（已结课 且 类型可回放）：%d" % total_all)
    print("本地已完整：%d        缺失：%d" % (total_have, total_gap))
    print("=" * 68)
    return 1 if total_gap else 0


if __name__ == "__main__":
    sys.exit(main())
