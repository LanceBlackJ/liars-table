"""OpenAI 兼容的 LLM 客户端。

刻意做成 provider 无关：只依赖 `POST {base_url}/chat/completions` 这一个端点，
换任何 OpenAI 兼容网关（DLUT / DeepSeek / OpenAI / 本地 vLLM）都只改 .env。

离线模式（SG_MOCK=1 或未配密钥）返回预置台词，用于无网络时跑通整个游戏流程。
"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from collections import deque
from typing import Any

import httpx

from .config import SETTINGS

Message = dict[str, str]


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 限流器
#
# 有的网关限制 1 分钟最多约 20 次请求，超了返回 429。
# 一局狼人杀的一个昼夜循环就要 ~10 次调用，不做节流会直接打爆。
# 这里用滑动窗口在客户端主动排队，宁可等，也不要让一局游戏中途炸掉。
# 上限可用 SG_RATE_PER_MIN 调整（服务商不限流的话可以调大）。
# --------------------------------------------------------------------------

RATE_LIMIT = int(os.getenv("SG_RATE_PER_MIN", "18"))   # 默认 18，给 20 次/分钟的网关留 2 次余量
RATE_WINDOW = 60.0

# 单次调用最多试几回。给到 10 是因为最常见的中断是家里网抖一下、
# 或者 WiFi 切换，几秒到几十秒就恢复了——这种情况不该让玩家重开一局。
MAX_ATTEMPTS = 10


def _backoff(attempt: int) -> float:
    """指数退避，封顶 20 秒；加抖动避免多个请求同时重试。

    10 次的累计等待约 100 秒，足够熬过绝大多数瞬时断网，
    又不至于让玩家在真的没网时干等好几分钟。
    """
    return min(20.0, 1.5 * (2 ** attempt)) + random.uniform(0, 1.5)

_lock = threading.Lock()
_calls: deque[float] = deque()


def _throttle() -> None:
    """滑动窗口限流。达到上限就阻塞到最早那次调用滑出窗口。"""
    while True:
        with _lock:
            now = time.monotonic()
            while _calls and now - _calls[0] >= RATE_WINDOW:
                _calls.popleft()
            if len(_calls) < RATE_LIMIT:
                _calls.append(now)
                return
            wait = RATE_WINDOW - (now - _calls[0]) + 0.05
        time.sleep(min(wait, RATE_WINDOW))


def rate_state() -> dict[str, Any]:
    """给前端显示"还能发几次"用。"""
    with _lock:
        now = time.monotonic()
        recent = [t for t in _calls if now - t < RATE_WINDOW]
        oldest = recent[0] if recent else None
        return {
            "limit": RATE_LIMIT,
            "used": len(recent),
            "resets_in": round(RATE_WINDOW - (now - oldest), 1) if oldest else 0.0,
        }


# --------------------------------------------------------------------------
# 离线兜底台词
# --------------------------------------------------------------------------

_MOCK_SPEECH = [
    "我那会儿确实不在场，你们要问就问，我没什么好瞒的。",
    "等一下——你刚才说你在厨房？可我记得那个时间厨房是黑的。",
    "这件事我不想多谈。跟案子无关。",
    "我看未必。有人在急着把话题往我身上引，这本身就很可疑。",
    "我承认我隐瞒了一点事，但那和他的死没有关系。",
    "先别急着下结论，线索还没摆全呢。",
]


def _mock_reply(messages: list[Message], json_mode: bool) -> str:
    if json_mode:
        return json.dumps({"_mock": True, "note": "离线模式占位"}, ensure_ascii=False)
    seed = sum(len(m.get("content", "")) for m in messages)
    rng = random.Random(seed)
    return rng.choice(_MOCK_SPEECH)


# --------------------------------------------------------------------------
# 真实调用
# --------------------------------------------------------------------------


def chat(
    messages: list[Message],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 1200,
    json_mode: bool = False,
) -> str:
    """同步调用 chat/completions，返回正文字符串。"""
    if SETTINGS.mock:
        return _mock_reply(messages, json_mode)

    payload: dict[str, Any] = {
        "model": model or SETTINGS.model_chat,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # 并非所有网关都支持；不支持时靠 extract_json 兜底
        payload["response_format"] = {"type": "json_object"}

    url = f"{SETTINGS.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {SETTINGS.api_key}",
        "Content-Type": "application/json",
    }

    # 客户端主动排队，再叠加退避重试。
    #
    # 两类失败都要重试，而且要给够次数：
    #   · 连不上（断网、WiFi 切换、网关抽风）——原来是直接抛出，
    #     结果网络抖一下整局就废了。这是最常见的中断原因。
    #   · 429 限流。
    # 一局游戏要几十次调用，任何一次失败都会打断玩家，
    # 所以宁可在这里多等一会儿，也别把错误抛到上层。
    last_err = ""
    resp = None
    for attempt in range(MAX_ATTEMPTS):
        _throttle()
        try:
            # trust_env=False 让 httpx 忽略 HTTP_PROXY/HTTPS_PROXY（SG_BYPASS_PROXY=1 时）。
            # 内网接口走本地代理（Clash 等）会被转发到隧道外面，必然 502。
            with httpx.Client(
                timeout=SETTINGS.timeout, trust_env=not SETTINGS.bypass_proxy
            ) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            resp = None
            if attempt == MAX_ATTEMPTS - 1:
                break
            time.sleep(_backoff(attempt))
            continue

        if resp.status_code != 429:
            break

        last_err = resp.text[:200]
        if attempt < MAX_ATTEMPTS - 1:
            # 限流要等窗口滑出去，退避比普通网络错误给得长
            time.sleep(min(RATE_WINDOW, 8 * (attempt + 1)) + random.uniform(0, 2))

    if resp is None:
        raise LLMError(
            f"连不上 {SETTINGS.base_url}，已重试 {MAX_ATTEMPTS} 次：{last_err}\n"
            "网络恢复后可以直接继续这一局——进度已经存下来了。"
        )
    if resp.status_code == 429:
        raise LLMError(f"网关持续限流（429），已重试 {MAX_ATTEMPTS} 次：{last_err}")

    if resp.status_code == 502:
        raise LLMError(
            f"网关返回 502（{SETTINGS.base_url}）。最常见的原因是请求被本地代理"
            "（Clash/V2Ray 等，HTTP_PROXY=127.0.0.1:7890 之类）截走了——"
            "如果接口在内网，需要直连。当前 SG_BYPASS_PROXY="
            f"{'1（已绕过代理）' if SETTINGS.bypass_proxy else '0（正在走代理，内网接口请改成 1）'}。"
        )
    if resp.status_code >= 400:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:500]}") from exc

    content = msg.get("content") or ""
    # 部分推理模型（Qwen3-*-Thinking、Qwen3-235B-A22B 等）把思考过程写进 content。
    # 不剥掉的话会被当成角色台词播出去——凶手会当场自曝。
    return strip_thinking(content)


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_OPEN = re.compile(r"^.*?</think>", re.S | re.I)


def strip_thinking(text: str) -> str:
    """剥掉推理模型的思考段。兼容闭合与被截断（只有 </think>）两种情况。"""
    out = _THINK_BLOCK.sub("", text)
    if "</think>" in out.lower():
        out = _THINK_OPEN.sub("", out, count=1)
    # 有的模型只吐 <think> 不吐闭合标签，且思考后没有正文——此时整段丢弃更安全
    if "<think>" in out.lower():
        out = out[: out.lower().index("<think>")]
    return out.strip()


def _repair(text: str) -> str:
    """修常见的 JSON 手误。

    实测最高频的一种：模型把内层的键也转义了，写成
        {"speech": \\"...\\", \\"reveal_clues\\": []}
    这不是合法 JSON，解析失败后原始串会被当成台词播出去，非常显眼。
    """
    t = text.strip()
    # 整体被多余的反斜杠转义：\" 出现次数远多于裸 "
    if t.count('\\"') >= 2 and t.count('\\"') * 2 >= t.count('"'):
        t = t.replace('\\"', '"')
    # 中文全角引号当成了 JSON 引号
    t = re.sub(r'(?<=[{,]\s)[“”](\w+)[“”]\s*:', r'"\1":', t)
    # 尾随逗号
    t = re.sub(r",\s*([}\]])", r"\1", t)
    return t


def extract_json(text: str) -> Any:
    """从模型输出里抠出 JSON。兼容 ```json 围栏、前后废话、以及常见转义手误。"""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    for candidate in (text, _repair(text)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    text = _repair(text)
    # 退化：抓第一个平衡的 { }
    start = text.find("{")
    if start == -1:
        raise LLMError(f"输出中找不到 JSON: {text[:300]}")
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise LLMError(f"JSON 括号不平衡: {text[:300]}")


def health() -> dict[str, Any]:
    """给前端 / 探测脚本用的连通性检查。"""
    if SETTINGS.mock:
        return {"ok": True, "mode": "mock", "detail": "离线模式，未调用远端"}
    try:
        reply = chat(
            [{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=16,
            temperature=0,
        )
        return {"ok": True, "mode": "live", "model": SETTINGS.model_chat, "detail": reply.strip()}
    except LLMError as exc:
        return {"ok": False, "mode": "live", "detail": str(exc)}
