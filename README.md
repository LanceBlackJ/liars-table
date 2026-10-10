# AI 桌游厅

和 AI 一起玩**剧本杀 / 狼人杀 / 谁是卧底 / 探案**。剧本杀是 1 个真人 + 若干 AI，剧本库**欢迎投稿**——见 [CONTRIBUTING.md](CONTRIBUTING.md)。

核心不是"让 AI 陪你聊天"，而是**信息隔离**——每个 AI 只知道它该知道的，所以凶手真的会撒谎、狼人真的会藏、卧底真的不知道自己是卧底。

架构设计见 [DESIGN.md](DESIGN.md)。

---

## 快速开始

```bash
conda create -y -n scriptgame python=3.12
conda run -n scriptgame python -m pip install -r requirements.txt

cp .env.example .env        # 填 SG_API_KEY；也可以跳过，启动后在页面 ⚙ 里填

conda run -n scriptgame python -m uvicorn backend.main:app --port 8848 --reload
```

打开 http://localhost:8848

### 接入模型

任何 **OpenAI 兼容**的接口（支持 `POST {base_url}/chat/completions`）都行：DeepSeek、通义千问（百炼）、硅基流动、OpenAI，
或自己部署的 vLLM / Ollama 兼容端点。两种配法，二选一：

1. 页面右上角 ⚙「模型设置」里填（保存即生效，存在本机 `data/user_config.json`，不会进版本库），可「测试连接」；
2. 复制 `.env.example` 为 `.env`，填 `SG_BASE_URL` / `SG_API_KEY` / `SG_MODEL_CHAT` / `SG_MODEL_REASON`。

不配密钥（或 `SG_MOCK=1`）就是**离线模式**：AI 说预置台词，整个流程照跑，适合开发和试玩界面。

对话用快一点、中文好的模型（`SG_MODEL_CHAT`）；生成本子的设定阶段和可解性校验用推理模型（`SG_MODEL_REASON`）。
太小的模型（约 10B 以下）容易把原始 JSON 漏进台词，不建议。

---

## 四款游戏

| 游戏 | 配置 | 时长 | 信息隔离的关键 |
|---|---|---|---|
| **剧本杀** | 1 人 + 5~9 AI | 60–180 分钟 | 凶手的上下文里**没有真相全文**，只有自己做了什么、要瞒什么 |
| **狼人杀** | 6–12 人，阵营可配 | 25–45 分钟 | 狼队夜聊只对狼可见，验人结果只对预言家可见（`visible_to` 白名单） |
| **谁是卧底** | 5–10 人，8 人以上双卧底 | 15–30 分钟 | **没人被告知自己是不是卧底**，要从别人的描述里自己意识到 |
| **探案** | 1 人 + 若干 NPC | 40–70 分钟 | 每个人物三层证词，摆出对应物证才会改口；NPC 自己都不知道下一层藏着什么 |

狼人杀和谁是卧底一律用「N号」称呼，真人的座位号随机，避免 AI 学到"1号是真人"这种元信息。

**狼人杀板子**（`backend/games/werewolf.py` 的 `PRESETS`，开局面板也可自定义）：

| 人数 | 配置 |
|---|---|
| 6 | 2 狼 + 预言家 + 女巫 + 2 民 |
| 7 | 2 狼 + 预言家 + 女巫 + 猎人 + 2 民 |
| 8 | 2 狼 + 预言家 + 女巫 + 守卫 + 3 民 |
| 9 | 3 狼 + 预言家 + 女巫 + 猎人 + 3 民 |
| 10 | 3 狼 + 预言家 + 女巫 + 猎人 + 守卫 + 3 民 |
| 11 | 4 狼 + 预言家 + 女巫 + 守卫 + 白痴 + 3 民 |
| 12 | 4 狼 + 预言家 + 女巫 + 猎人 + 骑士 + 4 民 |

神职：预言家、女巫、猎人、守卫、骑士、白痴。被投票放逐的人和首夜死的人有遗言。

### 剧本库

自带三个 **5 万汉字以上**的三幕硬核厚本，其余靠 AI 生成或投稿：

| 本 | 难度 | 规模 | 核心诡计 |
|---|---|---|---|
| **冬埋** | 硬核 | 8 人 · 147 线索 · 5.2 万字 | 北岭林场雪夜寿宴。二十年前埋下的东西，今晚被人挖了出来 |
| **哑舷** | 硬核 | 9 人 · 199 线索 · 5.5 万字 | 雾锁长江的客轮。手键与自动发报机的速率差、餐厅钟快 12 分钟 |
| **锁院** | 硬核 | 9 人 · 226 线索 · 5.6 万字 | 北宋中秋夜的贡院。一只被节流的铜壶慢了 21 分钟，漏、鼓、烛三套计时互相对不上 |

每个本可以换着角色玩——你看到的信息完全不同。一个人在一局里只会读到属于自己的那几千字，剩下的要靠搜证、质询和密谈从别人嘴里拼出来。

---

## AI 生成新本 / 新案子

选本页右上角「✨ AI 生成新本」，探案页右上角「✨ AI 生成新案子」，或命令行：

```bash
python scripts/gen_script.py "民国戏院后台的一桩命案" --suspects 5 --acts 3 --difficulty hard
python scripts/gen_script.py "北宋贡院锁院夜" --case          # 剧本杀 + 同一个故事的探案
python scripts/gen_script.py "海岛灯塔看守人之死" --case-only  # 只要探案
```

分阶段现写：设定与线索规划 → 每个角色 → 行踪矩阵与行动 → 线索与合成线索 → 推理链与答卷，
最后用与手写本子**同一套自检**校验，不过就把问题交回模型修补（最多 4 轮），仍不过则不写入。

- 剧本杀本子写入 `data/scripts/community/ai_*.json`，探案案子写入 `data/cases/case_ai_*.json`（二者默认被 `.gitignore` 忽略）。
- **场景保底**：每个地点的线索数按总搜证次数摊算（3–6 条）；探案案子要求 ≥6 个场景、每个场景 ≥3 个可查热区、≥16 件物证、≥4 个人物，
  并模拟一遍"从起始场景出发能不能拿到全部关键物证、撬开每一层证词"，死局不写入。
- 接口：`POST /api/gen/script`（`with_script` / `with_case` 选生成什么），`GET /api/gen/script/{job_id}` 轮询进度。

AI 生成的比手写厚本短得多（约 1 万汉字），机制也简单。它**只保证格式、信息隔离、可解性与场景够用，不保证好玩或诡计严密**，
更适合当草稿再改。

---

## 机制

**胜负判定（剧本杀）**：阵营输赢只是一半，个人目标没达成，票对了也不算完全胜利。

| | 个人目标全达成 | 有目标没达成 |
|---|---|---|
| **阵营赢** | 完全胜利 | 惨胜 |
| **阵营输** | 虽败犹荣 | 彻底失败 |

个人目标是自由文本，复盘时把整场公开记录交给一个 LLM 裁判去读（`backend/goals.py`）；判定失败时默认算达成。

**厚本机制**（三幕本用得上，老本子不受影响）：
- **三幕**：每个角色在第二、第三幕收到一页只有自己知道的追加剧本，常常推翻他前面说过的话
- **断片**：某个角色对某些时段没有记忆，得靠别人的目击倒推
- **合成线索**：谁同时握有两条线索，谁就自动拼出第三条；按人算，所以得私聊交换，可多级嵌套
- **随身线索**：由某一幕的剧本发到某个人手里，要不要公开由他自己决定
- **证人（NPC）**：不投票、不被投，只被质询
- **分层答卷**：投票前先答 N 道选择题，纯代码判分
- **解锁新地点**：幕间事件让地图上多出可搜的地方

**游戏内核**：真相 JSON 数据模型 + 结构断言；视角切分（纯代码）+ 逐幕泄漏检测；全部游戏的状态机为纯代码，不调用 LLM。

---

## 项目结构

```
backend/
  models.py        真相 JSON 数据模型。Script（上帝视角）/ CharacterView（单角色视角）分层
  library.py       视角切分 + 结构校验 + 泄漏自检。剧本杀的信息隔离在这里
  agents.py        角色 Agent 的 prompt 构造。凶手假口供策略、防越狱段
  engine.py        剧本杀状态机
  scriptgen.py     用大模型生成剧本杀本子
  casegen.py       用大模型生成探案案子（含可解性模拟）
  config.py        配置与 ⚙ 模型设置
  llm.py           OpenAI 兼容客户端 + 限流 + <think> 剥离 + 离线 mock
  avatar.py        头像 SVG 生成器
  games/           多游戏底座（EventLog 的 visible_to 白名单）、狼人杀、谁是卧底、探案
  main.py          FastAPI。玩家可见状态的序列化（最后一道泄漏防线）
data/scripts/      本库，每个本一个 JSON；community/ 放社区投稿与 AI 生成
data/cases/        探案案件
web/               前端，无构建步骤，原生 ES module
scripts/           校验、造本、冒烟测试等工具
docs/              本子格式说明与 JSON Schema
```

## 开发常用命令

```bash
# 校验所有本（结构断言 + 逐幕逐角色泄漏检查）
python scripts/validate_all.py

# 单个本的投稿自检：5 关 + 字数统计 + 建议
python scripts/check_script.py data/scripts/community/my_script.json

# 离线打完整局，验证状态机无死锁、信息隔离成立
python scripts/smoke_game.py --all
python scripts/smoke_new_games.py

# 对着跑起来的服务打一遍 API
python scripts/api_smoke.py
```

Windows 控制台默认是 GBK，脚本里有 emoji 时加环境变量 `PYTHONIOENCODING=utf-8`。

---

## 自己写一个本

```bash
python scripts/make_template.py                                        # 生成一个能玩的迷你模板
cp data/scripts/community/_template.json data/scripts/community/my_script.json
python scripts/check_script.py data/scripts/community/my_script.json   # 唯一的关卡
```

格式的每个字段见 [docs/SCRIPT_SPEC.md](docs/SCRIPT_SPEC.md)，编辑器补全用 `docs/script.schema.json`，投稿流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。

校验器强制的硬约束：
- `timeline` 必须是完整矩阵：角色数 × 时段数，一格不留空
- 目击关系必须双向一致
- 凶手必须在案发时段出现在案发地点
- 每个地点至少 1 条线索；至少 1 条红鲱鱼，且关键线索里不能混红鲱鱼
- **每个非凶手都必须有 `conceal` 类个人目标**——没有隐瞒动机，一局会变成"大家都说真话，十分钟结案"
- 凶手必须有 `cover_story`（预生成假口供）——不给凶手预设口供，它会现编，三轮追问就自相矛盾

写完记得在 `web/bgm.js` 的 `SCRIPT_TRACK` 里给它配一首曲子（不配则用默认曲目）。

---

## 设计上几个刻意的决定

**视角切分不用 LLM。** LLM 做切分会"顺手"多给或漏给信息，而且不可复现。信息隔离必须是确定性的，所以是纯代码 + 泄漏断言。

**流程控制不用 LLM。** "轮到谁发言""票数够不够""这条线索归谁"必须 100% 确定。LLM 只负责"说什么"。

**凶手 AI 招不出来，靠的是架构。** 不只在 prompt 里写"不许承认"——它的上下文里**根本没有真相全文**，只知道自己做了什么、要瞒什么。
prompt 是软防御，架构隔离才是硬防御。

**线索必须预生成。** 让 AI 现场编线索，编出来的必然和真相打架。线索是事实，事实必须提前锁死。

**头像和背景音乐都在本地生成。** 头像由后端用 Python 现画 SVG（不依赖外部头像 API）；背景音乐用 Web Audio 实时合成，没有音频文件。

---

## 许可

[MIT](LICENSE)
