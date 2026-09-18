"""登录态：从浏览器 Cookie 库读取、固化到 cookie.txt、现场登录。

思路照搬 fdu-yjsxk：macOS 上 Chrome/Edge 的 Cookie 库可读（含 HttpOnly
会话票），所以固化之后纯 HTTP 就能同步，不需要浏览器常驻；
bsk 退为最后的兜底（零依赖，但要开浏览器会话）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from iccourse.api import (BASE_URL, JS_LIST, _py_list, spec, url_list)
from iccourse.backends import BskBackend, CookieBackend
from iccourse.config import DEFAULT_CONFIG, PROJECT_ROOT, expand



# ------------------------------------------------------------ 登录态获取
#
# 思路照搬 fdu-yjsxk：从浏览器本地 Cookie 库读出该站点的 Cookie（含 HttpOnly
# 的会话票），固化到 cookie.txt，之后纯 HTTP 请求即可 —— 不需要浏览器常驻。
# bsk 退为最后的兜底（零依赖、但每次要开浏览器会话）。

COOKIE_DOMAIN = BASE_URL.split("//", 1)[1].split("/")[0]   # icourse.fudan.edu.cn

# macOS 上各浏览器对应的 .app 名（open -a 用）。
# 不用 webbrowser.get("edge")：Python 的 webbrowser 在 macOS 上没有 edge 控制器。
BROWSER_APPS = {"edge": "Microsoft Edge", "chrome": "Google Chrome"}

# Windows 上对应的可执行名，交给 cmd 的 start 调起（start 会查 PATH 与 App Paths）。
BROWSER_EXE_WIN = {"edge": "msedge", "chrome": "chrome"}


def _cookie_from_file(path: Path):
    """读 cookie.txt（内容是整条 Cookie 请求头）。返回 (cookie, err)。"""
    if not path.is_file():
        return None, "cookie.txt 不存在"
    try:
        txt = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return None, "读取 cookie.txt 失败：%s" % e
    txt = " ".join(txt.split())
    if not txt or "=" not in txt:
        return None, "cookie.txt 为空"
    return txt, None


def _browser_chain(browser):
    """把配置的 browser 字段规范成探测顺序（edge/chrome 都会兜底）。

    兼容 "edge" 与 ["edge","chrome"] 两种写法；
    无论怎么写，两个浏览器都会出现在链里，登录在哪个就用哪个。
    """
    if isinstance(browser, str):
        chain = [browser]
    elif isinstance(browser, (list, tuple)):
        chain = list(browser)
    else:
        chain = []
    chain = [str(b).lower() for b in chain if str(b).lower() in BROWSER_APPS]
    for b in ("edge", "chrome"):
        if b not in chain:
            chain.append(b)
    return chain


def _cookie_from_browser(browser, domain=COOKIE_DOMAIN):
    """从浏览器本地 Cookie 库读该站点的 Cookie（含 HttpOnly 会话票）。

    返回 (cookie, err)——err 用来解释"为什么没读到"。
    """
    try:
        import browser_cookie3  # 可选依赖
    except ImportError:
        return None, "browser-cookie3 未安装（pip install browser-cookie3）"
    if browser not in BROWSER_APPS or not hasattr(browser_cookie3, browser):
        return None, "不支持的浏览器：%s" % browser
    try:
        jar = getattr(browser_cookie3, browser)()
    except Exception as e:              # 权限、钥匙串、Cookie 库被占用等
        msg = str(e)
        low = msg.lower()
        # Windows 上 Chrome/Edge 自 127 起启用 App-Bound Encryption（2024-07 发布，
        # 密钥绑定到浏览器自身，第三方程序取不到，典型报错是
        # "Unable to get key for cookie decryption"。macOS / Linux 不受影响。
        if sys.platform == "win32" and ("decrypt" in low or "key" in low):
            return None, (
                "读取 %s Cookie 失败：%s\n"
                "        → Windows 上 Chrome/Edge 有 App-Bound Encryption，第三方程序读不了。\n"
                "          请改用 cookie.txt 方案（一次性）：浏览器登录 icourse → F12 →\n"
                "          Network → 随便点一个请求 → 复制请求头里的整条 Cookie →\n"
                "          粘进项目根目录的 cookie.txt（详见 notes/使用手册.md）"
                % (browser, msg))
        return None, "读取 %s Cookie 失败：%s" % (browser, e)
    picked = []
    for c in jar:
        d = (c.domain or "").lstrip(".")
        if not d:
            continue
        # icourse.fudan.edu.cn 本身 + 其父域 .fudan.edu.cn 上的 SSO 票都要
        if domain == d or domain.endswith("." + d):
            picked.append("%s=%s" % (c.name.strip(), c.value.strip()))
    if not picked:
        return None, "%s 里没有 %s 的登录 Cookie（请先在该浏览器登录一次）" % (browser, domain)
    return "; ".join(picked), None


def _save_cookie(path: Path, cookie):
    """把验证通过的 Cookie 固化，下次直接命中文件、不必再读浏览器。"""
    try:
        path.write_text(cookie, encoding="utf-8")
        return True
    except OSError:
        return False


def candidate_backends(cfg, override=None):
    """按优先级产出候选后端（惰性生成器）。override 来自命令行。

    auto 的顺序：cookie.txt → 浏览器 Cookie → bsk
      · cookie.txt 优先 —— 最近一次验证通过后固化的，命中即零开销；
      · 浏览器次之 —— 自动刷新，不用手工抓包（靠 browser-cookie3）；
      · bsk 兜底 —— 零依赖路线，代价是每次都要开浏览器会话（慢）。
    调用方逐个 open() 并用一次真实枚举验证，第一个通的即采用，
    所以前面能用就绝不会碰到后面那个（尤其不会白开 bsk 会话）。
    """
    auth = cfg.get("auth") or {}
    source = (override or auth.get("source") or "auto").lower()
    bsk_path = auth.get("bsk_path") or DEFAULT_CONFIG["auth"]["bsk_path"]
    cookie_file = expand(auth.get("cookie_file") or "cookie.txt")
    if not cookie_file.is_absolute():
        cookie_file = PROJECT_ROOT / cookie_file
    browsers = _browser_chain(auth.get("browser") or "edge")
    tenant = cfg.get("tenant_code", "222")

    def from_file():
        c, err = _cookie_from_file(cookie_file)
        if not c:
            print("      cookie.txt：%s" % err)
            return None
        return CookieBackend(c), None          # 本来就在文件里，无需再固化

    def from_browser():
        for name in browsers:
            c, err = _cookie_from_browser(name)
            if c:
                print("      从 %s 读到 Cookie（将固化到 cookie.txt）" % name)
                return CookieBackend(c), cookie_file
            print("      %s：%s" % (name, err))
        return None

    if source == "cookie":
        r = from_file()
        if r:
            yield r
        return
    if source == "browser":
        r = from_browser()
        if r:
            yield r
        return
    if source == "bsk":
        yield BskBackend(bsk_path, tenant), None
        return

    for factory in (from_file, from_browser):
        r = factory()
        if r:
            yield r
    yield BskBackend(bsk_path, tenant), None


def verify_login(backend):
    """用一次轻量枚举验证这个后端是否真的登录有效。

    返回 subs 列表（可能为空列表，也算通过）；失败返回 None。
    """
    got = backend.fetch_many([spec(url_list(), JS_LIST, _py_list)])
    d = got[0] if got else {}
    if not isinstance(d, dict) or d.get("error"):
        return None
    return d.get("subs") or []


# ------------------------------------------------------------ 现场登录

LOGIN_WAIT = 60        # 登录后最长自动重读多少秒
LOGIN_INTERVAL = 3     # 每次重读的间隔


def _open_in_browser(url, chain):
    """用配置里的浏览器打开 url；返回实际使用的浏览器名，失败返回 None。

    必须显式指定浏览器而不是用系统默认：macOS 的默认浏览器可能是 Safari，
    而脚本读不到 Safari 的 Cookie（沙盒 + TCC 限制），用户在 Safari 里登录
    等于白登录一场。统一点名打开可保证「登录用的浏览器」与
    「脚本读取的浏览器」是同一个。
    """
    if sys.platform == "darwin":
        for name in chain:
            app = BROWSER_APPS.get(name)
            if not app or not os.path.isdir("/Applications/%s.app" % app):
                continue
            try:
                subprocess.run(["open", "-a", app, url], check=True, timeout=15)
                return name
            except Exception:
                continue
        return None

    if sys.platform == "win32":
        for name in chain:
            exe = BROWSER_EXE_WIN.get(name)
            if not exe:
                continue
            try:
                # start 会把第一个带引号的参数当成窗口标题，故给一个空标题占位；
                # 收尾的空 "" 也一样要留着，否则 url 会被当成标题。
                subprocess.run('cmd /c start "" %s "%s"' % (exe, url),
                               check=True, timeout=15, shell=False)
                return name
            except Exception:
                continue
        return None

    try:                                    # Linux 等：交给系统默认浏览器
        subprocess.run(["xdg-open", url], check=True, timeout=15)
        return chain[0] if chain else None
    except Exception:
        return None


def cmd_login(cfg):
    """现场登录：打开浏览器 → 你登录 → 回车 → 自动读取并验证 → 存 cookie.txt。

    返回验证通过的 Cookie；失败抛 SystemExit。
    """
    auth = cfg.get("auth") or {}
    cookie_file = expand(auth.get("cookie_file") or "cookie.txt")
    if not cookie_file.is_absolute():
        cookie_file = PROJECT_ROOT / cookie_file
    chain = _browser_chain(auth.get("browser") or "edge")
    url = "%s/education/?tenant_code=%s" % (BASE_URL, cfg.get("tenant_code", "222"))

    used = _open_in_browser(url, chain)
    if used:
        print("正在用 %s 打开：%s" % (BROWSER_APPS[used], url))
    else:
        print("（未能自动打开浏览器）请手动访问：%s" % url)
    print("  · 未登录会自动跳到复旦统一认证，登录完回到课程页即可")
    print("  · 脚本只读 %s 的 Cookie，请在其中之一里登录" % " / ".join(chain))
    print()

    interactive = sys.stdin.isatty()
    if interactive:
        print("登录成功、能看到课程页面后，回到这里按【回车】")
        try:
            input("  >>> ")
        except EOFError:
            interactive = False
    else:
        print("（非交互环境，跳过等待，直接尝试读取）")

    # UIS 跳转完成后 cookie 落盘可能延迟，人在场时自动重读
    deadline = time.monotonic() + LOGIN_WAIT if interactive else None
    tries = 0
    while True:
        tries += 1
        cookie, why = "", ""
        for name in chain:
            c, err = _cookie_from_browser(name)
            if c:
                cookie, why = c, ""
                break
            why = err
        if cookie:
            if verify_login(CookieBackend(cookie)) is not None:
                break
            why = "读到了 Cookie，但接口仍未通过鉴权"
        if deadline is None or time.monotonic() >= deadline:
            raise SystemExit(
                "登录后仍读不到有效 Cookie（尝试 %d 次）。\n"
                "  最后的原因：%s\n"
                "  请确认是在 %s 里完成登录的（脚本只读这两个浏览器的 Cookie）；\n"
                "  若报权限错误，先完全退出该浏览器（含后台进程）再重试。"
                % (tries, why or "未知", " / ".join(chain)))
        print("  尚未就绪，%d 秒后重试（第 %d 次）..." % (LOGIN_INTERVAL, tries + 1))
        time.sleep(LOGIN_INTERVAL)

    if _save_cookie(cookie_file, cookie):
        print("✓ Cookie 已固化到 %s（下次直接命中，不必再读浏览器）" % cookie_file.name)
    print("✓ 登录态验证通过")
    return cookie
