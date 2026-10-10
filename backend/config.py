"""集中配置。所有外部依赖（网关地址、密钥、模型名）只在这里读取一次。

来源按优先级：
  1. 网页里「⚙ 模型设置」保存的 data/user_config.json（本机文件，已被 .gitignore 忽略）
  2. .env / 环境变量
  3. 代码默认值

SETTINGS 是一个可变的单例：设置改了，所有 `from .config import SETTINGS` 的模块立刻看到新值，
不用重启服务。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
SCRIPTS_DIR = DATA_DIR / "scripts"
GAMES_DIR = DATA_DIR / "games"
WEB_DIR = ROOT / "web"
USER_CONFIG = DATA_DIR / "user_config.json"

_EDITABLE = ("base_url", "api_key", "model_chat", "model_reason", "bypass_proxy")


@dataclass
class Settings:
    base_url: str
    api_key: str
    model_chat: str
    model_reason: str
    mock: bool
    timeout: float
    bypass_proxy: bool
    source: str = "env"          # env / user

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-你的")


def _forced_mock() -> bool:
    return os.getenv("SG_MOCK", "0") == "1"


def load_settings() -> Settings:
    s = Settings(
        base_url=os.getenv("SG_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        api_key=os.getenv("SG_API_KEY", ""),
        model_chat=os.getenv("SG_MODEL_CHAT", "deepseek-chat"),
        model_reason=os.getenv("SG_MODEL_REASON", "deepseek-reasoner"),
        mock=False,
        timeout=float(os.getenv("SG_TIMEOUT", "120")),
        # 默认走系统代理（访问海外服务通常需要）。如果接口在内网、而本机设了
        # HTTP_PROXY（Clash / V2Ray 之类），请求会被送出隧道外而返回 502——
        # 这时设 SG_BYPASS_PROXY=1 让程序忽略代理直连。
        bypass_proxy=os.getenv("SG_BYPASS_PROXY", "0") == "1",
    )
    s.mock = _forced_mock() or not s.configured     # 没配密钥时自动退化到离线模式
    return s


SETTINGS = load_settings()
_ENV_SNAPSHOT = {k: getattr(SETTINGS, k) for k in _EDITABLE}


class ConfigError(ValueError):
    pass


def _clean(patch: dict) -> dict:
    """校验并规整用户填的值。空字符串 = 不改（密钥尤其如此：界面从不回显它）。"""
    out: dict = {}
    if patch.get("base_url") not in (None, ""):
        u = str(patch["base_url"]).strip().rstrip("/")
        for tail in ("/chat/completions", "/completions"):
            if u.endswith(tail):
                u = u[: -len(tail)]
        if not (u.startswith("http://") or u.startswith("https://")):
            raise ConfigError("接口地址要以 http:// 或 https:// 开头，例如 https://api.deepseek.com/v1")
        out["base_url"] = u
    if patch.get("api_key") not in (None, ""):
        k = str(patch["api_key"]).strip()
        if not k.isascii() or any(c.isspace() for c in k) or len(k) < 8:
            raise ConfigError("密钥格式不对：应是一串不含空格和中文的字符（至少 8 位）")
        out["api_key"] = k
    for f in ("model_chat", "model_reason"):
        if patch.get(f) not in (None, ""):
            m = str(patch[f]).strip()
            if len(m) > 80 or any(c.isspace() for c in m):
                raise ConfigError("模型名不能含空格，也不要太长")
            out[f] = m
    if isinstance(patch.get("bypass_proxy"), bool):
        out["bypass_proxy"] = patch["bypass_proxy"]
    return out


def _apply(values: dict, source: str) -> None:
    for k, v in values.items():
        setattr(SETTINGS, k, v)
    SETTINGS.source = source
    SETTINGS.mock = _forced_mock() or not SETTINGS.configured


def _saved() -> dict:
    try:
        return json.loads(USER_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_user_config(patch: dict) -> None:
    """保存用户在网页里填的设置：立即生效，并落盘（下次启动仍在）。"""
    vals = _clean(patch)
    if not vals:
        raise ConfigError("没有要保存的内容")
    merged = {**_saved(), **vals}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USER_CONFIG.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(USER_CONFIG, 0o600)         # 里面有密钥；Windows 上基本无效，但无害
    except OSError:
        pass
    _apply(vals, "user")


def reset_user_config() -> None:
    """丢掉网页里保存的设置，回到 .env / 环境变量。"""
    try:
        USER_CONFIG.unlink()
    except FileNotFoundError:
        pass
    _apply(_ENV_SNAPSHOT, "env")


def public_view() -> dict:
    """给前端看的设置。**绝不含密钥本身**，只给尾号，让人知道填过没有。"""
    k = SETTINGS.api_key if SETTINGS.configured else ""
    return {
        "base_url": SETTINGS.base_url,
        "model_chat": SETTINGS.model_chat,
        "model_reason": SETTINGS.model_reason,
        "bypass_proxy": SETTINGS.bypass_proxy,
        "has_key": bool(k),
        "key_hint": ("…" + k[-4:]) if len(k) >= 8 else "",
        "mock": SETTINGS.mock,
        "forced_mock": _forced_mock(),
        "source": SETTINGS.source,
    }


# 启动时叠加上次在网页里保存的设置
_saved_vals = _saved()
if _saved_vals:
    try:
        _apply(_clean(_saved_vals), "user")
    except ConfigError:
        pass

GAMES_DIR.mkdir(parents=True, exist_ok=True)
