"""产物校验与清洗：文件名净化、docx/pptx 完整性判定、剥掉英文译文段。

校验是「数文本」而不是看体积 —— 空壳同样有截图和时间轴，
只是没有一句字幕，光看大小分辨不出来。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path


# 空壳判据：中文正文段数 / 时间轴条数 低于此比值，视为语音识别还没跑完
SHELL_RATIO = 0.15
SHELL_MIN_CJK = 20



# ============================================================ 产物校验

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(s)).strip()
    return s.rstrip(".") or "untitled"


def inspect_docx(path: Path) -> dict:
    """检查 docx 是否为完整的语音课件。返回 dict(ok, ts, cjk, img, size, reason)。"""
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

    # 兼容两种产物结构（所以按 <w:t> 文本节点统计，不要按 <w:p> 切段）：
    #   type=1,2 → 一行一个 <w:p>，时间戳形如 00:00:26
    #   type=2   → 整篇挤在一个 <w:p> 里用 <w:br/> 换行，时间戳是全角冒号 00:02:03：
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
        return info
    if cjk < SHELL_MIN_CJK or (cjk / float(ts)) < SHELL_RATIO:
        info["reason"] = "空壳（无字幕）：时间轴 %d 条 / 中文段 %d 条" % (ts, cjk)
        return info
    info["ok"] = True
    return info


def inspect_pptx(path: Path) -> dict:
    """检查 pptx 是否为有效的课件包（每页一张录屏截图，无可编辑文字）。

    返回 dict(ok, slides, img, size, reason)。
    """
    info = {"ok": False, "slides": 0, "img": 0,
            "size": path.stat().st_size if path.exists() else 0, "reason": ""}
    if not path.exists():
        info["reason"] = "文件不存在"
        return info
    try:
        if not zipfile.is_zipfile(path):
            info["reason"] = "不是有效 pptx（zip 损坏）"
            return info
        names = zipfile.ZipFile(path).namelist()
    except Exception as e:
        info["reason"] = "解析失败：%s" % e
        return info

    info["slides"] = len([n for n in names
                          if re.match(r"^ppt/slides/slide\d+\.xml$", n)])
    info["img"] = len([n for n in names if n.startswith("ppt/media/")])
    if info["slides"] < 1:
        info["reason"] = "无幻灯片"
        return info
    if info["img"] < 1:
        info["reason"] = "无课件截图"
        return info
    info["ok"] = True
    return info


# ============================================================ 译文剥离
#
# `type=1,2,3` 的产物 = 时间轴 / 中文原文段 / 英文译文段，逐条交替：
#
#     00:02:03
#     今天呢把这个转机的4G、网络给它提一下，
#     Today, I would like to mention the 4G and network of this turnaround.
#
# 原文段与译文段的样式**完全相同**（都是同一个 styles.xml 里的样式），
# 无法靠样式区分，只能靠「语言 + 位置」：
# 紧跟原文段之后的、连续的不含中文文本段判为译文。
# 原文里本身不含中文的句子（全英文术语等）会被保留下来的条件是它直接跟在时间轴后。

_TS_RE = re.compile(r"^\s*\d{2}:\d{2}:\d{2}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_P_OPEN_RE = re.compile(r"<w:p(?:\s[^>]*)?/?>")


def _para_spans(xml):
    """返回 [(start, end, seg)]：每段在 XML 里的精确字符区间。

    `<w:p` 不会嵌套 `<w:p`，所以「开标签 → 其后的第一个 `</w:p>`」即一段。
    """
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


def strip_translation(docx: Path):
    """就地剥掉 docx 里的译文段落，返回删除条数（0 = 无译文段）。

    只按字符区间删除段落本身，不改动任何其他 XML —— 段落之间的表格、
    图片、节属性等全部原样保留。
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
        if prev == "CJK" or in_trans:      # 紧跟原文 / 译文带的续行 -> 译文
            drop.append((s, e))
            in_trans = True
            n += 1
            continue
        prev, in_trans = "OTH", False      # 独立出现的不含中文段：是原文，留着

    if not n:
        return 0
    for s, e in reversed(drop):            # 从后往前删，前面的偏移不受影响
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
