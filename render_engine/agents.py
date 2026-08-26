from __future__ import annotations

from typing import Any


def agent_catalog() -> list[dict[str, Any]]:
    """智能体入口注册表：页面只读这里，避免入口散落在模板里。"""
    return [
        {
            "key": "script_storyboard",
            "label": "脚本分镜智能体",
            "icon": "panels-top-left",
            "url": "/agents/storyboard",
            "date_kind": "新增",
            "date": "2026-08-20",
            "description": "把短剧脚本拆成 HappyHorse 强一致性分镜：台词时长、首镜空间、角色档案、道具档案、素材提示词、分组镜头和三重自检。",
            "tags": ["脚本", "强一致性分镜", "角色档案", "道具档案", "自检"],
        },
        {
            "key": "drama_storyboard",
            "label": "短剧故事版",
            "icon": "clapperboard",
            "url": "/agents/drama-storyboard",
            "date_kind": "更新",
            "date": "2026-08-20",
            "description": "输入剧本后生成分镜、资产拆解、视频生成计划和后处理清单。",
            "tags": ["剧本", "分镜", "资产", "视频生成", "后处理"],
        },
    ]
