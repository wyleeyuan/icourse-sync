"""产物命名与覆盖判定：目录结构、文件路径、已完整/兜底状态、旧文件迁移。

命名约定：``<dest>/<课程名>/<课程名>_<课次>_语音课件.docx``
锁死课次的兜底产物同目录，后缀换成 ``_字幕.docx`` / ``_课件.pptx``。
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

from iccourse.documents import inspect_docx, inspect_pptx, safe_name



# ============================================================ 目录布局

FLAT_RE = re.compile(r"^(?P<course>.+?)_(?P<sub>\d{4}-\d{2}-\d{2}第[^_]*节)_语音课件\.docx$")


def course_dir(dest: Path, course_title):
    return dest / safe_name(course_title)


def target_path(dest: Path, course_title, sub_title) -> Path:
    """主产物：<dest>/<课程名>/<课程名>_<课次>_语音课件.docx"""
    fn = "%s_%s_语音课件.docx" % (safe_name(course_title), safe_name(sub_title))
    return course_dir(dest, course_title) / fn


def subtitle_path(dest: Path, course_title, sub_title) -> Path:
    """兜底产物①：<dest>/<课程名>/<课程名>_<课次>_字幕.docx（纯语音识别原文）"""
    fn = "%s_%s_字幕.docx" % (safe_name(course_title), safe_name(sub_title))
    return course_dir(dest, course_title) / fn


def deck_path(dest: Path, course_title, sub_title) -> Path:
    """兜底产物②：<dest>/<课程名>/<课程名>_<课次>_课件.pptx（每页一张录屏截图）"""
    fn = "%s_%s_课件.pptx" % (safe_name(course_title), safe_name(sub_title))
    return course_dir(dest, course_title) / fn


def clean_fallback(dest: Path, course_title, sub_title):
    """合并版到手后，清掉同课次此前遗留的拆分兜底产物（目录里只留最好的形态）。"""
    gone = []
    for p in (subtitle_path(dest, course_title, sub_title),
              deck_path(dest, course_title, sub_title)):
        if p.exists():
            try:
                p.unlink()
                gone.append(p.name)
            except Exception as e:
                print("      清理失败 %s：%s" % (p.name, e))
    if gone:
        print("      - 已清理该课次遗留的兜底产物：%s" % "、".join(gone))
    return gone


def covered(dest: Path, course_title, sub_title):
    """该课次本地已达到的覆盖程度：'main' / 'fallback' / None。

    main     —— 有完整的语音课件（截图 + 字幕合成一份）
    fallback —— 只有兜底的两份（`_字幕.docx` + `_课件.pptx`），内容等价但拆开了
    """
    p = target_path(dest, course_title, sub_title)
    if p.exists() and inspect_docx(p)["ok"]:
        return "main"
    s = subtitle_path(dest, course_title, sub_title)
    d = deck_path(dest, course_title, sub_title)
    if s.exists() and d.exists() and inspect_docx(s)["ok"] and inspect_pptx(d)["ok"]:
        return "fallback"
    return None


def fallback_age_days(dest: Path, course_title, sub_title):
    """兜底产物距今多少天（取两份里最新的 mtime）；都不存在则返回 None。

    用来给「重试修复」做节流：兜底是"次优形态"，但对应课次的导出 key
    多半已被服务端缓存永久锁死，每轮都重试纯属白跑。
    """
    ts = []
    for p in (subtitle_path(dest, course_title, sub_title),
              deck_path(dest, course_title, sub_title)):
        if p.exists():
            try:
                ts.append(p.stat().st_mtime)
            except OSError:
                pass
    if not ts:
        return None
    return (time.time() - max(ts)) / 86400.0


def migrate_flat(dest: Path):
    """把早期平铺在根目录的课件移进各自的课程子目录（幂等）。"""
    moved = 0
    if not dest.is_dir():
        return 0
    for p in sorted(dest.glob("*_语音课件.docx")):
        if not p.is_file():
            continue
        m = FLAT_RE.match(p.name)
        if not m:
            continue
        sub = course_dir(dest, m.group("course"))
        new_p = sub / p.name
        if new_p.exists():
            continue
        try:
            sub.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(new_p))
            moved += 1
            print("      迁移 -> %s/  %s" % (sub.name, p.name))
        except Exception as e:
            print("      迁移失败 %s：%s" % (p.name, e))
    return moved

