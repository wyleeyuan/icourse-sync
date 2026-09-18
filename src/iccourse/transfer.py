"""导出任务：触发、轮询、下载、校验落盘。

平台的导出是「两步异步」：先 export-async 触发，再轮询
get-courseware-status 拿最终下载地址。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from iccourse.api import (BASE_URL, EXPORT_TYPE, JS_EXPORT, JS_STATUS, UA,
                          _py_export, _py_status, spec, url_export, url_status)
from iccourse.documents import inspect_docx, inspect_pptx, strip_translation


def download(url, path: Path, cookie=None, base=BASE_URL):
    """把导出产物下载到本地，返回字节数。"""
    headers = {"User-Agent": UA, "Referer": base + "/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        with open(path, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 256)
    return path.stat().st_size


# ============================================================ 主流程

def fetch_status_all(backend, ids, rounds=8, interval=2.5, types=EXPORT_TYPE):
    """轮询导出状态，返回 {sub_id: 提取结果}。"""
    res = {}
    for _ in range(rounds):
        todo = [i for i in ids
                if i not in res or not (res[i].get("url") or res[i].get("failed")
                                        or res[i].get("err"))]
        if not todo:
            break
        specs = [spec(url_status(i, types), JS_STATUS, _py_status) for i in todo]
        try:
            got = backend.fetch_many(specs)
        except Exception as e:
            print("      轮询异常：%s" % e)
            break
        for i, v in zip(todo, got):
            res[i] = v if isinstance(v, dict) else {"err": "bad response"}
        if any(not (res.get(i, {}).get("url") or res[i].get("failed") or res[i].get("err"))
               for i in ids):
            time.sleep(interval)
    return res


def export_batch(backend, items, types=EXPORT_TYPE):
    """触发导出 → 轮询 → 返回 {sub_id: {url,name}}。items 是 [(sub_id, label)]"""
    ids = [i for i, _ in items]
    if not ids:
        return {}
    specs = [spec(url_export(i, types), JS_EXPORT, _py_export) for i in ids]
    got = backend.fetch_many(specs)
    accepted = []
    accepted_map = {}
    for i, v in zip(ids, got):
        if isinstance(v, dict) and v.get("code") == 0:
            accepted.append(i)
            accepted_map[i] = v
        else:
            msg = (v or {}).get("msg") if isinstance(v, dict) else "bad response"
            print("      触发失败 sub_id=%s：%s" % (i, msg))
    if not accepted:
        return {}
    polled = fetch_status_all(backend, accepted, types=types)
    out = {}
    for i in accepted:
        v = polled.get(i) or {}
        out[i] = v if v.get("url") else {"failed": True,
                                         "msg": v.get("msg") or v.get("err") or "超时未生成"}
    return out


def harvest(backend, dest: Path, jobs, label_prefix=""):
    """jobs: [(sub, path, kind)]，kind ∈ {'main','sub','deck'}。下载 → 校验 → 完整才落盘。

    返回 (saved, shells, failed)；saved 是 [(label, size, info)]
    """
    saved, shells, failed = [], [], []
    for sub, path, kind in jobs:
        sid = sub["sub_id"]
        r = sub["_result"]
        label = sub["_label"]
        if not r.get("url"):
            failed.append((label, r.get("msg") or "未生成"))
            print("      %s未生成 %s" % (label_prefix, label))
            continue
        ext = ".pptx" if kind == "deck" else ".docx"
        tmp = Path(tempfile.gettempdir()) / ("icc_%s_%s%s" % (sid, kind, ext))
        try:
            size = download(r["url"], tmp, cookie=getattr(backend, "cookie", None))
        except Exception as e:
            failed.append((label, "下载失败：%s" % e))
            if tmp.exists():
                tmp.unlink()
            continue

        if kind == "deck":
            info = inspect_pptx(tmp)
            detail = "幻灯片 %d / 截图 %d" % (info["slides"], info["img"])
        else:
            prefix = ""
            if kind == "merge_all":        # 修复路径：先剥掉译文，再按合并版判据校验
                try:
                    prefix = "译文段 -%d / " % strip_translation(tmp)
                except Exception as e:
                    failed.append((label, "剥离译文失败：%s" % e))
                    tmp.unlink()
                    continue
            info = inspect_docx(tmp)
            detail = "%s时间轴 %d / 中文段 %d / 图 %d" % (prefix, info["ts"],
                                                         info["cjk"], info["img"])

        if info["ok"]:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), str(path))
            saved.append((label, size, info))
            print("      ✓ %s%.1f MB  %s" % (label_prefix, size / 1048576.0, detail))
        else:
            tmp.unlink()
            shells.append((label, info["reason"]))
            print("      ✗ %s%s（已丢弃，未落盘）" % (label_prefix, info["reason"]))
    return saved, shells, failed
