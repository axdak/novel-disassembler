#!/usr/bin/env python3
"""analysis_context_pack 中视觉资产支援逻辑的测试。"""

import json

from analysis_context_pack import collect_visual_tags, summarize_story_structure
from story_schema_rules import dump_supplementary_tags


def _event(name, tags=None, supplementary=None, group=""):
    detail = {"涉及章节": "0001", "提取理由": "测试"}
    if supplementary is not None:
        detail["补充标签"] = supplementary
    return {
        "名称": name,
        "发生地点": "",
        "参与成员": [],
        "重量级": 0,
        "目标事件": [],
        "分组": group,
        "时间": "0001-01-01T00:00:00",
        "别名": [],
        "标签集": list(tags or []),
        "介绍": "",
        "详情": detail,
    }


def test_visual_tags_from_tag_field():
    item = _event("退婚现场", tags=["画面类型:冲突对峙", "视觉用途:封面候选", "热血"])
    assert collect_visual_tags(item) == ["画面类型:冲突对峙", "视觉用途:封面候选"]


def test_visual_tags_from_supplementary_field():
    """压缩后场景：视觉前缀全部沉到 详情.补充标签。"""
    supp = dump_supplementary_tags(["画面类型:冲突对峙", "视觉用途:封面候选", "剧情母题:复仇"])
    item = _event("退婚现场", tags=["热血"], supplementary=supp)
    assert collect_visual_tags(item) == ["画面类型:冲突对峙", "视觉用途:封面候选"]


def test_visual_tags_merged_from_both_dedup():
    supp = dump_supplementary_tags(["画面类型:冲突对峙", "视觉用途:封面候选"])
    item = _event("退婚现场", tags=["画面类型:冲突对峙"], supplementary=supp)
    # 标签集直读优先，补充标签去重追加
    assert collect_visual_tags(item) == ["画面类型:冲突对峙", "视觉用途:封面候选"]


def test_visual_tags_empty_when_none():
    item = _event("普通对话", tags=["热血", "对白"])
    assert collect_visual_tags(item) == []


def test_visual_tags_handles_corrupt_supplementary():
    """补充标签损坏：不抛错，按空对待，仍能读到标签集中的视觉前缀。"""
    item = _event("退婚现场", tags=["画面类型:冲突对峙"], supplementary="not a json")
    assert collect_visual_tags(item) == ["画面类型:冲突对峙"]


def test_visual_tags_ignores_non_dict_input():
    assert collect_visual_tags(None) == []
    assert collect_visual_tags("string") == []
    assert collect_visual_tags(123) == []


def test_summary_shows_visual_tags_inline():
    """事件名旁应当显示已有视觉前缀，方便 LLM 浏览结构索引时看到。"""
    supp = dump_supplementary_tags(["视觉用途:封面候选"])
    story = {
        "介绍": {"标题": "测试", "描述": ""},
        "角色集": [],
        "事件集": [
            _event("退婚现场", tags=["画面类型:冲突对峙"], supplementary=supp, group="0010-退婚"),
            _event("茶馆对话", tags=["对白"]),
        ],
        "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }
    summary = summarize_story_structure(story)
    assert "退婚现场(0010-退婚)[画面类型:冲突对峙,视觉用途:封面候选]" in summary
    # 没有视觉前缀的事件不应该加方括号
    assert "茶馆对话[" not in summary


def test_summary_does_not_decorate_non_event_collections():
    """只对事件集做视觉前缀展示；其他集合保持原行为。"""
    char_item = {
        "名称": "主角",
        "是否主角": True,
        "性别": 0,
        "年龄": 18,
        "生日": "0001-01-01T00:00:00",
        "所属阵营": [],
        "关系": [],
        "分组": "主角团",
        "别名": [],
        "标签集": ["画面类型:角色卡"],  # 即使误标也不应渲染到角色名旁
        "介绍": "",
        "详情": {"首次章节": "0001", "最近章节": "0001", "提取理由": "测试"},
    }
    story = {
        "介绍": {"标题": "测试", "描述": ""},
        "角色集": [char_item],
        "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }
    summary = summarize_story_structure(story)
    assert "主角(主角团)" in summary
    assert "主角(主角团)[" not in summary


from analysis_context_pack import TASKS, output_contract


def test_chapter_structure_task_registered():
    assert "chapter_structure" in TASKS
    info = TASKS["chapter_structure"]
    assert info["name"]  # 非空
    assert info["goal"]
    # 章节级 output 路径用 {NNN} 占位,实际章节号由 per-chapter 派发时替换
    assert any("分章" in p and "章节结构.md" in p for p in info["outputs"])


def test_narrative_structure_task_registered():
    assert "narrative_structure" in TASKS
    info = TASKS["narrative_structure"]
    expected = {
        "全书分析/故事结构/三幕式结构图.md",
        "全书分析/故事结构/Brooks四部分结构图.md",
        "全书分析/故事结构/Freytag五段结构图.md",
        "全书分析/故事结构/故事七要素档案.md",
        "全书分析/故事结构/故事力学评估.md",
        "全书分析/故事结构/故事工程学评估.md",
        "全书分析/故事结构/小说骨架.md",
    }
    assert expected.issubset(set(info["outputs"]))


def test_chapter_structure_output_contract_has_five_sections():
    contract = output_contract("chapter_structure")
    # 五小节标题必须全部出现
    for header in ["三幕式定位", "故事七要素", "故事力学", "故事工程学", "小说骨架"]:
        assert header in contract
    # 三幕式术语锁定
    assert "第一幕" in contract and "建置" in contract
    assert "第二幕" in contract and "对抗" in contract
    assert "第三幕" in contract and "解决" in contract
    # 禁止编造的硬规则
    assert "待确认" in contract


def test_narrative_structure_output_contract_has_seven_artifacts():
    contract = output_contract("narrative_structure")
    for artifact in [
        "三幕式结构图", "Brooks四部分结构图", "Freytag五段结构图",
        "故事七要素档案", "故事力学评估", "故事工程学评估", "小说骨架",
    ]:
        assert artifact in contract
    # 七要素清单出现
    for elem in [
        "主角", "缺陷", "有利的故事环境", "反面角色",
        "主角的盟友", "改变人生的事件", "整合故事要素",
    ]:
        assert elem in contract
