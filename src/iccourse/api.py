"""平台接口契约：URL 构造、浏览器侧 JS、响应提取器。

同一个请求要两套「提取器」：
  · js —— 在浏览器里跑（bsk 后端用），把原始响应压成小对象再传回；
  · py —— 纯 HTTP 后端（cookie）直接解析。

字段语义与实测记录见 notes/api.md（本地文档，gitignore）；CDN 域名、UA 等常量也在这里。
"""
from __future__ import annotations

import re


BASE_URL = "https://icourse.fudan.edu.cn"
EXPORT_TYPE = "1,2"      # 1=课件截图  2=语音识别原文（不含 3=译文）—— 原生形态，首选
# 修复用候选：`(sub_id, "1,2")` 被锁死成空壳时，换**别的缓存 key** 逐个重试，
# 每多一个候选就多一次"救回合并版"的机会。
#   1,2,3 —— 多一份英文译文，生成后本地剥掉即等价于 "1,2"（见 strip_translation）
#   1,2,4 —— 多一份 AI 摘要，实测导出成 Word 时未混入正文，下载即用
# ⚠️ 一个课次可能**多个 key 先后被污染**（同一节在识别跑完前被导出过两次就会这样，
#    实测 sub_id=662773：1,2 与 1,2,3 双双锁死），所以是「逐个试」，不是「只试一个」。
EXPORT_MERGE_CANDIDATES = ("1,2,3", "1,2,4")
SUBTITLE_TYPE = "2"      # 兜底①：只取语音识别原文 -> _字幕.docx
DECK_TYPE = "1"          # 兜底②：只取课件截图 -> _课件.pptx
TRANS_TYPES = ("course_live", "olive", "video")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# ============================================================ 接口与提取器
#
# 每个请求都要两套「提取器」：
#   js —— 在浏览器里跑（bsk 后端），把原始响应压成小对象后传回，
#         避免 search-trans-result 那种几 MB 的响应体穿过 bsk。
#   py —— 在 Python 里跑（cookie 后端），输入是已解析的 JSON。

def url_list():
    return BASE_URL + "/courseapi/v3/multi-search/get-my-sub-list?page=1&per_page=200"


def url_trans(sub_id):
    return BASE_URL + "/courseapi/v3/web-socket/search-trans-result?sub_id=%s&format=json" % sub_id


def url_export(sub_id, types=EXPORT_TYPE):
    return (BASE_URL + "/courseapi/v2/export/export-async?sub_id=%s"
            "&export_type=courseware&type=%s" % (sub_id, types))


def url_status(sub_id, types=EXPORT_TYPE):
    return (BASE_URL + "/courseapi/v2/export/get-courseware-status?type=%s"
            "&sub_id=%s&get_source_url=1" % (types, sub_id))


JS_LIST = """(j, s) => {
  if (!j || !j.data) return { error: 'HTTP ' + s };
  const arr = Object.values(j.data.list || {}).filter(x => x && x.course_id);
  return { count: arr.length, subs: arr.map(x => ({
    sub_id: x.sub_id, course_id: x.course_id,
    course_title: x.course_title || '', sub_title: x.sub_title || '',
    sub_type: x.sub_type || '', class_over: x.class_over || 0 })) };
}"""

JS_TRANS = """(j, s) => {
  if (!j || j.code === undefined) return { code: -1, n: 0 };
  const l = j.list || [];
  const ac = (l[0] && l[0].all_content) || [];
  return { code: j.code, n: ac.length };
}"""

JS_EXPORT = """(j, s) => ({
  code: j ? j.code : -1,
  msg: String((j && j.msg) || ('HTTP ' + s)).slice(0, 60)
})"""

JS_STATUS = """(j, s) => {
  if (!j) return { err: 'HTTP ' + s };
  if (j.code === 0 && j.data && j.data.path_name)
    return { url: j.data.path_name, name: j.data.file_name };
  const msg = String(j.msg || '');
  const hard = j.code !== 0 && j.code !== 200 && !/生成中|导出中|处理中/.test(msg);
  return hard ? { failed: true, code: j.code, msg: msg.slice(0, 60) }
              : { code: j.code, msg: msg.slice(0, 60) };
}"""


def _py_list(j, status):
    if not isinstance(j, dict) or "data" not in j:
        return {"error": "HTTP %s" % status}
    raw = ((j.get("data") or {}).get("list") or {})
    # list 里除了索引项，末尾还会混一个 "now": <int> 的时间戳，必须按类型过滤
    arr = [x for x in raw.values() if isinstance(x, dict) and x.get("course_id")]
    subs = [{"sub_id": x.get("sub_id"), "course_id": x.get("course_id"),
             "course_title": x.get("course_title") or "",
             "sub_title": x.get("sub_title") or "",
             "sub_type": x.get("sub_type") or "",
             "class_over": x.get("class_over") or 0} for x in arr]
    return {"count": len(subs), "subs": subs}


def _py_trans(j, status):
    if not isinstance(j, dict) or "code" not in j:
        return {"code": -1, "n": 0}
    lst = j.get("list") or []
    ac = ((lst[0] or {}).get("all_content") if lst and isinstance(lst[0], dict) else []) or []
    return {"code": j.get("code"), "n": len(ac)}


def _py_export(j, status):
    if not isinstance(j, dict):
        return {"code": -1, "msg": "HTTP %s" % status}
    return {"code": j.get("code"), "msg": str(j.get("msg") or "")[:60]}


def _py_status(j, status):
    if not isinstance(j, dict):
        return {"err": "HTTP %s" % status}
    if j.get("code") == 0 and (j.get("data") or {}).get("path_name"):
        return {"url": j["data"]["path_name"], "name": j["data"].get("file_name")}
    msg = str(j.get("msg") or "")
    hard = (j.get("code") not in (0, 200)) and not re.search(r"生成中|导出中|处理中", msg)
    if hard:
        return {"failed": True, "code": j.get("code"), "msg": msg[:60]}
    return {"code": j.get("code"), "msg": msg[:60]}


def spec(url, js, py):
    return {"url": url, "js": js, "py": py}
