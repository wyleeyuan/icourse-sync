#!/usr/bin/env python3
"""复旦 icourse 语音课件增量同步 —— 命令行入口。

把复旦大学智慧教学资源平台（icourse.fudan.edu.cn）的「语音课件」
（课件截图 + 语音识别原文，按时间轴对齐的 Word）增量同步到本地。

正常只产出合并的语音课件；只有某课次的服务端导出缓存已被锁死、
合并版永久拿不回来时，才退化为拆分的 `_字幕.docx` + `_课件.pptx` 两份。

实现拆在 iccourse/ 包里，各模块职责：

    config      配置与全局常量
    api         接口契约（URL 构造 / 浏览器 JS / 响应提取器）
    backends    请求后端（bsk 借浏览器会话、cookie 纯 HTTP）
    auth        登录态（读浏览器 Cookie、固化 cookie.txt、现场登录）
    documents   产物校验与清洗（docx/pptx 完整性、剥掉译文段）
    paths       产物命名与覆盖判定
    transfer    导出触发 / 轮询 / 下载 / 落盘

用法::

    python3 src/icc_sync.py                # 按 config.json 同步
    python3 src/icc_sync.py --login        # 现场登录并固化 Cookie
    python3 src/icc_sync.py --dry-run      # 只报告计划，不调导出接口
    python3 src/icc_sync.py --list         # 列出全部可导课次及本地状态
    python3 src/icc_sync.py --dest ~/x     # 临时覆盖目标目录
    python3 src/icc_sync.py --course 程序分析
    python3 src/icc_sync.py --force        # 忽略本地文件重导（慎用）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# 支持 `python3 src/icc_sync.py` 直接运行：把 src/ 放进模块搜索路径，
# 这样 iccourse 包不论从哪个工作目录调用都能被找到。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from iccourse.api import (DECK_TYPE, EXPORT_MERGE_CANDIDATES, EXPORT_TYPE,
                          JS_TRANS, SUBTITLE_TYPE, TRANS_TYPES, _py_trans, spec,
                          url_trans)
from iccourse.auth import (_save_cookie, candidate_backends, cmd_login,
                           verify_login)
from iccourse.config import (DEFAULT_RETRY_FALLBACK_DAYS, PROJECT_ROOT, expand,
                             load_config, write_default_config)
from iccourse.documents import inspect_docx, safe_name
from iccourse.paths import (clean_fallback, covered, deck_path,
                            fallback_age_days, migrate_flat, subtitle_path,
                            target_path)
from iccourse.transfer import export_batch, harvest


def main():
    ap = argparse.ArgumentParser(
        description="复旦 icourse 语音课件增量同步",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="指定配置文件（默认用项目根 config.json）")
    ap.add_argument("--dest", help="目标目录（覆盖配置）")
    ap.add_argument("--course", action="append", default=None,
                    help="只同步指定课程（可重复，按课程名模糊匹配）")
    ap.add_argument("--auth", choices=["auto", "bsk", "cookie", "browser"],
                    help="登录态来源（覆盖配置）")
    ap.add_argument("--login", action="store_true",
                    help="现场登录：打开浏览器 → 登录 → 自动读取并固化 Cookie")
    ap.add_argument("--force", action="store_true",
                    help="忽略本地文件全部重导（会推高服务端下载计数，慎用）")
    ap.add_argument("--no-repair", action="store_true",
                    help="禁用修复路径（逐个换 1,2,3 / 1,2,4 缓存 key），锁死课次直接走拆分兜底")
    ap.add_argument("--dry-run", action="store_true", help="只报告计划，不调导出接口")
    ap.add_argument("--list", action="store_true", help="只列出课次清单")
    ap.add_argument("--keep-session", action="store_true", help="结束后不关浏览器会话")
    args = ap.parse_args()

    cfg, cfg_path = load_config(args.config)
    if cfg_path is None:
        new_cfg = PROJECT_ROOT / "config.json"
        write_default_config(new_cfg)
        cfg_path = new_cfg
        print("已生成默认配置：%s" % new_cfg)
        print("（当前内容就是内置默认值，改了 dest 再跑即可）\n")

    dest = expand(args.dest or cfg["dest"])
    dest.mkdir(parents=True, exist_ok=True)
    n = migrate_flat(dest)
    if n:
        print("已迁移 %d 份旧文件到课程子目录。" % n)

    print("目标目录：%s" % dest)
    print("配置来源：%s" % cfg_path)

    if args.login:
        print("[登录] 现场登录 ...")
        cmd_login(cfg)
        return 0

    print("[1/6] 准备登录态 ...")
    # 逐个候选后端 open + 真实枚举验证；前面的能用就绝不碰后面
    # （cookie.txt 过期时会自动降级去读浏览器，读到了再固化回来）
    backend, subs, save_to, failed = None, None, None, []
    for cand, dest_cookie in candidate_backends(cfg, args.auth):
        try:
            cand.open()
            subs = verify_login(cand)
        except Exception as e:
            print("      %s 不可用：%s" % (cand.label, str(e)[:140]))
            subs = None
        if subs is None:
            print("      %s 登录态无效（多为 Cookie 过期），换下一个来源" % cand.label)
            failed.append(cand.label)
            try:
                cand.close()
            except Exception:
                pass
            continue
        backend, save_to = cand, dest_cookie
        print("      后端：%s" % cand.label)
        break
    if backend is None:
        raise SystemExit("找不到能用的登录态（已尝试：%s）。\n"
                         "  先跑一次：icc-sync --login" % ("、".join(failed) or "无"))
    if save_to is not None:
        ck = getattr(backend, "cookie", None)
        if ck and _save_cookie(save_to, ck):
            print("      Cookie 已固化到 %s（下次直接命中）" % save_to.name)

    print("[2/6] 枚举我的课程 ...")
    try:
        now = int(time.time())

        cands = []
        for s in subs:
            if s.get("sub_type") not in TRANS_TYPES:
                continue
            try:
                over = int(float(s.get("class_over") or 0))
            except Exception:
                over = 0
            if over == 0 or over > now:      # 未排期 / 尚未结课 -> 不可能有回放
                continue
            cands.append(s)

        flt = cfg.get("courses") or {}
        inc = [k.lower() for k in (flt.get("include") or [])]
        exc = [k.lower() for k in (flt.get("exclude") or [])]
        if inc:
            cands = [s for s in cands if any(k in s["course_title"].lower() for k in inc)]
        if exc:
            cands = [s for s in cands if not any(k in s["course_title"].lower() for k in exc)]
        if args.course:
            kws = [k.lower() for k in args.course]
            cands = [s for s in cands if any(k in s["course_title"].lower() for k in kws)]

        print("      全库 %d 条 / 已结课候选 %d 条" % (len(subs), len(cands)))

        if args.list:
            for s in cands:
                c = covered(dest, s["course_title"], s["sub_title"])
                mark = {"main": "完整", "fallback": "待修复", None: "缺失"}[c]
                print("      [%s] %s/%s" % (mark, safe_name(s["course_title"]),
                                            target_path(dest, s["course_title"],
                                                        s["sub_title"]).name))
            return 0

        retry_days = float(cfg.get("retry_fallback_days",
                                   DEFAULT_RETRY_FALLBACK_DAYS))
        print("[3/6] 比对本地文件 ...")
        plan, skipped = [], []
        for s in cands:
            p = target_path(dest, s["course_title"], s["sub_title"])
            label = "%s/%s" % (safe_name(s["course_title"]), p.name)
            cov = covered(dest, s["course_title"], s["sub_title"])
            if p.exists() and not args.force:
                info = inspect_docx(p)
                if info["ok"]:
                    skipped.append(label)
                    continue
                print("      需重导 [%s] 原因：%s" % (label, info["reason"]))
                if info["size"] and info["reason"].startswith("空壳"):
                    p.unlink()          # 空壳直接清掉，别在目录里留废文件
            # 本地已有拆分兜底 => (sub_id,"1,2") 确定被锁死，直接走修复路径，
            # 不必再白试一次 "1,2"；但修复本身要按天数节流，否则每轮同步都
            # 为它白跑一遍导出 + 重下兜底（那两个 key 短期内不会变好）。
            mode = "repair" if (cov == "fallback" and not args.no_repair) else "normal"
            if mode == "repair" and not args.force:
                age = fallback_age_days(dest, s["course_title"], s["sub_title"])
                if age is not None and age < retry_days:
                    skipped.append("%s  [兜底产物 %.1f 天前才试过，%.1f 天后自动重试]"
                                   % (label, age, retry_days - age))
                    continue
            plan.append({"sub": s, "file": label, "path": p, "mode": mode})

        print("      已完整 %d 份 / 待处理 %d 份" % (len(skipped), len(plan)))
        for it in plan:
            print("        - %s%s" % (it["file"],
                                      "   [走修复路径]" if it["mode"] == "repair" else ""))
        if not plan:
            print("\n[6/6] 无需更新。")
            return 0

        print("[4/6] 预检语音识别就绪状态 ...")
        ids = [it["sub"]["sub_id"] for it in plan]
        try:
            ready = backend.fetch_many(
                [spec(url_trans(i), JS_TRANS, _py_trans) for i in ids])
            ready_map = {i: (ready[k] if k < len(ready) and isinstance(ready[k], dict) else {})
                         for k, i in enumerate(ids)}
        except Exception as e:
            print("      预检不可用（%s）。" % e)
            print("      → 判不准就绪状态，为免把空壳种进服务端缓存，本轮不导出。")
            return 0

        runnable, waiting = [], []
        for it in plan:
            sid = it["sub"]["sub_id"]
            d = ready_map.get(sid) or {}
            if d.get("code") == 0 and d.get("n", 0) > 0:
                runnable.append(it)
            else:
                waiting.append((it["file"], d.get("code"), d.get("n", 0)))
        for f, c, nn in waiting:
            print("      等待识别 [%s]  code=%s 字幕=%s 条" % (f, c, nn))
        print("      可导出 %d 节 / 等待源站识别 %d 节" % (len(runnable), len(waiting)))

        if not runnable:
            print("\n无需导出：待处理的课次都还没跑完语音识别。")
            print("（等源站识别完成后重跑本脚本即可，脚本不会提前导出空壳。）")
            return 0
        if args.dry_run:
            print("\n--dry-run：以上 %d 节将会被导出，本次未调用导出接口。" % len(runnable))
            return 0

        normal = [it for it in runnable if it["mode"] == "normal"]
        repair = [it for it in runnable if it["mode"] == "repair"]
        saved, shells, failed = [], [], []

        # ---- 第一层：原生 type=1,2。形态最好、零损耗，正常课次都走这里。
        if normal:
            print("[5/6] 触发导出并轮询（%d 节 · type=%s）..." % (len(normal), EXPORT_TYPE))
            label_of = {it["sub"]["sub_id"]: it["file"] for it in normal}
            results = export_batch(backend, [(i, label_of[i]) for i in label_of])
            jobs = []
            for it in normal:
                it["sub"]["_result"] = results.get(it["sub"]["sub_id"]) or {}
                it["sub"]["_label"] = it["file"]
                jobs.append((it["sub"], it["path"], "main"))

            print("[6/6] 下载并校验 ...")
            s1, h1, f1 = harvest(backend, dest, jobs)
            saved += s1
            failed += f1
            reasons = {lbl: why for lbl, why in h1}
            ok_labels = {lbl for lbl, _sz, _inf in s1}
            for it in normal:
                if it["file"] in reasons:
                    print("      ⤷ 主产物是空壳（%s）→ 转修复路径" % reasons[it["file"]])
                    repair.append(it)
                elif it["file"] in ok_labels:
                    # 合并版到手 -> 顺手清掉历史遗留的拆分兜底/孤儿产物
                    clean_fallback(dest, it["sub"]["course_title"],
                                   it["sub"]["sub_title"])
            rl = {it["file"] for it in repair}
            shells += [(l, w) for l, w in h1 if l not in rl]

        # ---- 第二层（修复）：逐个换用**未被污染的缓存 key**，直到拿到合并版。
        #      "1,2,3" 多一份英文译文，本地剥掉即等价于原生 "1,2"；
        #      "1,2,4" 多一份 AI 摘要，实测导出成 Word 时没混进正文，下载即用。
        #      一个课次可能**多个 key 先后被污染**（识别跑完前被导出过多次就会这样），
        #      所以是「逐个试」，不是「只试一个」。
        for cand_types in EXPORT_MERGE_CANDIDATES:
            if not repair:
                break
            print("\n修复：%d 节改用 type=%s（独立缓存 key）重取%s ..."
                  % (len(repair), cand_types,
                     "并剥掉译文段" if "3" in cand_types else ""))
            items = [(it["sub"]["sub_id"], it["file"]) for it in repair]
            r_all = export_batch(backend, items, types=cand_types)
            jobs_all = []
            for it in repair:
                it["sub"]["_result"] = r_all.get(it["sub"]["sub_id"]) or {}
                it["sub"]["_label"] = it["file"]
                jobs_all.append((it["sub"], it["path"], "merge_all"))
            s2, h2, f2 = harvest(backend, dest, jobs_all,
                                 label_prefix="[修复 %s] " % cand_types)
            saved += s2
            failed += f2
            ok = {lbl for lbl, _sz, _inf in s2}
            # 合并版到手 -> 清掉此前遗留的拆分兜底产物
            for it in repair:
                if it["file"] in ok:
                    clean_fallback(dest, it["sub"]["course_title"],
                                   it["sub"]["sub_title"])
            # 只有真正落盘的才算修好；本轮空壳 / 触发失败的留给下一个候选
            repair = [it for it in repair if it["file"] not in ok]
            shells += [(l, "%s 下 - %s" % (cand_types, w)) for l, w in h2]

        if repair:
            print("\n      ⤷ 修复 key（%s）全部只返回空壳，转拆分兜底。"
                  % "、".join(EXPORT_MERGE_CANDIDATES))

        # ---- 第三层（兜底）：合并版两个 key 都没救回来时，退回拆分产物：
        #        type=2 -> 纯字幕 _字幕.docx
        #        type=1 -> 课件截图 _课件.pptx（每页一张录屏截图）
        if repair:
            print("\n兜底：%d 节合并版两个 key 都不可用，改用拆分接口重取 ..." % len(repair))
            items = [(it["sub"]["sub_id"], it["file"]) for it in repair]

            print("      → 取纯字幕（type=%s）" % SUBTITLE_TYPE)
            r_sub = export_batch(backend, items, types=SUBTITLE_TYPE)
            jobs_sub = []
            for it in repair:
                it["sub"]["_result"] = r_sub.get(it["sub"]["sub_id"]) or {}
                it["sub"]["_label"] = it["file"]
                jobs_sub.append((it["sub"],
                                 subtitle_path(dest, it["sub"]["course_title"],
                                               it["sub"]["sub_title"]), "sub"))
            saved_sub, shells_sub, failed_sub = harvest(backend, dest, jobs_sub,
                                                        label_prefix="[字幕] ")

            print("      → 取课件截图（type=%s）" % DECK_TYPE)
            r_deck = export_batch(backend, items, types=DECK_TYPE)
            jobs_deck = []
            for it in repair:
                it["sub"]["_result"] = r_deck.get(it["sub"]["sub_id"]) or {}
                it["sub"]["_label"] = it["file"]
                jobs_deck.append((it["sub"],
                                  deck_path(dest, it["sub"]["course_title"],
                                            it["sub"]["sub_title"]), "deck"))
            saved_deck, shells_deck, failed_deck = harvest(backend, dest, jobs_deck,
                                                           label_prefix="[课件] ")

            saved += saved_sub + saved_deck
            failed += failed_sub + failed_deck
            # 兜底过的课次不再按"空壳丢弃"报告；两份里缺哪份就报哪份
            fb = {it["file"] for it in repair}
            shells = [(l, w) for l, w in shells if l not in fb]
            shells += [(l, "字幕 - " + w) for l, w in shells_sub]
            shells += [(l, "课件 - " + w) for l, w in shells_deck]

        # 修复过程中先报空壳、换 key 后成功的课次，不该同时出现在"未通过校验"里
        ok_final = {lbl for lbl, _sz, _inf in saved}
        shells = [(l, w) for l, w in shells if l not in ok_final]

        print("\n================ 同步结果 ================")
        print("目录        : %s" % dest)
        print("新增/更新   : %d" % len(saved))
        print("跳过(已完整): %d" % len(skipped))
        if shells:
            print("未通过校验  : %d  （已丢弃，未落盘）" % len(shells))
            for f, why in shells:
                print("   - %s  (%s)" % (f, why))
        if waiting:
            print("等待识别    : %d  （源站语音识别未跑完，本次未导出）" % len(waiting))
            for f, c, nn in waiting:
                print("   - %s" % f)
        if failed:
            print("失败        : %d" % len(failed))
            for f, why in failed:
                print("   - %s  (%s)" % (f, why))
        print("=========================================")
        return 0
    finally:
        if not args.keep_session:
            backend.close()


if __name__ == "__main__":
    sys.exit(main())
