"""断网中断 → 恢复后继续。

要验证的是两件事，第二件才是关键：
  1. 单次调用失败会重试（llm 层），不会一抖就抛
  2. 重试用尽真的抛出来之后，**这一手没有被消费掉**——
     队列里那个人还在原位，网恢复了继续 step 就轮到他，
     不会出现「某个角色整局一句话没说」

第 2 点原来是坏的：engine 写的是 `self._run_ai(self.queue.pop(0))`，
先出队再调用，失败了这个人就永远跳过了。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["SG_MOCK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

import _fixtures  # noqa: F401
from backend import agents, engine, llm  # noqa: E402
from backend.engine import Phase  # noqa: E402
from backend.library import load_script  # noqa: E402

print("=" * 74)

# ---- 1. llm 层：连接错误要重试满 MAX_ATTEMPTS ----
calls = {"n": 0}
real_client = httpx.Client


class DeadClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        calls["n"] += 1
        raise httpx.ConnectError("模拟断网")


httpx.Client = DeadClient
# SETTINGS 是 frozen dataclass，用 replace 造一个非 mock 的临时配置
import dataclasses
_orig_settings = llm.SETTINGS
llm.SETTINGS = dataclasses.replace(_orig_settings, mock=False)
_sleep = llm.time.sleep
llm.time.sleep = lambda s: None          # 别真等，只数次数

try:
    llm.chat([{"role": "user", "content": "x"}])
    print("✗ 应该抛 LLMError 才对")
except llm.LLMError as e:
    print(f"① 连接失败重试次数：{calls['n']}（期望 {llm.MAX_ATTEMPTS}）"
          f"　{'✓' if calls['n'] == llm.MAX_ATTEMPTS else '✗'}")
    print(f"   报错文案：{str(e).splitlines()[-1]}")

httpx.Client = real_client
llm.time.sleep = _sleep
llm.SETTINGS = _orig_settings

# ---- 2. engine 层：失败的那一手不能被吃掉 ----
print()
g = engine.new_game("sc_songfeng", "c1")
g.step()                                 # 开场 → 读本
g.act({"type": "continue"})              # 读本 → 自我介绍

# 队列头如果是真人，step() 会直接返回等待，不走 AI 分支——
# 要挑一个 AI 才测得到「调用失败时这一手有没有被吃掉」
while g.queue and g.queue[0] == g.player_char_id:
    g.act({"type": "speak", "text": "我先说两句。"})
before_queue = list(g.queue)
victim = before_queue[0]
print(f"② 轮到的 AI：{g._name(victim)}")

# 让这一手的 LLM 调用炸掉
real_act = agents.act
def boom(*a, **k):
    raise llm.LLMError("模拟断网")
agents.act = boom

try:
    g.step()
    print("   ✗ 应该抛出来")
except llm.LLMError:
    same = g.queue[:1] == [victim]
    print(f"   断网后队列头仍是他：{same} {'✓' if same else '✗'}")
    print(f"   队列长度未变：{len(g.queue) == len(before_queue)}")

# 网络恢复
agents.act = real_act
g.step()
said = [e for e in g.public_log if e["kind"] == "speech" and e.get("speaker_id") == victim]
print(f"   恢复后他发言了：{bool(said)} {'✓' if said else '✗'}")

# ---- 3. 整局能不能走完（确认没引入死循环）----
print()
guard = 0
while g.phase != Phase.DONE and guard < 600:
    guard += 1
    out = g.step()
    if out["waiting"]:
        a = out["action"]
        if a == "continue":
            g.act({"type": "continue"})
        elif a == "speak":
            g.act({"type": "speak", "text": "我说两句。"})
        elif a == "search":
            g.act({"type": "search", "location": g.script.setting.map[0]})
        elif a == "ask":
            g.act({"type": "ask", "skip": True})
        elif a == "answer":
            g.act({"type": "answer", "text": "不清楚。"})
        elif a == "private":
            g.act({"type": "private", "skip": True})
        elif a == "vote":
            g.act({"type": "vote", "target_id": "c2"})
        else:
            break

everyone = {c.id for c in g.script.characters}
spoke = {e["speaker_id"] for e in g.public_log if e["kind"] == "speech"}
print(f"③ 整局走完：{g.phase == Phase.DONE}　步数 {guard}")
print(f"   所有角色都发过言：{everyone <= spoke} "
      f"{'✓' if everyone <= spoke else '✗ 缺 ' + str(everyone - spoke)}")
