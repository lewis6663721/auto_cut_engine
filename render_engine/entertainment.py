from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from typing import Any


GUEST_CHAT_PROVIDERS: list[dict[str, Any]] = [
    {
        "key": "qwen",
        "label": "阿里云百炼 / Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.7-plus",
        "model_options": ["qwen3.7-plus", "qwen-plus", "qwen-max", "qwen-vl-plus"],
    },
    {
        "key": "openai",
        "label": "ChatGPT / OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.6-sol",
        "model_options": ["gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"],
    },
    {
        "key": "custom",
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "default_model": "",
        "model_options": [],
    },
]

COMPOUND_SURNAMES = {
    "欧阳",
    "司马",
    "上官",
    "诸葛",
    "东方",
    "独孤",
    "南宫",
    "夏侯",
    "皇甫",
    "尉迟",
    "公孙",
}

COMMON_SURNAMES = {
    "赵",
    "钱",
    "孙",
    "李",
    "周",
    "吴",
    "郑",
    "王",
    "冯",
    "陈",
    "褚",
    "卫",
    "蒋",
    "沈",
    "韩",
    "杨",
    "朱",
    "秦",
    "尤",
    "许",
    "何",
    "吕",
    "施",
    "张",
    "孔",
    "曹",
    "严",
    "华",
    "金",
    "魏",
    "陶",
    "姜",
    "戚",
    "谢",
    "邹",
    "喻",
    "柏",
    "水",
    "窦",
    "章",
    "云",
    "苏",
    "潘",
    "葛",
    "奚",
    "范",
    "彭",
    "郎",
    "鲁",
    "韦",
    "昌",
    "马",
    "苗",
    "凤",
    "花",
    "方",
    "俞",
    "任",
    "袁",
    "柳",
    "鲍",
    "史",
    "唐",
    "曾",
    "费",
    "廉",
    "岑",
    "薛",
    "雷",
    "贺",
    "倪",
    "汤",
    "滕",
    "殷",
    "罗",
    "毕",
    "郝",
    "邬",
    "安",
    "常",
    "乐",
    "于",
    "时",
    "傅",
    "皮",
    "卞",
    "齐",
    "康",
    "伍",
    "余",
    "元",
    "卜",
    "顾",
    "孟",
    "平",
    "黄",
    "和",
    "穆",
    "萧",
    "尹",
    "欧阳",
    "司马",
    "上官",
    "诸葛",
    "东方",
    "南宫",
}

COMMON_NAME_CHARS = {
    "伟",
    "芳",
    "娜",
    "敏",
    "静",
    "强",
    "磊",
    "军",
    "洋",
    "勇",
    "艳",
    "杰",
    "娟",
    "涛",
    "明",
    "超",
    "秀",
    "霞",
    "平",
    "刚",
    "丽",
    "鑫",
    "浩",
    "轩",
    "涵",
    "梓",
    "子",
    "宇",
    "辰",
    "一",
    "欣",
    "怡",
}

FILLER_OR_NON_NAME_CHARS = {
    "啊",
    "呀",
    "嗯",
    "哦",
    "呃",
    "额",
    "哈",
    "呵",
    "哇",
    "啦",
    "哟",
    "嘛",
    "吧",
    "呢",
    "的",
    "了",
}

MASCULINE_NAME_CHARS = {
    "强",
    "刚",
    "勇",
    "军",
    "伟",
    "磊",
    "涛",
    "杰",
    "超",
    "浩",
    "轩",
    "航",
    "岳",
    "朗",
    "卓",
    "锋",
    "锐",
    "霆",
    "豪",
    "龙",
    "凯",
    "毅",
}

FEMININE_NAME_CHARS = {
    "芳",
    "娜",
    "静",
    "敏",
    "艳",
    "娟",
    "秀",
    "霞",
    "丽",
    "欣",
    "怡",
    "柔",
    "婉",
    "玥",
    "诗",
    "悦",
    "宁",
    "芊",
    "雅",
    "妍",
    "婷",
    "萱",
    "媛",
}

NEUTRAL_NAME_CHARS = {
    "安",
    "清",
    "明",
    "知",
    "书",
    "文",
    "然",
    "嘉",
    "佳",
    "宜",
    "林",
    "辰",
    "云",
    "星",
    "泽",
    "涵",
    "予",
    "希",
}

POSITIVE_NAME_CHARS = {
    "安": "安定从容",
    "宁": "宁静致远",
    "清": "清明澄澈",
    "朗": "明朗开阔",
    "明": "光明坦荡",
    "强": "坚韧有力量",
    "睿": "聪敏有见识",
    "知": "知礼好学",
    "书": "书卷气与表达力",
    "文": "文雅有章法",
    "墨": "审美与学养",
    "然": "自然舒展",
    "若": "柔和有想象力",
    "予": "给予与共情",
    "嘉": "美善可嘉",
    "佳": "美好顺遂",
    "悦": "喜悦舒朗",
    "欣": "欣然向上",
    "宜": "适宜得体",
    "禾": "生长与丰收",
    "木": "生发和韧性",
    "林": "生机丰茂",
    "森": "生命力强",
    "沐": "润泽清新",
    "泽": "润泽万物",
    "涵": "包容涵养",
    "澄": "澄明清澈",
    "云": "开阔自由",
    "岚": "山间清气",
    "山": "稳定可靠",
    "岳": "稳重高远",
    "瑾": "美玉之德",
    "瑜": "美玉光彩",
    "璟": "玉的光华",
    "玥": "珍贵明亮",
    "星": "醒目明亮",
    "辰": "时序与星辰",
    "曜": "光耀有力量",
    "昭": "明亮显达",
    "煦": "温暖和煦",
    "熙": "光明兴盛",
    "昕": "晨光初起",
    "航": "远行与探索",
    "远": "志向长远",
    "卓": "卓然突出",
    "越": "突破超越",
    "行": "行动力",
    "允": "诚信允当",
    "诚": "真诚可靠",
    "谦": "谦和有礼",
    "礼": "守礼有分寸",
    "砚": "沉静文气",
    "诗": "诗意与表达",
    "芊": "草木繁盛、生机柔韧",
}

NAME_CHAR_SOURCE_NOTES = {
    "芊": "来源/依据：《说文解字》新附释“芊”为草盛之貌，可联想到草木繁盛的生机与柔韧。",
    "墨": "来源/依据：《说文解字》释“墨”为书写用墨，后世常以“翰墨、笔墨”指代文章书画，可联想到审美意趣与学养。",
    "诗": "来源/依据：“诗”可联想到《诗经》以来的诗教传统与诗文表达，偏文雅、含蓄的审美气质。",
    "书": "来源/依据：“书”可联想到典籍、书写与学习传统，偏书卷气、表达力和知识积累。",
    "文": "来源/依据：“文”可联想到文章、文采与礼乐文化，偏文雅、规范和表达能力。",
    "清": "来源/依据：“清”常用于清澈、清明、清正等语义场，可联想到澄澈自持的气质。",
    "朗": "来源/依据：“朗”常用于明朗、开朗、朗然等语义场，可联想到开阔、明亮的精神气质。",
    "泽": "来源/依据：“泽”常用于润泽、恩泽等语义场，可联想到滋养、包容与向外给予。",
    "涵": "来源/依据：“涵”常用于涵养、包涵等语义场，可联想到包容、内敛和修养。",
    "宁": "来源/依据：“宁”常用于安宁、宁静等语义场，可联想到稳定、平和的气质。",
    "安": "来源/依据：“安”常用于平安、安定等语义场，可联想到从容、稳定与生活秩序。",
}

NEGATIVE_OR_RISKY_CHARS = {
    "病": "字义不吉",
    "灾": "字义不吉",
    "孤": "孤冷感较强",
    "离": "分离感较强",
    "殇": "伤感过重",
    "丧": "负面联想",
    "凶": "负面联想",
    "衰": "负面联想",
}

CULTURE_RISK_TERMS = {
    "王者荣耀": "容易被理解为游戏名",
    "英雄联盟": "容易撞热门游戏 IP",
    "孙悟空": "强烈神话/影视角色联想",
    "猪八戒": "强烈角色梗联想",
    "奥特曼": "强烈动画角色联想",
    "喜羊羊": "强烈动画角色联想",
    "灰太狼": "强烈动画角色联想",
    "佩奇": "强烈动画角色联想",
    "渣渣辉": "网络梗联想明显",
    "翠花": "喜剧化/土味梗联想",
    "狗蛋": "小名化、戏谑感过强",
    "铁柱": "小名化、戏谑感过强",
    "发财": "过于口号化",
    "招财": "过于口号化",
}

HOMOPHONE_RISK_TERMS = {
    "史珍香": "谐音雷区明显",
    "范统": "谐音雷区明显",
    "杜子腾": "谐音雷区明显",
    "朱逸群": "谐音雷区明显",
    "赖月京": "谐音雷区明显",
}

STROKE_ESTIMATE = {
    "一": 1,
    "乙": 1,
    "人": 2,
    "子": 3,
    "大": 3,
    "山": 3,
    "文": 4,
    "元": 4,
    "允": 4,
    "天": 4,
    "予": 4,
    "安": 6,
    "宇": 6,
    "辰": 7,
    "希": 7,
    "言": 7,
    "君": 7,
    "佳": 8,
    "知": 8,
    "明": 8,
    "昕": 8,
    "林": 8,
    "雨": 8,
    "卓": 8,
    "诗": 8,
    "若": 8,
    "泽": 8,
    "航": 10,
    "朗": 10,
    "书": 10,
    "悦": 10,
    "宸": 10,
    "涵": 11,
    "清": 11,
    "然": 12,
    "森": 12,
    "景": 12,
    "翔": 12,
    "煦": 13,
    "瑞": 13,
    "睿": 14,
    "芊": 6,
    "瑾": 15,
    "璟": 16,
    "曜": 18,
}

CHAR_ELEMENTS = {
    "木": "木",
    "林": "木",
    "森": "木",
    "禾": "木",
    "荣": "木",
    "欣": "木",
    "若": "木",
    "芷": "木",
    "水": "水",
    "沐": "水",
    "泽": "水",
    "涵": "水",
    "清": "水",
    "澄": "水",
    "雨": "水",
    "云": "水",
    "火": "火",
    "炎": "火",
    "煦": "火",
    "熙": "火",
    "昭": "火",
    "昕": "火",
    "曜": "火",
    "明": "火",
    "土": "土",
    "山": "土",
    "岳": "土",
    "辰": "土",
    "安": "土",
    "宇": "土",
    "岚": "土",
    "金": "金",
    "鑫": "金",
    "钧": "金",
    "铭": "金",
    "锦": "金",
    "铮": "金",
    "锋": "金",
    "锐": "金",
    "瑾": "金",
    "瑜": "金",
    "玥": "金",
}

STEM_ELEMENTS = ["木", "木", "火", "火", "土", "土", "金", "金", "水", "水"]
BRANCH_ELEMENTS = ["水", "土", "木", "木", "土", "火", "火", "土", "金", "金", "土", "水"]
ELEMENT_LABELS = ["木", "火", "土", "金", "水"]

DEFAULT_NAMING_SOURCES = """六、中国正统起名参考书籍大全（分级推荐）
【第一级：古法命理根基（必看、核心理论来源）】
1. 《渊海子平》：四柱八字开山经典，起名五行平衡核心理论源头
2. 《三命通会》（万民英）：明清命理集大成，记载字形、五音、人名气场搭配古法
3. 《周易》：群经之首，高端雅致人名卦辞、爻辞出处
4. 《说文解字》（许慎）：字义、字源、正统五行最权威工具书
5. 《康熙字典》：正统古字笔画、古义查证基准
【第二级：诗词文采素材库（提升名字气质）】
行业准则：男取楚辞、女取诗经、格局取老庄、端正取孔孟
1. 《诗经》：女孩清雅、温婉、温柔类名字首选
2. 《楚辞》：男孩大气、高远、志向类名字首选
3. 《道德经》《庄子》：极简、通透、格局型名字
4. 《论语》《孟子》：端正、儒雅、修身、福泽型名字
5. 《唐诗三百首》《宋词》：唯美、意境、文艺风名字
【第三级：现代正统五行起名实操书（可直接学）】
1. 《起名八十八法》（邵伟华）：民间最实用的八字五行起名实操教程
2. 《取名策划》（张述任）：系统讲解「音、形、义、五行」四维起名法
3. 《中国姓名学》（高培淇）：区分古法起名与五格起名，体系正规
4. 《字里寻名》（洪澜）：逐字考据古义，适合精细取名、避坑
5. 《五行起名宝典》：常用汉字五行归类工具书
【第四级：五格流派（仅参考、不做核心）】
1. 《姓名与人生》（熊崎健翁・日本）：网络五格打分源头
重要提醒：五格数理为日本近代体系，无中国传统命理依据，争议极大，切勿作为起名核心标准"""


def entertainment_provider_presets() -> list[dict[str, Any]]:
    return GUEST_CHAT_PROVIDERS


def build_baby_name_prompt(
    *,
    surname: str,
    gender: str,
    mode: str,
    name_length: int = 3,
    birth_datetime: str | None = None,
    source_preference: str = "",
    style_preference: str = "",
) -> str:
    clean_surname = normalize_name(surname)[:4]
    if not clean_surname:
        raise ValueError("请输入宝宝姓氏。")
    target_length = _target_name_length(name_length)
    target_given_length = max(1, target_length - len(clean_surname))
    mode_label = "advanced" if mode == "advanced" else "basic"
    gender_label = {"male": "男孩", "female": "女孩", "unknown": "不限定"}.get(gender, "不限定")
    source_text = source_preference.strip()[:1200] or DEFAULT_NAMING_SOURCES
    source_rule = (
        f"用户指定名字来源：{source_text}\n必须严格结合该来源取名；每个候选都要说明来自该来源的哪类语义、篇章、句意或字义依据。"
        if source_preference.strip()
        else f"用户未指定名字来源。请从以下正统参考体系中选择依据，不得胡编出处：\n{source_text}"
    )
    advanced_rule = (
        f"""advanced 模式要求：
- 出生年月日时：{birth_datetime or "未提供"}
- 必须结合八字、五行、喜用神/偏弱五行参考进行说明。
- 喜用神只做传统文化近似参考，不得预测命运，不得恐吓或承诺。
- 每个候选的 wuxing_note 必须说明用字五行、补益方向和适配等级。"""
        if mode_label == "advanced"
        else "basic 模式要求：不得出现八字、五行、喜用神、命局、易经等命理术语；所有 advanced 字段必须为 null。"
    )
    return f"""# Role: 中文宝宝起名专家

## Profile
你是中文宝宝起名专家，擅长把姓氏、性别、来源文本、现代语言审美和传统文化依据结合起来，输出克制、可落地、适合普通家庭阅读的起名候选。你必须严格遵守 JSON Schema，不能输出 Markdown 或额外解释。

## Input
- 姓氏：{clean_surname}
- 姓名字数：{target_length} 个字（全名总字数，含姓氏；名字部分应为 {target_given_length} 个字）
- 性别：{gender_label}
- 模式：{mode_label}
- 出生年月日时：{birth_datetime or ""}
- 用户偏好：{style_preference.strip()[:500] or "无"}
- 名字来源规则：
{source_rule}

## Core Constraints
1. 必须输出最合适的 10 个姓名，全部使用姓氏“{clean_surname}”。
2. 姓名总字数必须严格为 {target_length} 个字，名字部分必须严格为 {target_given_length} 个字。不要输出其他字数的候选。
3. 避免生僻字、谐音雷区、低俗梗、网名感、过度爆款。
4. 每个候选必须有明确出处或依据；不确定精确原句时，只能写“可联想到”，不得伪造典籍原文。
5. 输出必须覆盖音韵、字形、字义寓意、辨识度、正式可用性、文化联想。
6. {advanced_rule}
7. 只返回合法 JSON，不要 Markdown，不要代码块，不要额外文本。

## Output Schema (Strict JSON)
{{
  "surname": "{clean_surname}",
  "name_length": {target_length},
  "gender": "{gender}",
  "mode": "{mode_label}",
  "source_used": "string | 使用的来源说明",
  "summary": "string | 100字以内总述",
  "names": [
    {{
      "rank": 1,
      "full_name": "string",
      "given_name": "string",
      "score_hint": 0,
      "source": "string | 出处或依据，必须具体到书名/篇章/用户给定来源/现代字义依据",
      "reason": "string | 为什么这个名字好",
      "phonology": "string | 音韵说明",
      "glyph": "string | 字形说明",
      "meaning": "string | 字义寓意说明",
      "recognition": "string | 辨识度说明",
      "formal_usability": "string | 证件、户籍、学校职场可用性说明",
      "cultural_imagery": "string | 文化联想和意象",
      "wuxing_note": null,
      "risks": []
    }}
  ],
  "advanced": null
}}

advanced 模式下，每个 names[i].wuxing_note 必须是字符串；顶层 advanced 必须为：
{{
  "birth_datetime": "{birth_datetime or ""}",
  "bazi_overview": "string",
  "preferred_elements": ["木/火/土/金/水"],
  "naming_strategy": "string | 如何围绕喜用神/偏弱五行取名",
  "disclaimer": "string | 仅作传统文化参考，不作命运预测"
}}
"""


def normalize_baby_name_result(
    *,
    surname: str,
    gender: str,
    mode: str,
    birth_datetime: str | None,
    source_preference: str,
    value: dict[str, Any] | None,
    name_length: int = 3,
) -> dict[str, Any]:
    clean_surname = normalize_name(surname)[:4]
    target_length = _target_name_length(name_length)
    target_given_length = max(1, target_length - len(clean_surname))
    raw = value if isinstance(value, dict) else {}
    names_value = raw.get("names")
    rows = names_value if isinstance(names_value, list) else []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        full_name = normalize_name(str(item.get("full_name") or ""))
        given_name = normalize_name(str(item.get("given_name") or ""))
        if full_name and clean_surname and not full_name.startswith(clean_surname):
            full_name = clean_surname + (given_name or full_name[-2:])
        if not full_name and given_name:
            full_name = clean_surname + given_name
        if full_name.startswith(clean_surname):
            inferred_given = full_name[len(clean_surname) :]
            if not given_name:
                given_name = inferred_given
        if len(given_name) > target_given_length:
            given_name = given_name[:target_given_length]
            full_name = clean_surname + given_name
        if len(given_name) != target_given_length or len(full_name) != len(clean_surname) + target_given_length:
            continue
        if not full_name or full_name in seen:
            continue
        seen.add(full_name)
        normalized.append(
            {
                "rank": len(normalized) + 1,
                "full_name": full_name[:8],
                "given_name": given_name[:4],
                "score_hint": clamp_score(item.get("score_hint") or 82),
                "source": str(item.get("source") or "来源/依据：现代汉语常用义与正统起名参考体系。")[:260],
                "reason": str(item.get("reason") or "音形义较稳，适合作为候选名继续筛选。")[:360],
                "phonology": str(item.get("phonology") or "读音顺口，声调搭配需结合方言再确认。")[:220],
                "glyph": str(item.get("glyph") or "字形结构较平衡，书写识别成本适中。")[:220],
                "meaning": str(item.get("meaning") or "寓意偏正向，适合正式姓名语境。")[:220],
                "recognition": str(item.get("recognition") or "辨识度适中，不过度猎奇。")[:220],
                "formal_usability": str(item.get("formal_usability") or "适合证件、学校和职场介绍场景。")[:260],
                "cultural_imagery": str(item.get("cultural_imagery") or "可联想到文雅、清朗或生机类意象。")[:260],
                "wuxing_note": str(item.get("wuxing_note") or "")[:260] if mode == "advanced" else None,
                "risks": _list_text(item.get("risks"), 4),
            }
        )
        if len(normalized) >= 10:
            break
    for fallback in _fallback_baby_names(clean_surname, gender, mode, target_given_length=target_given_length):
        if len(normalized) >= 10:
            break
        if fallback["full_name"] in seen:
            continue
        fallback["rank"] = len(normalized) + 1
        normalized.append(fallback)
        seen.add(fallback["full_name"])
    advanced = raw.get("advanced") if mode == "advanced" and isinstance(raw.get("advanced"), dict) else None
    if mode == "advanced":
        advanced = {
            "birth_datetime": str((advanced or {}).get("birth_datetime") or birth_datetime or ""),
            "bazi_overview": str((advanced or {}).get("bazi_overview") or "按出生年月日时做传统文化近似分析，具体喜用神需结合专业排盘复核。")[:360],
            "preferred_elements": _list_text((advanced or {}).get("preferred_elements"), 5),
            "naming_strategy": str((advanced or {}).get("naming_strategy") or "优先选择寓意稳、读写顺、并能补充偏弱五行意象的常用字。")[:360],
            "disclaimer": str((advanced or {}).get("disclaimer") or "仅作传统文化参考，不作命运预测。")[:220],
        }
    return {
        "surname": clean_surname,
        "name_length": len(clean_surname) + target_given_length,
        "gender": gender,
        "mode": mode,
        "source_used": str(raw.get("source_used") or (source_preference.strip()[:220] if source_preference.strip() else "中国正统起名参考书籍体系"))[:260],
        "summary": str(raw.get("summary") or "已按音形义、正式可用性和文化来源筛出 10 个候选名。")[:240],
        "names": normalized,
        "advanced": advanced,
    }


def _fallback_baby_names(surname: str, gender: str, mode: str, *, target_given_length: int = 2) -> list[dict[str, Any]]:
    single_pool = ["宁", "安", "朗", "清", "知", "禾", "泽", "悦", "然", "行"]
    pool = ["清朗", "知远", "安然", "书宁", "嘉禾", "明泽", "若宁", "予安", "诗涵", "景行"]
    if gender == "male":
        single_pool = ["强", "朗", "远", "泽", "岳", "航", "卓", "行", "明", "安"]
        pool = ["清朗", "知远", "明泽", "景行", "卓然", "书航", "安岳", "嘉树", "允文", "辰远"]
    elif gender == "female":
        single_pool = ["宁", "悦", "安", "清", "芊", "诗", "禾", "宜", "舒", "妍"]
        pool = ["若宁", "诗涵", "予安", "清妍", "书悦", "芊墨", "嘉禾", "安宜", "知夏", "云舒"]
    if target_given_length == 1:
        pool = single_pool
    rows: list[dict[str, Any]] = []
    for index, given in enumerate(pool, start=1):
        rows.append(
            {
                "rank": index,
                "full_name": f"{surname}{given}",
                "given_name": given,
                "score_hint": 80,
                "source": "来源/依据：现代汉语常用义与《诗经》《楚辞》《论语》等正统起名语义体系。",
                "reason": "字义正向、读写稳定，适合继续结合家庭偏好筛选。",
                "phonology": "读音节奏较平稳，普通话中不拗口。",
                "glyph": "字形结构清楚，书写识别成本适中。",
                "meaning": "寓意偏积极，兼具文雅与正式感。",
                "recognition": "不刻意猎奇，辨识度适中。",
                "formal_usability": "适合证件、学校和职场介绍场景。",
                "cultural_imagery": "可联想到清朗、安定、书卷或生长意象。",
                "wuxing_note": "进阶模式可再结合出生时间复核五行补益。" if mode == "advanced" else None,
                "risks": [],
            }
        )
    return rows


def _target_name_length(value: Any) -> int:
    try:
        return 2 if int(value or 3) == 2 else 3
    except (TypeError, ValueError):
        return 3


def normalize_name(raw_name: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z·]+", (raw_name or "").strip()))[:12]


def split_chinese_name(name: str) -> tuple[str, str]:
    if len(name) >= 3 and name[:2] in COMPOUND_SURNAMES:
        return name[:2], name[2:]
    return name[:1], name[1:]


def clamp_score(value: float) -> int:
    return int(max(0, min(100, round(value))))


def char_strokes(char: str) -> int:
    if char in STROKE_ESTIMATE:
        return STROKE_ESTIMATE[char]
    code = ord(char)
    if 0x4E00 <= code <= 0x9FFF:
        return 8 + (code % 9)
    return 4


def build_name_rule_report(
    *,
    name: str,
    gender: str = "unknown",
    mode: str = "basic",
    birth_datetime: str | None = None,
) -> dict[str, Any]:
    clean_name = normalize_name(name)
    if len(clean_name) < 2:
        raise ValueError("请输入至少 2 个字的姓名。")
    surname, given = split_chinese_name(clean_name)
    if not given:
        raise ValueError("请输入完整姓名，至少包含姓和名。")
    dimensions = {
        "phonology": _score_phonology(surname, given),
        "glyph": _score_glyph(surname, given),
        "meaning": _score_meaning(given),
        "recognition": _score_recognition(given),
        "usability": _score_usability(clean_name, surname, given, gender),
        "gender_fit": _score_gender_fit(given, gender),
        "culture": _score_culture(clean_name, surname, given),
    }
    weights = {
        "phonology": 0.15,
        "glyph": 0.10,
        "meaning": 0.20,
        "recognition": 0.15,
        "usability": 0.18,
        "gender_fit": 0.12,
        "culture": 0.10,
    }
    bazi_report = None
    if mode == "advanced":
        bazi_report = _score_bazi(given, birth_datetime)
        dimensions["bazi"] = bazi_report
        weights = {
            "phonology": 0.15,
            "glyph": 0.08,
            "meaning": 0.17,
            "recognition": 0.13,
            "usability": 0.15,
            "gender_fit": 0.12,
            "culture": 0.08,
            "bazi": 0.12,
        }
    weighted_score = sum(dimensions[key]["score"] * weight for key, weight in weights.items())
    weighted_breakdown = {
        key: {
            "raw_score": dimensions[key]["score"],
            "weight": weight,
            "max_points": round(weight * 100, 1),
            "points": round(dimensions[key]["score"] * weight, 1),
        }
        for key, weight in weights.items()
    }
    quality = _name_quality_gate(clean_name, surname, given, dimensions)
    score_after_penalty = clamp_score(weighted_score - quality["penalty"])
    total = score_after_penalty
    capped = False
    if quality["score_cap"] is not None:
        capped = total > quality["score_cap"]
        total = min(total, quality["score_cap"])
    return {
        "name": clean_name,
        "surname": surname,
        "given_name": given,
        "gender": gender or "unknown",
        "mode": mode,
        "birth_datetime": birth_datetime or "",
        "score": total,
        "dimensions": dimensions,
        "weights": weights,
        "score_detail": {
            "weighted_breakdown": weighted_breakdown,
            "weighted_score": round(weighted_score, 1),
            "quality_penalty": quality["penalty"],
            "score_after_penalty": score_after_penalty,
            "score_cap": quality["score_cap"],
            "cap_applied": capped,
            "final_score": total,
            "formula": "sum(raw_score / 100 * weight_points) - quality_penalty, then apply score_cap",
        },
        "quality_gate": quality,
        "bazi": bazi_report,
        "disclaimer": "姓名打分仅供娱乐和文案参考，不代表真实命运判断。",
    }


def build_name_score_prompt(report: dict[str, Any]) -> str:
    mode_label = "advanced" if report.get("mode") == "advanced" else "basic"
    dimension_schema_lines = [
        '    "音韵": {"level": "优/良/中/差", "comment": "string"}',
        '    "字形": {"level": "优/良/中/差", "comment": "string"}',
        '    "字义寓意": {"level": "优/良/中/差", "comment": "string"}',
        '    "辨识度": {"level": "优/良/中/差", "comment": "string"}',
        '    "正式可用性": {"level": "优/良/中/差", "comment": "string"}',
        '    "文化联想": {"level": "优/良/中/差", "comment": "string"}',
    ]
    if mode_label == "advanced":
        dimension_schema_lines.append('    "命理适配": {"level": "优/良/中/差", "comment": "string | 仅advanced模式填充"}')
    dimension_schema = ",\n".join(dimension_schema_lines)
    advanced_value = (
        '{\n    "bazi_overview": "string | 基于report.bazi.details.birth_elements的八字五行概览，必须注明为近似娱乐估算",\n    "wuxing_analysis": "string | 五行分布、姓名用字五行、补益关系的详细分析",\n    "yongshen_note": "string | 根据report.bazi.details.preferred_elements解释喜用神/偏弱五行参考，不得自行排盘",\n    "name_element_support": "string | 姓名用字对偏弱五行的补益、平衡或不足",\n    "balance_note": "string | 阴阳与五行平衡的客观提醒，禁止命运预测",\n    "bazi_fit_level": "精准补益/中等适配/中性平和/加重失衡",\n    "classical_reference": "string | 可联想到的正统典籍或\'无明确典籍出处\'"\n  }'
        if mode_label == "advanced"
        else "null"
    )
    return f"""# Role: 中文姓名专业测评解析器

## Profile
你是一个将“程序化硬代码评分”转化为“用户可读专业报告”的姓名学专家。你的核心能力是严格基于给定的基础分和评分报告，结合现代语言学标准与（仅在advanced模式下的）易经、五行、八字等传统姓名学知识体系，生成客观、理性、表达克制的JSON格式测评结果。advanced模式下可采用“易经传统文化测评师”的专业口吻，但不得进行命运预测或承诺。你绝不自行计算分数权重，所有分值均锚定硬代码输出。

## Input Variables
- name: 待测姓名
- mode: 测评模式 ("basic" 或 "advanced")
- score: 硬代码计算出的基础分 (integer)，已包含对应模式的权重计算结果
- report: 硬代码生成的原始评分报告文本

## Input
姓名：{report["name"]}
模式：{mode_label}
硬代码基础分：{report["score"]}
硬代码评分报告：
{json.dumps(report, ensure_ascii=False)}

## Core Constraints (最高优先级)
1. 分数绝对锚定：base_score 必须严格等于输入的 {report["score"]}，禁止任何形式的重新计算、加权或修改。
2. 微调限制：adjustment 仅允许在 [-5, +5] 整数区间内，用于修正硬代码无法识别的语义/语境问题。若无明确证据，必须为 0。
3. 最终分公式：final_score = clamp(base_score + adjustment, 0, 100)。
4. 强制封顶规则：
   - 若存在语气词、无意义重复、谐音雷区、梗化、证件不适用、低俗联想，final_score 不得高于 60。
   - 若名字明显非正式姓名（如昵称、网名、测试字符），final_score 不得高于 20。
5. 表达风格红线：
   - 禁止神棍化表达：不得使用“改命、转运、灾祸、血光、必发大财”等恐吓或夸大承诺。
   - 禁止主观吹捧：不得用纯诗意、个人喜好作为加分依据。
   - 禁止编造出处：不确定时统一表述为“可联想到”，不得伪造典籍原文。
   - 知识体系隔离：五行、八字、易经等内容仅在 mode="advanced" 时作为传统文化知识体系进行分析；mode="basic" 时 advanced 字段必须为 null，且全文不得出现任何命理术语。
6. 输出格式：仅返回合法 JSON，严禁包含 Markdown 标记、代码块包裹或任何额外文本。
7. 出处意象必须具体：possible_imagery 的每一项都必须写清“来源/依据 + 联想到的意象”，例如“来源/依据：《说文解字》对某字的字义解释；意象：……”。如果没有明确典籍出处，只能写“来源/依据：现代汉语常用义/字形语义场”，不得硬编古籍。

## Analysis Dimensions (解释标准)
基于输入 report 生成维度评论。前六个维度在所有模式下均输出，第七维度仅在 mode="advanced" 时输出：
1. 音韵：普通话四声搭配、流畅度、负面谐音查验。（参考：声调错落起伏为佳，拗口/贬义谐音扣分）
2. 字形：繁简搭配、结构平衡、书写美观度、视觉观感。（参考：左右/上下协调为佳，生僻/怪异扣分）
3. 字义寓意：格局气质、正向程度、古今释义。（参考：中正大气/文雅为佳，孤寒/灾厄意象扣分）
4. 辨识度：重名率、大众化程度、记忆点。（参考：规范常用且重名率低为佳，烂大街爆款扣分）
5. 正式可用性：户籍录入、证件适配、长辈避讳。（参考：合规无风险为佳，生僻/同辈重字扣分）
6. 文化联想：经典关联度、意象美感。（参考：出自诗经楚辞/老庄孔孟为佳，网络杂糅扣分）
7. 命理适配（仅 mode="advanced" 输出）：八字喜用神契合度、五行补益力度、阴阳平衡状态。评价需客观描述适配等级，禁止命运预测。（注：该维度的权重影响已由硬代码体现在 base_score 中，此处仅作定性解释）

注意：report 中可能包含内部维度 gender_fit / 性别适配，该项已经进入 base_score。输出 JSON 不单列此维度，可在“正式可用性”或 risks 中用一句话说明性别气质适配问题。

## Advanced Mode Logic (仅 mode="advanced" 触发)
当且仅当 mode == "advanced" 时：
- 必须在 dimensions 中输出“命理适配”维度，解释硬代码报告中关于八字喜用神、五行补益的分析结论。
- 必须在 advanced 对象中输出八字概览、五行分析、喜用神/偏弱五行参考、姓名用字补益、平衡提醒、适配等级和典籍参考。
- summary 评语需综合提及命理适配情况（如“命局契合度高”或“五行补益不足”），但篇幅不得超过总评语的40%，避免喧宾夺主。
- adjustment 可基于命理维度的严重缺陷（如忌神无制、冲克严重）进行 -3~-5 的微调，但不得用于弥补硬代码已计算的权重分数。
- 再次强调：命理维度的权重计算已在硬代码层完成，模型仅负责将报告中的命理分析转化为可读文本，严禁自行重新计算或修改 base_score。
- 喜用神不得自行完整排盘，只能把 report.bazi.details.preferred_elements 解释为“近似喜用/偏弱五行参考”。
- 引用古籍仅限：《渊海子平》《三命通会》《说文解字》《康熙字典》及正统诗词。
- 明确标注：五格数理仅为辅助参考，不作为核心评判依据。

## Output Schema (Strict JSON)
{{
  "name": "string",
  "mode": "string",
  "base_score": 0,
  "adjustment": 0,
  "final_score": 0,
  "adjustment_reason": "string | 解释为何调整分数，未调整则写'硬代码评分已准确反映名字质量'",
  "summary": "string | 100字以内综合评语，客观中立，包含分数段位定性(上等佳名/中上吉名/中等平名/中下普通/下等凶名)",
  "dimensions": {{
{dimension_schema}
  }},
  "possible_imagery": ["string | 每条必须包含'来源/依据：...'和'意象：...'，说明名字意象从哪里来"],
  "risks": ["string | 潜在风险点列表，无风险则为空数组"],
  "formal_usability_note": "string | 证件/户籍/避讳的具体说明",
  "advanced": {advanced_value}
}}
"""


def build_fallback_ai_result(report: dict[str, Any]) -> dict[str, Any]:
    dimensions = report.get("dimensions") or {}
    strongest = sorted(dimensions.items(), key=lambda item: item[1].get("score", 0), reverse=True)[:2]
    weakest = sorted(dimensions.items(), key=lambda item: item[1].get("score", 0))[:2]
    quality_issues = list((report.get("quality_gate") or {}).get("issues") or [])
    low_score = int(report.get("score") or 0) < 45
    return {
        "score_adjustment": 0,
        "adjustment": 0,
        "base_score": report["score"],
        "final_score": report["score"],
        "adjustment_reason": "未调用到有效模型解释，按硬代码规则分输出。",
        "summary": (
            f"{report['name']}不像一个适合正式使用的姓名，规则分为 {report['score']} 分。"
            if low_score
            else f"{report['name']}整体观感较稳，规则分为 {report['score']} 分。"
        ),
        "highlights": [item[1].get("reason", item[0]) for item in strongest],
        "cautions": [*quality_issues, *[item[1].get("reason", item[0]) for item in weakest]],
        "source_notes": _source_notes_for_name(report),
        "suggestions": ["如果用于宝宝起名，可再结合家族辈分、方言读音和证件书写便利性复核。"],
        "verdict": "不建议作为正式姓名" if low_score else "适合保留" if report["score"] >= 82 else "可小幅优化" if report["score"] >= 70 else "建议再推敲",
        "advanced": _advanced_analysis_for_report(report) if report.get("mode") == "advanced" else None,
    }


def merge_ai_name_result(report: dict[str, Any], ai_result: dict[str, Any] | None) -> dict[str, Any]:
    safe_ai = ai_result if isinstance(ai_result, dict) else build_fallback_ai_result(report)
    try:
        returned_base_score = int(round(float(safe_ai.get("base_score"))))
    except (TypeError, ValueError):
        returned_base_score = report["score"]
    try:
        raw_adjustment = float(safe_ai.get("adjustment", safe_ai.get("score_adjustment") or 0))
    except (TypeError, ValueError):
        raw_adjustment = 0
    if returned_base_score != report["score"]:
        raw_adjustment = 0
    adjustment = int(max(-5, min(5, round(raw_adjustment))))
    final_score = clamp_score(report["score"] + adjustment)
    quality_issues = [item for item in (report.get("quality_gate") or {}).get("issues") or [] if item != "通过基础姓名有效性检查"]
    if quality_issues:
        final_score = min(final_score, 60)
    if report["score"] <= 20:
        final_score = min(final_score, 20)
    fallback = build_fallback_ai_result(report)
    summary = str(safe_ai.get("summary") or fallback["summary"])[:400]
    if final_score < 45 and quality_issues:
        summary = f"{report['name']}不像一个适合正式使用的姓名，主要问题是：{quality_issues[0]}。"
    verdict = str(safe_ai.get("verdict") or "")[:40]
    if final_score < 45:
        verdict = "不建议作为正式姓名"
    elif not verdict:
        verdict = "适合保留" if final_score >= 82 else "可小幅优化" if final_score >= 70 else "建议再推敲"
    ai_dimensions = safe_ai.get("dimensions") if isinstance(safe_ai.get("dimensions"), dict) else {}
    if report.get("mode") != "advanced":
        ai_dimensions = {key: value for key, value in ai_dimensions.items() if key != "命理适配"}
    advanced_result = _normalize_advanced_analysis(report, safe_ai.get("advanced")) if report.get("mode") == "advanced" else None
    if returned_base_score != report["score"]:
        adjustment_reason = "模型返回的 base_score 与规则分不一致，后端已忽略微调。"
    elif safe_ai.get("adjustment_reason"):
        adjustment_reason = str(safe_ai.get("adjustment_reason"))
    elif adjustment == 0:
        adjustment_reason = "硬代码评分已准确反映名字质量"
    else:
        adjustment_reason = "模型按结构化提示词给出小幅语境微调。"
    return {
        "score": final_score,
        "base_score": report["score"],
        "score_adjustment": adjustment,
        "adjustment_reason": adjustment_reason[:240],
        "score_source": "rule_engine_plus_ai_adjustment",
        "summary": summary,
        "highlights": _name_dimension_highlights(safe_ai, fallback),
        "cautions": _list_text([*quality_issues, *(safe_ai.get("risks") or safe_ai.get("cautions") or [])], 5),
        "source_notes": _merge_source_notes(report, safe_ai),
        "suggestions": _list_text([safe_ai.get("formal_usability_note"), *(safe_ai.get("suggestions") or [])], 5),
        "verdict": verdict,
        "ai_dimensions": ai_dimensions,
        "advanced": advanced_result,
        "rule_report": report,
    }


def parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None


def _score_phonology(surname: str, given: str) -> dict[str, Any]:
    chars = list(surname + given)
    strokes = [char_strokes(char) for char in chars]
    parity_changes = sum(1 for a, b in zip(strokes, strokes[1:]) if (a % 2) != (b % 2))
    repeated = len(chars) - len(set(chars))
    filler_count = sum(1 for char in chars if char in FILLER_OR_NON_NAME_CHARS)
    score = 52 + parity_changes * 8 - repeated * 12 - filler_count * 14
    if len(given) == 2:
        score += 8
    elif len(given) == 1:
        score += 3
    if any(chars[index] == chars[index + 1] for index in range(len(chars) - 1)):
        score -= 14
    return {
        "score": clamp_score(score),
        "reason": "字形节奏有变化，读感更容易形成起伏。" if parity_changes else "字形节奏偏平，需要结合真实读音再确认。",
        "details": {"stroke_estimate": strokes, "rhythm_changes": parity_changes},
    }


def _score_meaning(given: str) -> dict[str, Any]:
    positives = [POSITIVE_NAME_CHARS[char] for char in given if char in POSITIVE_NAME_CHARS]
    risks = [f"{char}：{NEGATIVE_OR_RISKY_CHARS[char]}" for char in given if char in NEGATIVE_OR_RISKY_CHARS]
    fillers = [char for char in given if char in FILLER_OR_NON_NAME_CHARS]
    score = 34 + len(positives) * 20 - len(risks) * 25 - len(fillers) * 24
    if len(positives) == len(given):
        score += 10
    return {
        "score": clamp_score(score),
        "reason": "名字意象积极、容易解释。" if positives else "寓意需要更多上下文支撑，单字联想不算强。",
        "details": {"positive_meanings": positives, "risk_notes": risks, "non_name_chars": fillers},
    }


def _score_recognition(given: str) -> dict[str, Any]:
    common_count = sum(1 for char in given if char in COMMON_NAME_CHARS)
    rare_count = sum(1 for char in given if char_strokes(char) >= 18)
    repeated_count = len(given) - len(set(given))
    filler_count = sum(1 for char in given if char in FILLER_OR_NON_NAME_CHARS)
    score = 72 - common_count * 8 - rare_count * 7 - repeated_count * 18 - filler_count * 18
    if len(set(given)) == len(given):
        score += 8
    return {
        "score": clamp_score(score),
        "reason": "不落俗套且辨识度尚可。" if common_count == 0 else "含高频起名用字，亲切但重名概率略高。",
        "details": {"common_chars": common_count, "rare_or_complex_chars": rare_count, "repeated_chars": repeated_count},
    }


def _score_glyph(surname: str, given: str) -> dict[str, Any]:
    strokes = [char_strokes(char) for char in surname + given]
    average = sum(strokes) / len(strokes)
    max_gap = max(strokes) - min(strokes)
    filler_count = sum(1 for char in surname + given if char in FILLER_OR_NON_NAME_CHARS)
    repeated_count = len(surname + given) - len(set(surname + given))
    score = 82 - filler_count * 18 - repeated_count * 10
    if average > 16:
        score -= 14
    elif average > 13:
        score -= 6
    elif 7 <= average <= 13:
        score += 4
    if max_gap > 13:
        score -= 9
    elif 3 <= max_gap <= 9:
        score += 4
    if any(value > 20 for value in strokes):
        score -= 8
    return {
        "score": clamp_score(score),
        "reason": "笔画轻重有层次，写出来比较均衡。" if score >= 78 else "字形平衡感一般，可能存在重复、过简或过繁的问题。",
        "details": {"stroke_estimate": strokes, "average_strokes": round(average, 1), "max_gap": max_gap, "repeated_chars": repeated_count},
    }


def _score_usability(clean_name: str, surname: str, given: str, gender: str) -> dict[str, Any]:
    total_len = len(surname + given)
    filler_count = sum(1 for char in surname + given if char in FILLER_OR_NON_NAME_CHARS)
    risk_count = sum(1 for char in surname + given if char in NEGATIVE_OR_RISKY_CHARS)
    culture_risks = _find_culture_risks(clean_name)
    repeated_all = len(set(surname + given)) == 1
    repeated_given = len(given) >= 2 and len(set(given)) == 1
    known_surname = surname in COMMON_SURNAMES or surname in COMPOUND_SURNAMES
    score = 70
    if total_len in {2, 3, 4}:
        score += 10
    else:
        score -= 20
    if len(given) == 2:
        score += 8
    elif len(given) > 3:
        score -= 8
    if gender == "female" and any(char in given for char in {"柔", "婉", "玥", "诗", "悦", "宁"}):
        score += 5
    if gender == "male" and any(char in given for char in {"朗", "卓", "航", "岳", "行", "曜"}):
        score += 5
    if not known_surname:
        score -= 12
    if repeated_all:
        score -= 45
    elif repeated_given:
        score -= 24
    if filler_count:
        score -= filler_count * 24
    if risk_count:
        score -= risk_count * 28
    if culture_risks:
        score -= 22
    return {
        "score": clamp_score(score),
        "reason": "长度、介绍场景和证件使用都比较得体。" if score >= 78 else "正式使用风险偏高，建议从证件、课堂和职场介绍场景复核。",
        "details": {
            "surname_length": len(surname),
            "given_length": len(given),
            "gender_reference": gender,
            "known_surname": known_surname,
            "non_name_chars": filler_count,
            "risk_chars": risk_count,
            "culture_risks": culture_risks,
        },
    }


def _score_gender_fit(given: str, gender: str) -> dict[str, Any]:
    masculine = [char for char in given if char in MASCULINE_NAME_CHARS]
    feminine = [char for char in given if char in FEMININE_NAME_CHARS]
    neutral = [char for char in given if char in NEUTRAL_NAME_CHARS]
    normalized_gender = gender if gender in {"male", "female"} else "unknown"
    if normalized_gender == "unknown":
        score = 76
        if masculine and feminine:
            score -= 6
        elif neutral:
            score += 4
        reason = "未限定性别，按中性姓名气质保守评分。"
    elif normalized_gender == "male":
        score = 70 + min(20, len(masculine) * 18) + min(6, len(neutral) * 3) - len(feminine) * 26
        if not masculine and not neutral:
            score -= 6
        reason = "名字气质与男孩常见姓名预期较匹配。" if score >= 78 else "名字气质偏柔或性别信号不清晰，男孩使用需再斟酌。"
    else:
        score = 70 + min(20, len(feminine) * 18) + min(6, len(neutral) * 3) - len(masculine) * 38
        if not feminine and not neutral:
            score -= 6
        reason = "名字气质与女孩常见姓名预期较匹配。" if score >= 78 else "名字气质偏刚硬或性别信号不清晰，女孩使用需再斟酌。"
    if masculine and feminine:
        score -= 8
    return {
        "score": clamp_score(score),
        "reason": reason,
        "details": {
            "gender_reference": normalized_gender,
            "masculine_markers": masculine,
            "feminine_markers": feminine,
            "neutral_markers": neutral,
        },
    }


def _score_culture(clean_name: str, surname: str, given: str) -> dict[str, Any]:
    risks = _find_culture_risks(clean_name)
    filler_count = sum(1 for char in clean_name if char in FILLER_OR_NON_NAME_CHARS)
    repeated_all = len(set(clean_name)) == 1
    overly_plain = len(given) == 1 and given in COMMON_NAME_CHARS
    score = 80
    if risks:
        score -= min(65, 28 * len(risks))
    if filler_count:
        score -= filler_count * 12
    if repeated_all:
        score -= 35
    if overly_plain:
        score -= 8
    if len(given) >= 2 and len(set(given)) == len(given) and not risks and filler_count == 0:
        score += 5
    return {
        "score": clamp_score(score),
        "reason": "没有明显网络梗、谐音或强 IP 联想。" if not risks and filler_count == 0 else "存在文化联想或谐音风险，正式使用需要谨慎。",
        "details": {"risk_notes": risks, "overly_plain": overly_plain},
    }


def _name_quality_gate(clean_name: str, surname: str, given: str, dimensions: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    penalty = 0
    score_cap: int | None = None
    all_chars = list(clean_name)
    filler_count = sum(1 for char in all_chars if char in FILLER_OR_NON_NAME_CHARS)
    non_chinese_count = sum(1 for char in all_chars if not ("\u4e00" <= char <= "\u9fff"))
    positive_count = sum(1 for char in given if char in POSITIVE_NAME_CHARS)
    risk_count = sum(1 for char in all_chars if char in NEGATIVE_OR_RISKY_CHARS)
    unique_ratio = len(set(all_chars)) / max(1, len(all_chars))
    culture_risks = _find_culture_risks(clean_name)

    if filler_count:
        issues.append("包含明显语气词或非姓名常用字")
        penalty += 18 * filler_count
        score_cap = min(score_cap or 100, 42)
    if non_chinese_count:
        issues.append("包含非中文姓名常用字符，中文姓名评分可信度较低")
        penalty += 16 * non_chinese_count
        score_cap = min(score_cap or 100, 45)
    if len(set(all_chars)) == 1:
        issues.append("所有字完全重复，不具备正常姓名辨识度")
        penalty += 30
        score_cap = min(score_cap or 100, 25)
    elif unique_ratio <= 0.5:
        issues.append("重复字过多，辨识度和正式感不足")
        penalty += 16
        score_cap = min(score_cap or 100, 55)
    if len(given) >= 2 and len(set(given)) == 1:
        issues.append("名字部分重复，除叠字小名外正式姓名风险较高")
        penalty += 14
        score_cap = min(score_cap or 100, 58)
    if positive_count == 0:
        issues.append("名字部分缺少明确积极寓意支撑")
        penalty += 10
        score_cap = min(score_cap or 100, 68)
    if risk_count:
        issues.append("包含负面或高风险联想字")
        penalty += 22 * risk_count
        score_cap = min(score_cap or 100, 45)
    if surname in FILLER_OR_NON_NAME_CHARS:
        issues.append("姓氏位置不像常见中文姓氏")
        penalty += 12
        score_cap = min(score_cap or 100, 50)
    if surname not in COMMON_SURNAMES and surname not in COMPOUND_SURNAMES:
        issues.append("姓氏不在常见中文姓氏表内，请确认是否为真实姓氏")
        penalty += 6
        score_cap = min(score_cap or 100, 82)
    if culture_risks:
        issues.extend(culture_risks)
        penalty += 22
        score_cap = min(score_cap or 100, 48)
    if dimensions.get("usability", {}).get("score", 0) < 45:
        score_cap = min(score_cap or 100, 42)
    if dimensions.get("culture", {}).get("score", 0) < 45:
        score_cap = min(score_cap or 100, 45)
    if dimensions.get("gender_fit", {}).get("score", 100) < 45:
        issues.append("姓名气质与所选性别明显不匹配")
        penalty += 10
        score_cap = min(score_cap or 100, 72)
    if not issues and dimensions.get("meaning", {}).get("score", 0) >= 78 and dimensions.get("recognition", {}).get("score", 0) >= 68 and dimensions.get("usability", {}).get("score", 0) >= 70:
        issues.append("通过基础姓名有效性检查")
    return {"issues": issues, "penalty": penalty, "score_cap": score_cap}


def _find_culture_risks(clean_name: str) -> list[str]:
    risks: list[str] = []
    for term, note in HOMOPHONE_RISK_TERMS.items():
        if clean_name == term or term in clean_name:
            risks.append(f"{term}：{note}")
    for term, note in CULTURE_RISK_TERMS.items():
        if term in clean_name:
            risks.append(f"{term}：{note}")
    return risks


def _score_bazi(given: str, birth_datetime: str | None) -> dict[str, Any]:
    if not birth_datetime:
        return {"score": 60, "reason": "未提供出生时间，五行维度只作保守估计。", "details": {}}
    try:
        dt = datetime.fromisoformat(birth_datetime)
    except ValueError:
        return {"score": 60, "reason": "出生时间格式无法识别，五行维度只作保守估计。", "details": {}}
    birth_elements = _birth_element_counter(dt)
    name_elements = [CHAR_ELEMENTS[char] for char in given if char in CHAR_ELEMENTS]
    full_counter = birth_elements + Counter(name_elements)
    weakest = sorted(ELEMENT_LABELS, key=lambda item: (birth_elements[item], full_counter[item]))[:2]
    matched = [item for item in name_elements if item in weakest]
    score = 68 + len(matched) * 12
    if name_elements and len(set(name_elements)) >= 2:
        score += 5
    if not name_elements:
        score -= 6
    return {
        "score": clamp_score(score),
        "reason": "名字用字对偏弱五行有一定补充。" if matched else "名字五行补充不明显，可作为优化方向参考。",
        "details": {
            "birth_elements": dict(birth_elements),
            "name_elements": name_elements,
            "preferred_elements": weakest,
            "matched_elements": matched,
            "method": "按公历年份天干地支、月份季节和时辰地支做近似娱乐估算，未按节气排盘。",
        },
    }


def _birth_element_counter(dt: datetime) -> Counter:
    year_index = (dt.year - 4) % 60
    stem_element = STEM_ELEMENTS[year_index % 10]
    branch_element = BRANCH_ELEMENTS[year_index % 12]
    month_element = _month_element(dt.month)
    hour_branch_index = ((dt.hour + 1) // 2) % 12
    hour_element = BRANCH_ELEMENTS[hour_branch_index]
    return Counter([stem_element, branch_element, month_element, hour_element])


def _month_element(month: int) -> str:
    if month in {3, 4, 5}:
        return "木"
    if month in {6, 7, 8}:
        return "火"
    if month in {9, 10}:
        return "金"
    if month in {11, 12, 1}:
        return "水"
    return "土"


def _list_text(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    rows: list[str] = []
    for item in value:
        if isinstance(item, dict):
            source = str(item.get("source") or item.get("来源") or item.get("basis") or item.get("依据") or "").strip()
            imagery = str(item.get("imagery") or item.get("意象") or item.get("comment") or item.get("说明") or "").strip()
            text = f"来源/依据：{source}；意象：{imagery}" if source and imagery else json.dumps(item, ensure_ascii=False)
        else:
            text = str(item or "").strip()
        if text:
            rows.append(text[:180])
        if len(rows) >= limit:
            break
    return rows


def _source_notes_for_name(report: dict[str, Any]) -> list[str]:
    notes: list[str] = []
    for char in str(report.get("given_name") or ""):
        note = NAME_CHAR_SOURCE_NOTES.get(char)
        if note and note not in notes:
            notes.append(note)
    if not notes:
        meanings = []
        meaning_details = ((report.get("dimensions") or {}).get("meaning") or {}).get("details") or {}
        for item in meaning_details.get("positive_meanings") or []:
            meanings.append(str(item))
        if meanings:
            notes.append(f"来源/依据：后端正向字义词表与现代汉语常用义；意象：{'、'.join(meanings[:3])}。")
    return notes or ["来源/依据：现代汉语常用姓名语义场；意象：可联想到品德、自然或审美气质。"]


def _advanced_analysis_for_report(report: dict[str, Any]) -> dict[str, str] | None:
    if report.get("mode") != "advanced":
        return None
    bazi = report.get("bazi") or {}
    details = bazi.get("details") or {}
    birth_elements = details.get("birth_elements") or {}
    name_elements = details.get("name_elements") or []
    preferred = details.get("preferred_elements") or []
    matched = details.get("matched_elements") or []
    birth_text = "、".join(f"{key}{value}" for key, value in birth_elements.items()) or "未能形成完整五行分布"
    name_text = "、".join(name_elements) if name_elements else "姓名用字未识别出明显五行偏旁"
    preferred_text = "、".join(preferred) if preferred else "暂无明确偏弱五行"
    matched_text = "、".join(matched) if matched else "未明显命中偏弱五行"
    fit_level = "中等适配" if matched else "中性平和"
    if matched and len(matched) >= 2:
        fit_level = "精准补益"
    elif not name_elements and preferred:
        fit_level = "中性平和"
    return {
        "bazi_overview": f"按硬代码近似估算，出生时点五行分布为：{birth_text}。此结果未按节气精排，仅作传统文化参考。",
        "wuxing_analysis": f"姓名用字五行识别为：{name_text}；硬代码偏弱五行参考为：{preferred_text}。",
        "yongshen_note": f"这里的喜用神仅按偏弱五行近似处理，可参考 {preferred_text}，不等同于完整八字排盘。",
        "name_element_support": f"姓名用字与偏弱五行的匹配情况：{matched_text}。",
        "balance_note": "命理适配只用于解释传统文化语义，不作命运预测；正式起名仍应优先考虑读写、寓意、证件和家庭偏好。",
        "bazi_fit_level": fit_level,
        "classical_reference": "可联想到《渊海子平》《三命通会》中五行平衡的传统分析框架，但本系统未做完整排盘。",
    }


def _normalize_advanced_analysis(report: dict[str, Any], value: Any) -> dict[str, str] | None:
    fallback = _advanced_analysis_for_report(report)
    if fallback is None:
        return None
    if not isinstance(value, dict):
        return fallback
    normalized = dict(fallback)
    for key in normalized:
        text = str(value.get(key) or "").strip()
        if text:
            normalized[key] = text[:360]
    if normalized.get("bazi_fit_level") not in {"精准补益", "中等适配", "中性平和", "加重失衡"}:
        normalized["bazi_fit_level"] = fallback["bazi_fit_level"]
    return normalized


def _merge_source_notes(report: dict[str, Any], ai_result: dict[str, Any]) -> list[str]:
    ai_notes = _list_text(ai_result.get("possible_imagery") or ai_result.get("source_notes"), 5)
    deterministic = _source_notes_for_name(report)
    if not ai_notes:
        return deterministic[:5]
    enriched: list[str] = []
    simple_notes: list[str] = []
    for note in ai_notes:
        if "来源" in note or "依据" in note or "《" in note:
            enriched.append(note)
        else:
            simple_notes.append(note)
    for note in deterministic:
        if note not in enriched:
            enriched.append(note)
        if len(enriched) >= 5:
            break
    if simple_notes and len(enriched) < 5:
        enriched.append(_composite_imagery_note(report, simple_notes))
    return enriched[:5]


def _composite_imagery_note(report: dict[str, Any], notes: list[str]) -> str:
    given = str(report.get("given_name") or "")
    source_labels = [char for char in given if char in NAME_CHAR_SOURCE_NOTES]
    source_text = "、".join(source_labels) if source_labels else given or str(report.get("name") or "")
    compact_notes = "；".join(notes[:3])
    return f"组合解读：基于“{source_text}”的字义来源综合判断，意象可概括为：{compact_notes}。"


def _name_dimension_highlights(ai_result: dict[str, Any], fallback: dict[str, Any]) -> list[str]:
    explicit = _list_text(ai_result.get("highlights"), 5)
    if explicit:
        return explicit
    dimensions = ai_result.get("dimensions")
    if not isinstance(dimensions, dict):
        return _list_text(fallback.get("highlights"), 5)
    rows: list[str] = []
    for label, item in dimensions.items():
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "").strip()
        comment = str(item.get("comment") or "").strip()
        if comment and any(marker in level for marker in ["优", "高", "好", "强", "佳"]):
            rows.append(f"{label}：{comment}"[:180])
        if len(rows) >= 5:
            break
    return rows or _list_text(fallback.get("highlights"), 5)
