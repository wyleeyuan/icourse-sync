"""配置与全局常量。

只依赖标准库 —— 其它模块都可以安全地 import 它。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


# 仓库根目录：本文件在 src/iccourse/ 下，往上两级才是项目根
# （config.json / cookie.txt 都在那里，路径算错会读不到配置）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEST = "~/Desktop/语音课件"
# 已降级到拆分兜底的课次，隔这么多天才重试一次「升级回合并版」。
# 理由：那类课次的两个导出 key 都已被服务端缓存锁死，短期内不会变好，
# 每次都重试等于每轮白跑一遍导出 + 重下兜底；但万一服务端缓存哪天刷新了，
# 隔几天自动再试一次就能自己升级回来（`--force` 可立即重试）。
DEFAULT_RETRY_FALLBACK_DAYS = 7

DEFAULT_CONFIG = {
    "dest": DEFAULT_DEST,
    "tenant_code": "222",
    "retry_fallback_days": DEFAULT_RETRY_FALLBACK_DAYS,
    "courses": {"include": [], "exclude": []},
    "auth": {
        "source": "auto",                  # auto | bsk | cookie | browser
        "bsk_path": "~/.local/bin/bsk",
        "cookie_file": "cookie.txt",
        "browser": "edge",
    },
}

# ============================================================ 配置

def expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(explicit=None):
    """读取配置。返回 (config, 来源路径 or None)。"""
    cands = []
    if explicit:
        cands.append(expand(explicit))
    cands.append(PROJECT_ROOT / "config.json")
    for c in cands:
        if c.is_file():
            try:
                raw = json.loads(c.read_text(encoding="utf-8"))
            except Exception as e:
                raise SystemExit("配置文件解析失败 %s：%s" % (c, e))
            return _deep_merge(DEFAULT_CONFIG, raw), c
    return json.loads(json.dumps(DEFAULT_CONFIG)), None


def write_default_config(path: Path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg = {"_readme": "只改 dest 就能用；其余项见 config.example.json 说明",
           **cfg}
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
