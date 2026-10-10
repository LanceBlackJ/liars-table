"""角色头像：纯 Python 生成 SVG，零外部依赖。

刻意不用 DiceBear 之类的外部头像 API——本项目可能跑在内网 / 复杂代理环境下，
外链图片会随机挂掉，而且头像必须和角色人设对得上（老管家该是老人，
少妇该是女性），随机种子生成的抽象图案做不到这点。

每个角色在剧本 JSON 里显式声明特征（发型/配饰/年龄/肤色/衣色），
这里把它们组装成 SVG。同样的输入永远得到同样的输出。
"""
from __future__ import annotations

from .models import AvatarSpec

# --------------------------------------------------------------------------
# 调色板 —— 为深色 UI 挑的：中低明度、彼此色相拉开，放在 #14120f 底上都认得出
# --------------------------------------------------------------------------

GARMENT = {
    "indigo": ("#3c4f7a", "#2a3a5e"),
    "crimson": ("#8c3b3b", "#6b2a2a"),
    "olive": ("#5f6b3a", "#47512a"),
    "plum": ("#6b3f63", "#52304c"),
    "teal": ("#2f6b68", "#22514f"),
    "ochre": ("#96702f", "#735424"),
    "slate": ("#4a5259", "#373d43"),
    "rust": ("#96552f", "#734024"),
    "moss": ("#3f6b4a", "#2f5138"),
    "wine": ("#6b2f45", "#512334"),
}

SKIN = {
    "light": "#e8c9a8",
    "mid": "#d4a882",
    "tan": "#bd8f66",
    "deep": "#9c6f4c",
}

HAIR_COLOR = {
    "black": "#1e1a17",
    "darkbrown": "#3a2c22",
    "gray": "#8b8378",
    "white": "#cfc8bd",
    "auburn": "#5c3320",
}


# --------------------------------------------------------------------------
# 发型：每个返回一段 SVG path/shape，画在头（cx=50, cy=42, r=19）之上
# --------------------------------------------------------------------------


def _hair(style: str, c: str) -> str:
    if style == "short":  # 短发，男性常见
        return f'<path d="M31 40c0-12 8-18 19-18s19 6 19 18c0-6-4-9-8-9-3 0-4 2-11 2s-9-2-12-2c-4 0-7 3-7 9z" fill="{c}"/>'
    if style == "buzz":  # 板寸
        return f'<path d="M31.5 41c0-11 8-17 18.5-17S68.5 30 68.5 41c-2-5-6-8-18.5-8S33.5 36 31.5 41z" fill="{c}"/>'
    if style == "bun":  # 发髻，民国女性
        return (
            f'<circle cx="50" cy="18" r="7.5" fill="{c}"/>'
            f'<path d="M30 44c0-14 9-21 20-21s20 7 20 21c0-8-5-12-8-12H38c-3 0-8 4-8 12z" fill="{c}"/>'
        )
    if style == "long":  # 披肩长发
        return (
            f'<path d="M28 46c0-15 9-23 22-23s22 8 22 23v22c-4 2-7-4-7-13 0-9-6-12-15-12s-15 3-15 12c0 9-3 15-7 13z" fill="{c}"/>'
        )
    if style == "updo":  # 盘发
        return (
            f'<ellipse cx="50" cy="19" rx="11" ry="7" fill="{c}"/>'
            f'<path d="M31 42c0-13 8-20 19-20s19 7 19 20c-1-7-6-11-19-11s-18 4-19 11z" fill="{c}"/>'
        )
    if style == "balding":  # 谢顶，老年
        return f'<path d="M31 44c0-6 2-10 5-12 0 4 2 6 4 6-1-3 0-6 2-7 4 8 20 4 24 1 2 3 3 8 3 12-1-9-8-13-19-13s-18 5-19 13z" fill="{c}"/>'
    if style == "bald":
        return ""
    if style == "cap":  # 鸭舌帽 / 工作帽
        return (
            f'<path d="M29 40c0-12 9-19 21-19s21 7 21 19z" fill="{c}"/>'
            f'<rect x="27" y="38" width="46" height="5" rx="2.5" fill="{c}"/>'
        )
    if style == "hood":  # 连帽，现代 / 极地
        return (
            f'<path d="M26 50c0-17 10-27 24-27s24 10 24 27c0-9-3-14-6-16-4-3-11-4-18-4s-14 1-18 4c-3 2-6 7-6 16z" fill="{c}"/>'
        )
    return _hair("short", c)


def _accessory(kind: str, skin: str) -> str:
    if kind == "glasses":
        return (
            '<g fill="none" stroke="#2b2621" stroke-width="2">'
            '<circle cx="42" cy="44" r="7"/><circle cx="58" cy="44" r="7"/>'
            '<path d="M49 44h2M31 42l4 1M69 42l-4 1"/></g>'
        )
    if kind == "round_glasses":  # 民国知识分子的圆框眼镜，金丝边
        return (
            '<g fill="none" stroke="#a08e6a" stroke-width="1.8">'
            '<circle cx="42" cy="44" r="6.5"/><circle cx="58" cy="44" r="6.5"/>'
            '<path d="M48.5 44h3M32 43l3.5 .5M68 43l-3.5 .5"/></g>'
        )
    if kind == "mustache":
        return '<path d="M44 55c2-1.5 4-1.5 6 0 2-1.5 4-1.5 6 0-2 2.5-4 2-6 .8-2 1.2-4 1.7-6-.8z" fill="#2b2318"/>'
    if kind == "beard":
        return (
            f'<path d="M34 46c0 14 7 22 16 22s16-8 16-22c0 8-4 12-16 12s-16-4-16-12z" fill="#5a5148"/>'
        )
    if kind == "scarf":
        return '<path d="M32 71c6-4 12-5 18-5s12 1 18 5v4H32z" fill="#8c3b3b"/>'
    if kind == "goggles":  # 极地护目镜
        return (
            '<g><rect x="30" y="37" width="40" height="11" rx="5.5" fill="#22282c" stroke="#4a5259" stroke-width="1.5"/>'
            '<rect x="34" y="39.5" width="14" height="6" rx="3" fill="#5aa8c0" opacity=".75"/>'
            '<rect x="52" y="39.5" width="14" height="6" rx="3" fill="#5aa8c0" opacity=".75"/></g>'
        )
    if kind == "earrings":
        return '<circle cx="31" cy="50" r="2.2" fill="#c9a227"/><circle cx="69" cy="50" r="2.2" fill="#c9a227"/>'
    return ""


def render(spec: AvatarSpec, size: int = 96) -> str:
    """生成一张头像 SVG。"""
    fg, bg2 = GARMENT.get(spec.garment, GARMENT["slate"])
    skin = SKIN.get(spec.skin, SKIN["mid"])
    hair_c = HAIR_COLOR.get(spec.hair_color, HAIR_COLOR["black"])
    uid = spec.hair + spec.garment + spec.skin  # 渐变 id 需唯一，避免同页多图互相污染

    # 老年角色：眼下加一点阴影，脸颊收窄
    aged = spec.hair in ("balding", "bald") or spec.hair_color in ("gray", "white")
    wrinkle = (
        '<path d="M38 52q4 2 8 0M54 52q4 2 8 0" stroke="#00000022" stroke-width="1.4" fill="none"/>'
        if aged
        else ""
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}" role="img" aria-label="{spec.hair} avatar">
<defs>
  <linearGradient id="bg{uid}" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="{bg2}"/><stop offset="1" stop-color="#15130f"/>
  </linearGradient>
  <clipPath id="cl{uid}"><circle cx="50" cy="50" r="50"/></clipPath>
</defs>
<g clip-path="url(#cl{uid})">
  <rect width="100" height="100" fill="url(#bg{uid})"/>
  <!-- 肩 / 衣 -->
  <path d="M50 66c-15 0-26 8-29 20l-3 14h64l-3-14c-3-12-14-20-29-20z" fill="{fg}"/>
  <path d="M50 66l-6 12 6 6 6-6-6-12z" fill="{bg2}" opacity=".9"/>
  <!-- 颈 -->
  <rect x="44" y="58" width="12" height="12" rx="5" fill="{skin}" opacity=".92"/>
  <!-- 头 -->
  <ellipse cx="50" cy="44" rx="19" ry="20.5" fill="{skin}"/>
  <!-- 耳 -->
  <circle cx="30.5" cy="46" r="3.6" fill="{skin}"/><circle cx="69.5" cy="46" r="3.6" fill="{skin}"/>
  {wrinkle}
  <!-- 眼 -->
  <ellipse cx="42.5" cy="44" rx="2.1" ry="2.4" fill="#241d17"/>
  <ellipse cx="57.5" cy="44" rx="2.1" ry="2.4" fill="#241d17"/>
  <!-- 眉 -->
  <path d="M38.5 38.5q4-1.8 8 0M53.5 38.5q4-1.8 8 0" stroke="{hair_c}" stroke-width="1.9" fill="none" stroke-linecap="round"/>
  <!-- 嘴 -->
  <path d="M45.5 55q4.5 3 9 0" stroke="#8a5545" stroke-width="1.8" fill="none" stroke-linecap="round"/>
  {_hair(spec.hair, hair_c)}
  {_accessory(spec.accessory, skin)}
</g>
<circle cx="50" cy="50" r="49" fill="none" stroke="#00000055" stroke-width="2"/>
</svg>'''


def fallback_spec(char_id: str, name: str) -> AvatarSpec:
    """剧本没写 avatar 字段时的兜底：按 id 稳定散列，保证同一角色永远同一张脸。"""
    h = sum(ord(ch) * (i + 7) for i, ch in enumerate(char_id + name))
    hairs = ["short", "bun", "long", "buzz", "updo", "balding"]
    garments = list(GARMENT)
    return AvatarSpec(
        hair=hairs[h % len(hairs)],
        hair_color=list(HAIR_COLOR)[h // 7 % len(HAIR_COLOR)],
        skin=list(SKIN)[h // 13 % len(SKIN)],
        garment=garments[h // 3 % len(garments)],
        accessory="none",
    )
