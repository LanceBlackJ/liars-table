"""探测 OpenAI 兼容网关：列出可用模型，并逐个试跑一次最小请求。

在校园网 / 校园 VPN 环境下运行：
    conda run -n scriptgame python scripts/probe_api.py

会依次尝试 /models 列表；若网关不支持该端点，则退回到用一组常见模型名逐个试探。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = os.getenv("SG_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
KEY = os.getenv("SG_API_KEY", "")
TIMEOUT = 60.0
# 内网接口需要绕过本地代理，走 Clash/V2Ray 会 502
BYPASS = os.getenv("SG_BYPASS_PROXY", "0") == "1"

# 网关不给 /models 时的候选清单
CANDIDATES = [
    "deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1",
    "qwen-max", "qwen-plus", "qwen-turbo", "qwen2.5-72b-instruct", "qwen3-235b-a22b",
    "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-3.5-turbo",
    "claude-3-5-sonnet", "claude-sonnet-4",
    "glm-4", "glm-4-plus", "moonshot-v1-8k", "kimi-k2",
    "ERNIE-4.0-8K", "Baichuan4", "yi-large", "step-1v-8k",
    "llama-3.3-70b-instruct", "internlm2.5-latest",
]

HEADERS = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def banner(t: str) -> None:
    print(f"\n{'=' * 62}\n  {t}\n{'=' * 62}")


def list_models() -> list[str]:
    banner(f"GET {BASE}/models")
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=not BYPASS) as c:
            r = c.get(f"{BASE}/models", headers=HEADERS)
    except httpx.RequestError as e:
        print(f"  连接失败: {e}")
        return []

    print(f"  HTTP {r.status_code}")
    if r.status_code == 502:
        print("  → 502。通常是请求被本地代理截走了（内网接口需要直连）。")
        print(f"    当前 SG_BYPASS_PROXY={'1 已绕过' if BYPASS else '0 正在走代理'}；"
              "接口在内网时请设成 1。")
        return []
    if r.status_code >= 400:
        print(f"  → {r.text[:400]}")
        return []

    try:
        data = r.json()
    except Exception:
        print(f"  → 非 JSON 响应: {r.text[:400]}")
        return []

    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    if ids:
        print(f"  发现 {len(ids)} 个模型：")
        for i in ids:
            print(f"    - {i}")
    else:
        print(f"  → 响应中没有 data[].id：{json.dumps(data, ensure_ascii=False)[:400]}")
    return ids


def try_model(model: str) -> tuple[bool, str]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "回复两个字：正常"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=not BYPASS) as c:
            r = c.post(f"{BASE}/chat/completions", headers=HEADERS, json=body)
    except httpx.RequestError as e:
        return False, f"连接失败: {e}"
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    try:
        return True, r.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        return False, f"响应结构异常: {r.text[:160]}"


def main() -> None:
    print(f"网关: {BASE}")
    print(f"密钥: {'已配置 (' + KEY[:8] + '…)' if KEY else '未配置'}")
    if not KEY:
        print("\n请先复制 .env.example 为 .env 并填入 SG_API_KEY。")
        return

    found = list_models()
    targets = found or CANDIDATES
    if not found:
        banner("未能取得模型列表，改为逐个试探常见模型名")

    banner(f"逐个试跑（共 {len(targets)} 个）")
    ok: list[tuple[str, str]] = []
    for m in targets:
        good, msg = try_model(m)
        print(f"  {'✅' if good else '❌'} {m:<34} {msg[:70]}")
        if good:
            ok.append((m, msg))

    banner("结果")
    if ok:
        print(f"  可用模型 {len(ok)} 个：")
        for m, _ in ok:
            print(f"    {m}")
        print(f"\n  建议写入 .env：")
        print(f"    SG_MODEL_CHAT={ok[0][0]}")
        print(f"    SG_MODEL_REASON={ok[-1][0]}")
    else:
        print("  没有任何模型可用。")
        print("  最常见原因：请求被本地代理截走（HTTP_PROXY=127.0.0.1:7890 之类）。")
        print("  接口在内网的话需要直连，确认 SG_BYPASS_PROXY=1。")


if __name__ == "__main__":
    main()
