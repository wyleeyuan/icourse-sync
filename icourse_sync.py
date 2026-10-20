#!/usr/bin/env python3
"""复旦 icourse 语音课件增量同步 —— 单文件 · 纯标准库 · 零第三方依赖。

产物：<dest>/<课程名>/课件/<课程名>_<课次>_语音课件.docx
      内容 = 课件录屏截图 + 带时间轴的语音识别原文，按时间对齐。

登录态：项目根目录的 cookie.txt，内容是整条 Cookie 请求头。
        获取方式见 --help 或 README（Chrome DevTools 复制一次，长期有效）。

用法：
    python3 icourse_sync.py             # 校验登录态 → 增量同步
    python3 icourse_sync.py --check     # 只校验 cookie.txt 是否还有效
    python3 icourse_sync.py --dry-run   # 只列计划，不触发导出
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "https://icourse.fudan.edu.cn"
TENANT = "222"                 # 复旦租户号
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

DEFAULT_DEST = "~/Documents/课程"

NATIVE_TYPES = "1,2"                      # 原生：1=课件截图 2=语音识别原文
REPAIR_KEYS = ("1,2,3", "1,2,4")          # 缓存被锁死成空壳时，换个 key 重取
SUB_TYPES = ("course_live", "olive", "video")

POLL_ROUNDS, POLL_INTERVAL = 10, 2.5      # 导出轮询：最多 10 轮、每轮间隔 2.5 秒
SHELL_MIN_CJK, SHELL_RATIO = 20, 0.15     # 空壳判据：中文段数 / 中文段÷时间轴

COOKIE_HELP = """\
cookie.txt 缺失或已失效。重新取一次（约 1 分钟，之后长期有效）：

  1. Chrome 打开  %s/education/?tenant_code=%s  并确认已登录
  2. F12 → Network 标签 → Command+R 刷新页面
  3. 在请求列表里随便点一条 icourse.fudan.edu.cn 的请求
  4. 右侧 Headers → Request Headers，拉到底部找到 Cookie: 那一行
     （在它的值上右键 → Copy value；或右键请求 → Copy as cURL）
  5. 把内容整条粘进  %s  保存（覆盖原内容即可）

提示：Cookie 里的 _token 是 JWT，通常隔天前后过期，到时重做一遍即可。""" % (
    BASE, TENANT, ROOT / "cookie.txt")


# ============================================================ 配置

DEFAULT_CONFIG = {"dest": DEFAULT_DEST, "courses": {"include": [], "exclude": []}}


def load_config(explicit=None):
    """读可选的 config.json（缺省即用内置默认值）。返回 (config, 来源路径 or None)。"""
    cands = [Path(explicit).expanduser()] if explicit else []
    cands.append(ROOT / "config.json")
    for c in cands:
        if c.is_file():
            try:
                raw = json.loads(c.read_text(encoding="utf-8"))
            except Exception as e:
                raise SystemExit("配置文件解析失败 %s：%s" % (c, e))
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(raw or {})
            return cfg, c
    return dict(DEFAULT_CONFIG), None


def read_cookie(path=None):
    """读 cookie.txt（整条 Cookie 请求头）。返回 (cookie, 错误原因)。"""
    p = Path(path).expanduser() if path else (ROOT / "cookie.txt")
    if not p.is_file():
        return None, "cookie.txt 不存在"
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return None, "读取 cookie.txt 失败：%s" % e
    txt = " ".join(txt.split())
    return (txt, None) if txt and "=" in txt else (None, "cookie.txt 内容为空或格式不对")


# ============================================================ HTTP

def _get_json(url, cookie, timeout=90):
    """发一个带登录态的 GET，返回 (已解析的 JSON 或 None, HTTP 状态码)。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Cookie": cookie,
        "Referer": BASE + "/", "Accept": "application/json, text/plain, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore")), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, -1


def _download(url, cookie, path):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Cookie": cookie, "Referer": BASE + "/"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f, 256 * 1024)
    return path.stat().st_size


# ============================================================ 平台接口

def api_subs(cookie):
    """枚举我的全部课次。返回 (列表 或 None, 错误原因)。"""
    url = BASE + "/courseapi/v3/multi-search/get-my-sub-list?page=1&per_page=200"
    j, st = _get_json(url, cookie)
    if not isinstance(j, dict):
        return None, "接口无响应（HTTP %s）" % st
    if "data" not in j:
        msg = str(j.get("msg") or j.get("error") or "返回结构异常")
        return None, "登录态无效：%s" % msg[:80]
    raw = (j.get("data") or {}).get("list") or {}
    # list 里除索引项外还混着 "now": <时间戳>，必须按类型过滤
    return [x for x in raw.values()
            if isinstance(x, dict) and x.get("course_id")], None


def api_ready(cookie, sub_id):
    """预检该课次的语音识别是否跑完。返回 (code, 字幕条数)。"""
    url = BASE + "/courseapi/v3/web-socket/search-trans-result?sub_id=%s&format=json" % sub_id
    j, _ = _get_json(url, cookie)
    if not isinstance(j, dict) or "code" not in j:
        return -1, 0
    lst = j.get("list") or []
    first = lst[0] if lst and isinstance(lst[0], dict) else {}
    return j.get("code"), len(first.get("all_content") or [])


def _export_url(sub_id, types):
    return (BASE + "/courseapi/v2/export/export-async?sub_id=%s"
            "&export_type=courseware&type=%s" % (sub_id, types))


def _status_url(sub_id, types):
    return (BASE + "/courseapi/v2/export/get-courseware-status?type=%s"
            "&sub_id=%s&get_source_url=1" % (types, sub_id))


def export_batch(cookie, sub_ids, types):
    """触发批量导出并轮询到位。返回 {sub_id: (下载地址 or '', 失败原因 or None)}。"""
    out = {}
    for sid in sub_ids:
        j, st = _get_json(_export_url(sid, types), cookie)
        if isinstance(j, dict) and j.get("code") == 0:
            out[sid] = None                      # None = 已受理，待轮询
        else:
            msg = (j or {}).get("msg") or ("HTTP %s" % st)
            out[sid] = ("", "触发失败：%s" % str(msg)[:60])

    pending = [s for s in sub_ids if out.get(s) is None]
    for _ in range(POLL_ROUNDS):
        if not pending:
            break
        still = []
        for sid in pending:
            j, st = _get_json(_status_url(sid, types), cookie)
            code = (j or {}).get("code")
            data = (j or {}).get("data") or {}
            if code == 0 and data.get("path_name"):
                out[sid] = (data["path_name"], None)
                continue
            msg = str((j or {}).get("msg") or "")
            if code not in (0, 200, None) and not re.search(r"生成中|导出中|处理中", msg):
                out[sid] = ("", "服务端拒绝：%s" % (msg[:60] or code))
                continue
            still.append(sid)
        pending = still
        if pending:
            time.sleep(POLL_INTERVAL)

    for sid in pending:                          # 轮询轮数用尽仍在生成
        out[sid] = ("", "轮询超时（服务端仍在生成）")
    return out


# ============================================================ 产物

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip()
    return s.rstrip(".") or "untitled"


def target_path(dest, course_title, sub_title):
    fn = "%s_%s_语音课件.docx" % (safe_name(course_title), safe_name(sub_title))
    return dest / safe_name(course_title) / "课件" / fn


def inspect_docx(path):
    """检查 docx 是否为完整的语音课件。返回 dict(ok, ts, cjk, img, size, reason)。

    校验靠「数文本」而不是看体积 —— 空壳同样有截图和时间轴，只是没有一句字幕。
    """
    info = {"ok": False, "ts": 0, "cjk": 0, "img": 0,
            "size": path.stat().st_size if path.exists() else 0, "reason": ""}
    if not path.exists():
        info["reason"] = "文件不存在"
        return info
    try:
        if not zipfile.is_zipfile(path):
            info["reason"] = "不是有效 docx（zip 损坏）"
            return info
        z = zipfile.ZipFile(path)
        if "word/document.xml" not in z.namelist():
            info["reason"] = "缺少 word/document.xml"
            return info
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
        info["img"] = len([n for n in z.namelist() if n.startswith("word/media")])
    except Exception as e:
        info["reason"] = "解析失败：%s" % e
        return info

    ts = cjk = 0
    for chunk in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, re.S):
        t = re.sub(r"<[^>]+>", "", chunk).strip()
        if not t:
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}[：:]?$", t):
            ts += 1
        elif re.search(r"[一-龥]", t):
            cjk += 1
    info["ts"], info["cjk"] = ts, cjk

    if ts == 0:
        info["reason"] = "无时间轴，非语音课件"
    elif cjk < SHELL_MIN_CJK or (cjk / float(ts)) < SHELL_RATIO:
        info["reason"] = "空壳（无字幕）：时间轴 %d 条 / 中文段 %d 条" % (ts, cjk)
    else:
        info["ok"] = True
    return info


_TS_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_P_OPEN_RE = re.compile(r"<w:p(?:\s[^>]*)?/?>")


def _para_spans(xml):
    """每段在 XML 里的精确字符区间：`<w:p` 不嵌套，故「开标签 → 首个 </w:p>」即一段。"""
    spans = []
    for m in _P_OPEN_RE.finditer(xml):
        start = m.start()
        if xml[max(0, m.end() - 2):m.end()] == "/>":
            spans.append((start, m.end(), xml[start:m.end()]))
            continue
        end = xml.find("</w:p>", m.end())
        if end < 0:
            continue
        end += len("</w:p>")
        spans.append((start, end, xml[start:end]))
    return spans


def _para_text(seg):
    return "".join(re.sub(r"<[^>]+>", "", t)
                   for t in re.findall(r"<w:t[^>]*>(.*?)</w:t>", seg, re.S))


def strip_translation(docx):
    """就地剥掉译文段落，返回删除条数。

    `type=1,2,3` 的产物里，原文段与英文译文段样式完全相同，只能按「语言 + 位置」判：
    紧跟原文段之后的、连续的不含中文段落即译文。只删段落本身，其它 XML 原样保留。
    """
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
        items = [(i.filename, i.date_time, i.external_attr, z.read(i.filename))
                 for i in z.infolist()]

    drop, prev, in_trans, n = [], None, False, 0
    for s, e, seg in _para_spans(xml):
        t = _para_text(seg).strip()
        if not t:
            continue
        if _TS_RE.match(t):
            prev, in_trans = "TS", False
            continue
        if _CJK_RE.search(t):
            prev, in_trans = "CJK", False
            continue
        if prev == "CJK" or in_trans:          # 紧跟原文 / 译文带的续行 → 译文
            drop.append((s, e))
            in_trans = True
            n += 1
            continue
        prev, in_trans = "OTH", False          # 紧跟时间轴的纯英文句：是原文，留着

    if not n:
        return 0
    for s, e in reversed(drop):
        xml = xml[:s] + xml[e:]
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, dt, attr, data in items:
            if name == "word/document.xml":
                data = xml.encode("utf-8")
            zi = zipfile.ZipInfo(name, date_time=dt)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = attr
            zout.writestr(zi, data)
    return n


# ============================================================ 下载与校验落盘

def harvest(cookie, items, types):
    """items: [(sub, path)]，sub 里带 _url。下载 → 校验 → 完整才落盘。

    返回 (saved, shells, failed)；saved = [(label, size, info)]
    """
    saved, shells, failed = [], [], []
    for sub, path in items:
        label, url = sub["_label"], sub["_url"]
        if not url:
            failed.append((label, sub.get("_err") or "未生成"))
            print("      未生成 %s" % label)
            continue
        ext = ".docx"
        tmp = Path(tempfile.gettempdir()) / ("icc_%s%s" % (sub["sub_id"], ext))
        try:
            size = _download(url, cookie, tmp)
        except Exception as e:
            failed.append((label, "下载失败：%s" % e))
            if tmp.exists():
                tmp.unlink()
            continue

        prefix = ""
        if types != NATIVE_TYPES:              # 修复路径：先剥译文再校验
            try:
                prefix = "译文段 -%d / " % strip_translation(tmp)
            except Exception as e:
                failed.append((label, "剥离译文失败：%s" % e))
                tmp.unlink()
                continue
        info = inspect_docx(tmp)
        if info["ok"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(path))
            saved.append((label, size, info))
            print("      ✓ %.1f MB  %s时间轴 %d / 中文段 %d / 图 %d"
                  % (size / 1048576.0, prefix, info["ts"], info["cjk"], info["img"]))
        else:
            tmp.unlink()
            shells.append((label, info["reason"]))
            print("      ✗ %s（已丢弃，未落盘）" % info["reason"])
    return saved, shells, failed


# ============================================================ 主流程

def run_layer(cookie, items, types, tag=""):
    """一层导出：触发 → 轮询 → 下载校验。返回 (saved, shells, failed, unresolved)。"""
    print("\n%s触发导出并轮询（%d 节 · type=%s）..." % (tag, len(items), types))
    results = export_batch(cookie, [s["sub_id"] for s, _ in items], types)
    jobs, unresolved = [], []
    for sub, path in items:
        url, err = results.get(sub["sub_id"], ("", "无响应"))
        sub["_url"], sub["_err"] = url, err
        (jobs if url else unresolved).append((sub, path))
    saved, shells, failed = ([], [], []) if not jobs else harvest(cookie, jobs, types)
    for sub, _p in unresolved:
        failed.append((sub["_label"], sub["_err"] or "未生成"))
        print("      未生成 %s" % sub["_label"])
    return saved, shells, failed, [s for s, _p in unresolved]


def main():
    ap = argparse.ArgumentParser(
        description="复旦 icourse 语音课件增量同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=COOKIE_HELP)
    ap.add_argument("--dest", help="目标目录（覆盖 config.json）")
    ap.add_argument("--course", action="append", default=None,
                    help="只同步指定课程（可重复，按课名片段模糊匹配）")
    ap.add_argument("--check", action="store_true", help="只校验 cookie.txt 是否有效")
    ap.add_argument("--dry-run", action="store_true", help="只列计划，不触发导出")
    ap.add_argument("--force", action="store_true",
                    help="忽略本地已有文件全部重导（会推高服务端下载计数，慎用）")
    ap.add_argument("--cookie", help="指定 cookie 文件（默认项目根 cookie.txt）")
    ap.add_argument("--config", help="指定配置文件（默认项目根 config.json）")
    args = ap.parse_args()

    cfg, cfg_path = load_config(args.config)
    cookie, why = read_cookie(args.cookie)
    if not cookie:
        print("[登录态] %s\n\n%s" % (why, COOKIE_HELP))
        return 1

    print("[1/4] 校验登录态 ...")
    subs, err = api_subs(cookie)
    if subs is None:
        print("      ✗ %s\n\n%s" % (err, COOKIE_HELP))
        return 1
    print("      ✓ cookie.txt 有效，可见 %d 条课次" % len(subs))
    if args.check:
        return 0

    dest = Path(args.dest or cfg["dest"]).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    print("      目标目录：%s" % dest)
    if cfg_path:
        print("      配置来源：%s" % cfg_path)

    print("[2/4] 比对本地文件 ...")
    now = int(time.time())
    flt = cfg.get("courses") or {}
    inc = [k.lower() for k in (flt.get("include") or [])]
    exc = [k.lower() for k in (flt.get("exclude") or [])]
    kws = [k.lower() for k in (args.course or [])]
    plan, skipped = [], 0
    for s in subs:
        if s.get("sub_type") not in SUB_TYPES:
            continue
        try:
            over = int(float(s.get("class_over") or 0))
        except Exception:
            over = 0
        if over == 0 or over > now:            # 未排期 / 尚未结课 → 不可能有回放
            continue
        title = s.get("course_title") or ""
        if inc and not any(k in title.lower() for k in inc):
            continue
        if exc and any(k in title.lower() for k in exc):
            continue
        if kws and not any(k in title.lower() for k in kws):
            continue

        p = target_path(dest, title, s.get("sub_title") or "")
        label = "%s/%s" % (safe_name(title), p.name)
        if p.exists() and not args.force:
            info = inspect_docx(p)
            if info["ok"]:
                skipped += 1
                continue
            print("      需重导 [%s] 原因：%s" % (label, info["reason"]))
            if info["size"] and info["reason"].startswith("空壳"):
                p.unlink()                     # 空壳别留在目录里
        plan.append({"sub_id": s.get("sub_id"), "title": title,
                     "label": label, "path": p, "course_title": title,
                     "sub_title": s.get("sub_title") or ""})

    print("      已完整 %d 份 / 待处理 %d 份" % (skipped, len(plan)))
    for it in plan:
        print("        - %s" % it["label"])
    if not plan:
        print("\n无需更新。")
        return 0

    print("[3/4] 预检语音识别就绪状态 ...")
    ready, waiting = [], []
    for it in plan:
        code, n = api_ready(cookie, it["sub_id"])
        if code == 0 and n > 0:
            ready.append(it)
        else:
            waiting.append((it, code, n))
    for it, code, n in waiting:
        print("      等待识别 [%s]  code=%s 字幕=%s 条" % (it["label"], code, n))
    print("      可导出 %d 节 / 等待源站识别 %d 节" % (len(ready), len(waiting)))

    if not ready:
        print("\n无需导出：待处理的课次都还没跑完语音识别。")
        print("（等源站识别完成后重跑本脚本即可，脚本不会提前导出空壳。）")
        return 0
    if args.dry_run:
        print("\n--dry-run：以上 %d 节将会被导出，本次未调用导出接口。" % len(ready))
        return 0

    saved, shells, failed = [], [], []
    for it in ready:
        it["_label"] = it["label"]

    print("[4/4] 开始导出 ...")
    s, sh_, f, unresolved = run_layer(cookie, [(it, it["path"]) for it in ready],
                                      NATIVE_TYPES)
    saved += s
    shells += list(sh_)
    failed += f

    # 空壳的课次换个缓存 key 重取（一个课次可能多个 key 先后被污染，所以是逐个试）
    repair = [(dict(it), it["path"]) for it in ready if it["label"] in {l for l, _ in sh_}]
    for key in REPAIR_KEYS:
        if not repair:
            break
        print("\n修复：%d 节改用 type=%s 重取%s ..."
              % (len(repair), key, "并剥掉译文段" if "3" in key else ""))
        s, _sh, f, _un = run_layer(cookie, repair, key, tag="  ")
        saved += s
        failed += f
        ok = {l for l, _sz, _i in s}
        repair = [(sub, p) for sub, p in repair if sub["_label"] not in ok]
    if repair:
        print("\n      ⤷ 修复 key（%s）都没拿到完整产物。" % "、".join(REPAIR_KEYS))

    ok_final = {l for l, _sz, _i in saved}
    shells = [(l, w) for l, w in shells if l not in ok_final]

    print("\n================ 同步结果 ================")
    print("目录        : %s" % dest)
    print("新增/更新   : %d" % len(saved))
    print("跳过(已完整): %d" % skipped)
    if shells:
        print("未通过校验  : %d  （已丢弃，未落盘）" % len(shells))
        for f_, w in shells:
            print("   - %s  (%s)" % (f_, w))
    if waiting:
        print("等待识别    : %d  （源站语音识别未跑完，本次未导出）" % len(waiting))
        for it, _c, _n in waiting:
            print("   - %s" % it["label"])
    if failed:
        uniq = {}
        for f_, w in failed:
            uniq[f_] = w          # 同一课次可能多层重试，只报最后一次的原因
        print("失败        : %d" % len(uniq))
        for f_, w in uniq.items():
            print("   - %s  (%s)" % (f_, w))
    print("=========================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
