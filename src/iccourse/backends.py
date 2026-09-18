"""请求后端：bsk（借浏览器会话）与 cookie（纯 HTTP）。

两者接口一致 —— open() / fetch_many(specs) / close()，
上层不关心底层是浏览器还是 urllib。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from iccourse.api import BASE_URL, UA
from iccourse.config import expand



# ============================================================ 后端

class BackendError(RuntimeError):
    pass


class BaseBackend:
    label = "?"

    def open(self):
        pass

    def fetch_many(self, specs):
        raise NotImplementedError

    def close(self):
        pass


def _extract_json(text):
    """从 bsk 输出里抠 JSON（输出带版本提示前缀，且 JSON 可能是多行缩进的）。

    注意：要按「最先出现的开括号」决定用哪一对。若固定先试 `{`...`}`，
    形如 `[{"a":1}]` 的数组会被截成内部对象，静默返回错误的结构。
    """
    starts = []
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        if i >= 0:
            starts.append((i, closer))
    for i, closer in sorted(starts):
        j = text.rfind(closer)
        if j <= i:
            continue
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            continue
    return None


JS_BATCH = r"""
(async () => {
  const urls = __URLS__;
  const fns = __FNS__;
  const out = [];
  for (let i = 0; i < urls.length; i++) {
    let status = -1, body = '';
    try {
      const r = await fetch(urls[i], { credentials: 'include' });
      status = r.status;
      body = await r.text();
    } catch (e) {}
    let j = null;
    try { j = JSON.parse(body); } catch (e) {}
    let res;
    try { res = fns[i](j, status); }
    catch (e) { res = { __extract_error: String(e).slice(0, 80) }; }
    out.push(res);
  }
  return JSON.stringify(out);
})()
"""


def _resolve_bsk(p: Path) -> Path:
    """把配置里的 bsk 路径解析成真实可执行文件。

    配置默认值 `~/.local/bin/bsk` 是 macOS 的安装位置；Windows 上 npm 全局装的
    是 `bsk.cmd`，不在这里。所以依次尝试：原路径 → 按名字查 PATH → 补常见扩展名。
    都没命中就原样返回（交给后续 probe 判否，不会抛异常）。
    """
    if p.is_file():
        return p
    found = shutil.which(p.name)
    if found:
        return Path(found)
    for ext in (".cmd", ".exe", ".bat"):
        cand = Path(str(p) + ext)
        if cand.is_file():
            return cand
    return p


class BskBackend(BaseBackend):
    """通过 bsk（BrowserSkill CLI）复用浏览器里已有的登录态。"""

    label = "bsk"

    def __init__(self, bsk_path, tenant_code):
        self.path = _resolve_bsk(expand(bsk_path))
        self.tenant_code = tenant_code
        self.session = None

    def _run(self, args, timeout=180):
        env = dict(os.environ)
        env["PATH"] = str(self.path.parent) + os.pathsep + env.get("PATH", "")
        cmd = [str(self.path)] + args
        # Windows 上 npm 装出来的是 bsk.cmd —— 它没有可执行映像头，
        # 直接 CreateProcess 会报「不是有效的应用程序」，必须经 cmd /c 启动。
        if sys.platform == "win32" and self.path.suffix.lower() in (".cmd", ".bat"):
            cmd = ["cmd", "/c"] + cmd
        try:
            p = subprocess.run(cmd, capture_output=True,
                               text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise BackendError("bsk 调用超时：bsk %s" % " ".join(args[:2]))
        if p.returncode != 0:
            raise BackendError("bsk 失败(%d)：%s"
                               % (p.returncode, (p.stderr or p.stdout)[-400:]))
        return p.stdout

    def probe(self):
        """静默探测可用性（不抛异常）。"""
        if not self.path.is_file():
            return False
        try:
            out = self._run(["status"], timeout=25)
        except Exception:
            return False
        return "daemon" in out

    def open(self):
        d = _extract_json(self._run(["session", "start", "--json"], timeout=60))
        if not (isinstance(d, dict) and d.get("session_id")):
            raise BackendError("无法解析 bsk session start 输出")
        self.session = d["session_id"]
        edu = "%s/education/?tenant_code=%s" % (BASE_URL, self.tenant_code)
        self._run(["navigate", edu, "--session", self.session], timeout=90)
        time.sleep(6)

    def fetch_many(self, specs):
        if not specs:
            return []
        js = (JS_BATCH
              .replace("__URLS__", json.dumps([s["url"] for s in specs]))
              .replace("__FNS__", "[" + ",".join(s["js"] for s in specs) + "]"))
        out = self._run(["evaluate", js, "--session", self.session, "--quiet"],
                        timeout=240)
        d = _extract_json(out)
        if not isinstance(d, list) or len(d) != len(specs):
            raise BackendError("evaluate 返回异常：%s" % out[-300:])
        return d

    def close(self):
        if self.session:
            try:
                self._run(["session", "stop", self.session], timeout=30)
            except Exception:
                pass
            self.session = None


class CookieBackend(BaseBackend):
    """直接用 Cookie 发 HTTP 请求，不需要浏览器。"""

    label = "cookie"

    def __init__(self, cookie, base=BASE_URL):
        self.cookie = cookie.strip()
        self.base = base

    def fetch_many(self, specs):
        return [s["py"](*self._get(s["url"])) for s in specs]

    def _get(self, url):
        """返回 (已解析的 JSON, HTTP 状态码) —— 顺序与 py 提取器的签名一致。"""
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Cookie": self.cookie,
            "Referer": BASE_URL + "/", "Accept": "application/json, text/plain, */*",
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "ignore")), r.status
        except urllib.error.HTTPError as e:
            return None, e.code
        except Exception:
            return None, -1
