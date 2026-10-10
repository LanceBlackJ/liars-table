"""批量造本子的共用工具。

十个本子如果各写一份 JSON，光 witnessed_by 就够写错十次。
这里把易错的部分全部收口：

  · 时间线由「谁在哪」的表自动展开，目击关系同处一地自动对称
  · 凶手是否在案发时段待在案发地点，写文件前先自检
  · 每个非凶手是否有 conceal 目标、每个地点是否有线索，也一并自检

自检不过就不写文件——错的本子进了 data 目录，
下次 validate_all 才发现，中间可能已经开了局。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "data" / "scripts"


def _act_block(cid: str, a: dict) -> dict:
    """spec 里的短写法 → ActBlock。

    obs / goals / secret / retract / restore 都是短键。目标 id 自动生成
    （{角色}a{幕}g{序号}）；retract 里可以直接写第几个基础目标（整数，从 1 起），
    不用记 id 的拼法。
    """
    retract = [f"{cid}g{r}" if isinstance(r, int) else r for r in a.get("retract", [])]
    return {
        "act": a["act"], "title": a.get("title", ""), "text": a["text"],
        "observations": list(a.get("obs", [])),
        "goals": [{"id": f"{cid}a{a['act']}g{i + 1}", "type": t, "text": txt}
                  for i, (t, txt) in enumerate(a.get("goals", []))],
        "secret_add": a.get("secret", ""),
        "retract_goals": retract,
        "restore_slots": list(a.get("restore", [])),
        "grant_clues": list(a.get("grant", [])),
    }


def _clue(c) -> dict:
    """线索：老的 7 元组，或者新的字典（能写合成线索）。

    元组：(id, 地点, 文本, 解读, 指向, 是否红鲱鱼, 难度)
    字典：{"id","loc","text","reveals","pts","red","d"}，合成线索再加
          {"synth":True,"from":["a","b"],"note":"拼成时的旁白"}（loc 不写）
          随身线索 {"granted":True}（loc 不写；由 acts 里的 grant=[id] 发给某人）
    """
    if isinstance(c, dict):
        return {
            "id": c["id"], "location": c.get("loc", ""), "text": c["text"],
            "reveals": c.get("reveals", ""), "points_to": list(c.get("pts", [])),
            "is_red_herring": bool(c.get("red", False)), "difficulty": c.get("d", 1),
            "synth": bool(c.get("synth", False)), "from_clues": list(c.get("from", [])),
            "granted": bool(c.get("granted", False)),
            "combine_note": c.get("note", ""),
        }
    i, loc, txt, rev, pts, rh, d = c
    return {"id": i, "location": loc, "text": txt, "reveals": rev,
            "points_to": pts, "is_red_herring": rh, "difficulty": d}


def build(spec: dict[str, Any]) -> dict[str, Any]:
    slots = spec["slots"]
    where = spec["where"]
    dark = set(spec.get("no_witness_slots", []))
    murderer = spec["murderer"]

    chars = []
    for c in spec["chars"]:
        # NPC：证人/工作人员，不参与投票也不需要个人目标。
        # goals 给不给都行——给了就忽略，免得每个 spec 都要单独判断。
        is_npc = bool(c.get("npc", False))
        e = {
            "id": c["id"], "name": c["name"], "public_role": c["role"],
            "avatar": {
                "hair": c.get("hair", "short"),
                "hair_color": c.get("hc", "black"),
                "skin": c.get("skin", "mid"),
                "garment": c.get("garment", "slate"),
                "accessory": c.get("acc", "none"),
            },
            "voice": c.get("voice", ""),
            "private_secret": c["secret"],
            "motive": c.get("motive", ""),
            "is_murderer": c["id"] == murderer,
            "is_npc": is_npc,
            "personality": c["persona"],
            "speech_style": c["style"],
            "sample_lines": list(c.get("lines", [])),    # 口吻范例，AI 靠它找声音
            "personal_goals": [] if is_npc else [
                {"id": f"{c['id']}g{i+1}", "type": t, "text": txt}
                for i, (t, txt) in enumerate(c["goals"])
            ],
        }
        if c["id"] == murderer:
            e["cover_story"] = c["cover"]
        e["amnesia_slots"] = list(c.get("amnesia", []))
        e["acts"] = [_act_block(c["id"], a) for a in c.get("acts", [])]
        chars.append(e)

    timeline = []
    for cid, locs in where.items():
        for slot, loc in zip(slots, locs):
            wit = [] if slot in dark else [
                o for o, ol in where.items()
                if o != cid and ol[slots.index(slot)] == loc
            ]
            timeline.append({
                "slot": slot, "char": cid, "location": loc,
                "action": spec["actions"][(cid, slot)],
                "witnessed_by": wit,
                "observed": spec.get("observed", {}).get((cid, slot), []),
                "is_crime_action": cid == murderer and slot == spec["death_slot"],
            })

    return {
        "script_id": spec["id"],
        "meta": {
            "title": spec["title"], "player_count": len(spec["chars"]),
            "difficulty": spec.get("difficulty", "medium"),
            "estimated_minutes": spec.get("minutes", 80),
            "template": "closed_circle", "tagline": spec["tagline"],
            "acts": spec.get("acts", 2),
            "search_budget": spec.get("search_budget", []),
            "tags": spec.get("tags", []),
            "author": spec.get("author", ""),
            "spec_version": 1,
        },
        "setting": {
            "public_background": spec["background"],
            "map": spec["map"], "time_slots": slots,
        },
        "case": {
            "victim": spec["victim"], "death_slot": spec["death_slot"],
            "death_location": spec["death_location"],
            "weapon": spec["weapon"], "cause": spec["cause"],
            "body_found_by": spec["found_by"], "body_found_slot": spec["found_slot"],
        },
        "characters": chars,
        "timeline": timeline,
        "clues": [_clue(c) for c in spec["clues"]],
        "interludes": spec.get("interludes", []),
        "solution": {
            "murderer": murderer,
            "reasoning_chain": spec["chain"],
            "minimum_clues_required": spec["key_clues"],
            "questions": spec.get("questions", []),
            "question_pass": spec.get("question_pass", 0.6),
        },
    }


def self_check(d: dict) -> list[str]:
    """写文件前先拦一道。这几条都是我实际写错过的。"""
    bad = []
    m = d["solution"]["murderer"]
    slot = d["case"]["death_slot"]
    loc = d["case"]["death_location"]

    at = next((t for t in d["timeline"]
               if t["char"] == m and t["slot"] == slot), None)
    if at is None:
        bad.append(f"凶手 {m} 缺少案发时段 {slot} 的时间线")
    elif at["location"] != loc:
        bad.append(f"凶手 {m} 在 {slot} 位于「{at['location']}」，案发地点却是「{loc}」")

    # 合法地点 = 基础地图 + 幕间会解锁的地点。忘了这条的话，
    # 想在「解锁后新地点」放线索会被下面这个检查拦下来，看着像 bug，
    # 其实是没把 unlock_location 的目标地点算进去。
    unlockable = {
        it.get("location", "") for it in d.get("interludes", [])
        if it.get("effect") == "unlock_location" and it.get("location")
    }
    locs = set(d["setting"]["map"]) | unlockable
    covered = {k["location"] for k in d["clues"]}
    for x in locs - covered:
        bad.append(f"地点「{x}」没有任何线索，搜证会空手")
    for k in d["clues"]:
        if k.get("synth") or k.get("granted"):
            continue                         # 合成/随身线索没有地点
        if k["location"] not in locs:
            bad.append(f"线索 {k['id']} 的地点「{k['location']}」不在地图内，也不是任何幕间解锁的地点")

    if not any(k["is_red_herring"] for k in d["clues"]):
        bad.append("没有红鲱鱼线索，本子会过于直白")

    cids = {c["id"] for c in d["characters"]}
    for c in d["characters"]:
        if c["is_murderer"] or c.get("is_npc"):
            continue
        if not any(g["type"] == "conceal" for g in c["personal_goals"]):
            bad.append(f"{c['name']} 没有 conceal 目标，缺少隐瞒动机")
    for k in d["clues"]:
        for t in k["points_to"]:
            if t not in cids:
                bad.append(f"线索 {k['id']} 指向不存在的角色 {t}")
    for k in d["solution"]["minimum_clues_required"]:
        ids = {x["id"] for x in d["clues"]}
        if k not in ids:
            bad.append(f"关键线索 {k} 不存在")
        elif next(x for x in d["clues"] if x["id"] == k)["is_red_herring"]:
            bad.append(f"关键线索 {k} 被标成了红鲱鱼——矛盾")
    # 复用后端的完整校验 + 逐幕泄漏检查。以前这里手抄了一份「简化版」，
    # 后端加一条规则这边就漂移一次；现在加规则只改一处。
    if not bad:
        bad += _backend_check(d)
    return bad


def _backend_check(d: dict) -> list[str]:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pydantic import ValidationError
    from backend import library
    from backend.models import Script
    try:
        s = Script.model_validate(d)
    except ValidationError as e:
        return [f"字段不对: {' → '.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:12]]
    out = list(library.validate_script(s))
    for act in range(1, s.meta.acts + 1):
        for c in s.characters:
            for lk in library.assert_no_leak(s, library.build_view(s, c.id, act=act)):
                out.append(f"泄漏（第 {act} 幕 · {c.name}）: {lk}")
    return out


def write_all(specs: list[dict], out_dir: Path | None = None) -> int:
    """逐个建、逐个自检，过了才写。返回成功写入的个数（供调用方判断退出码）。

    out_dir 默认是 data/scripts；投稿用 data/scripts/community。
    """
    out_dir = out_dir or SCRIPTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for spec in specs:
        d = build(spec)
        problems = self_check(d)
        if problems:
            print(f"❌ {spec['title']}")
            for x in problems:
                print("   -", x)
            continue
        (out_dir / f"{spec['id']}.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ {d['meta']['title']}　{len(d['characters'])}人 / "
              f"时间线 {len(d['timeline'])} / 线索 {len(d['clues'])}")
        ok += 1
    print(f"\n写入 {ok}/{len(specs)} 个")
    return ok
