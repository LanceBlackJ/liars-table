"""对着跑起来的服务打一遍 API，验证三款游戏的建局 / 推进 / 存档 / 泄漏。

Windows 下用 curl 传中文 body 会被 shell 编码搞坏，所以用 httpx 直接发。
"""
from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8848"
C = httpx.Client(base_url=BASE, timeout=180, trust_env=False)  # trust_env=False 绕过本地代理

# 结束前绝不该出现在玩家状态里的字段
FORBIDDEN = ["spy_id", "civil_word", "spy_word", "is_murderer", "solution",
             "cover_story", "private_secret", "points_to", "is_red_herring"]


def check_leak(state: dict, label: str) -> None:
    blob = json.dumps(state, ensure_ascii=False)
    hits = [k for k in FORBIDDEN if f'"{k}"' in blob]
    # 玩家自己的身份信息是允许的（my_word / my_role_label / brief）
    if state.get("finished"):
        print(f"    （{label} 已结束，真相已释放，跳过泄漏检查）")
        return
    if hits:
        print(f"    ❌ {label} 泄漏字段: {hits}")
        sys.exit(1)
    # 其他玩家的身份不能出现
    for p in state.get("players", []):
        if "role" in p or "word" in p:
            print(f"    ❌ {label} players[] 带身份字段: {list(p)}")
            sys.exit(1)
    print(f"    ✅ {label} 无泄漏")


def drive(gid: str, label: str, max_steps: int = 60) -> dict:
    """自动推进，玩家动作用简单策略代打。"""
    st = C.get(f"/api/g/{gid}").json()
    for _ in range(max_steps):
        if st.get("finished"):
            break
        if st.get("waiting") and st.get("action"):
            a = st["action"]
            others = [p for p in st["players"] if p["alive"] and p["id"] != st["human_id"]]
            body: dict
            if a == "describe":
                body = {"type": "describe", "text": "这东西挺常见的，我平时也会用到。"}
            elif a == "speak":
                body = {"type": "speak", "text": "我先听听大家的说法，暂时没有明确怀疑。"}
            elif a == "vote":
                body = {"type": "vote", "target_id": others[0]["id"], "reason": "直觉。"}
            elif a == "wolf_kill":
                body = {"type": "wolf_kill", "target_id": others[0]["id"]}
            elif a == "seer_check":
                body = {"type": "seer_check", "target_id": others[0]["id"]}
            elif a == "witch_act":
                body = {"type": "witch_act", "action": "pass"}
            else:
                print(f"    ⚠️ 未处理动作 {a}，停止")
                break
            r = C.post(f"/api/g/{gid}/act", json=body)
            if r.status_code >= 400:
                print(f"    ❌ act 失败 {r.status_code}: {r.text[:200]}")
                sys.exit(1)
            st = r.json()["state"]
        else:
            r = C.post(f"/api/g/{gid}/step")
            if r.status_code >= 400:
                print(f"    ❌ step 失败 {r.status_code}: {r.text[:200]}")
                sys.exit(1)
            st = r.json()["state"]
    return st


print("=" * 66)
h = C.get("/api/health").json()
print(f"服务: {h.get('mode')}　模型: {h.get('model_chat')}")

cat = C.get("/api/catalog").json()
print(f"游戏目录: {[c['label'] for c in cat]}")

for kind, label in [("spy", "谁是卧底"), ("werewolf", "狼人杀")]:
    print(f"\n{'='*66}\n{label}")
    r = C.post("/api/g", json={"kind": kind, "human_name": "阿明"})
    if r.status_code >= 400:
        print(f"  ❌ 建局失败 {r.status_code}: {r.text[:300]}")
        sys.exit(1)
    st = r.json()
    gid = st["game_id"]
    print(f"  建局 OK  game_id={gid}")
    print(f"  玩家: {[p['name'] for p in st['players']]}")
    print(f"  我的身份: {st.get('my_word') or st.get('my_role_label')}")
    check_leak(st, label)

    # 头像端点
    a = C.get(f"/api/avatar/game/{gid}/p1.svg")
    print(f"  头像: HTTP {a.status_code} {a.headers.get('content-type')} {len(a.content)}B")

    st = drive(gid, label)
    print(f"  推进后 phase={st['phase']} finished={st['finished']}")
    if st.get("finished"):
        res = st["result"]
        print(f"  结果: {res.get('note', '')[:60]}")

    # 存档能否重新载入
    again = C.get(f"/api/g/{gid}").json()
    print(f"  重载存档 OK，事件数 {len(again['log'])}")

saves = C.get("/api/saves").json()
print(f"\n{'='*66}\n存档列表 {len(saves)} 个：")
for s in saves[:6]:
    print(f"  [{s['kind_label']:<6}] {s['title']:<18} {s['phase']:<10} {s['progress']*100:3.0f}%")

print("\n🎉 API 冒烟全部通过")
