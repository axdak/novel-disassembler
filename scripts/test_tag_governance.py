#!/usr/bin/env python3
"""标签治理的守恒、迁移和详情校验测试。"""

import copy

from apply_governance_ops import apply_ops
from story_schema_rules import validate_exact_story_schema
from validate_delta import validate_tag_governance_ops


def story_with_tags(tags=None, supplementary=None):
    detail = {"首次章节": "0001", "最近章节": "0001"}
    if supplementary is not None:
        detail["补充标签"] = supplementary
    return {
        "介绍": {"标题": "测试", "描述": "测试"},
        "角色集": [{
            "名称": "张三", "是否主角": True, "性别": 0, "年龄": 20,
            "生日": "0001-01-01T00:00:00", "所属阵营": [], "关系": [],
            "分组": "主角团", "别名": [], "标签集": tags or ["人物类型:主角", "热血", "初入江湖"],
            "介绍": "故事主角。", "详情": detail,
        }],
        "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": [],
    }


def tag_op(retained=None, demoted=None):
    return {
        "类型": "角色", "名称": "张三",
        "保留标签": retained if retained is not None else ["人物类型:主角"],
        "降级标签": demoted if demoted is not None else ["热血", "初入江湖"],
        "理由": "后两项仅描述当前阶段。",
    }


def patch(op):
    empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集", "其他事项集"]}
    return {"章节范围": "第001章-第005章", "新增元素": empty, "修改元素": empty, "治理操作": {"治理标签": [op]}}


def test_tag_governance_moves_tags_losslessly_and_records_reason():
    story = story_with_tags()
    result, logs, warnings = apply_ops(copy.deepcopy(story), patch(tag_op()))
    item = result["角色集"][0]
    assert warnings == []
    assert item["标签集"] == ["人物类型:主角"]
    assert item["详情"]["补充标签"] == '["热血","初入江湖"]'
    assert "理由:后两项仅描述当前阶段。" in logs[0]


def test_tag_governance_merges_existing_supplementary_tags_without_duplicates():
    story = story_with_tags(supplementary='["既有补充","热血"]')
    result, _, warnings = apply_ops(copy.deepcopy(story), patch(tag_op()))
    assert warnings == []
    assert result["角色集"][0]["详情"]["补充标签"] == '["既有补充","热血","初入江湖"]'


def test_tag_governance_requires_full_conservation_and_no_overlap():
    story = story_with_tags()
    missing = tag_op(demoted=["热血"])
    overlap = tag_op(retained=["人物类型:主角", "热血"])
    assert any("完整覆盖" in error for error in validate_tag_governance_ops(story, patch(missing), "governance"))
    assert any("不得重叠" in error for error in validate_tag_governance_ops(story, patch(overlap), "governance"))
    result, _, warnings = apply_ops(copy.deepcopy(story), patch(missing))
    assert warnings
    assert result == story


def test_tag_governance_is_governance_only_and_keeps_controlled_validation():
    story = story_with_tags()
    assert validate_tag_governance_ops(story, patch(tag_op()), "process") == ["治理操作.治理标签 仅允许在 governance 模式使用"]
    invalid = tag_op(retained=["人物类型:不存在"], demoted=["热血", "初入江湖"])
    assert any("未知受控标签" in error for error in validate_tag_governance_ops(story, patch(invalid), "governance"))


def test_supplementary_tags_reject_invalid_encoded_content_and_overlap():
    malformed = story_with_tags(supplementary="热血、初入江湖")
    errors, _ = validate_exact_story_schema(malformed, mode="final")
    assert any("必须是可解析的 JSON 字符串数组" in error for error in errors)
    overlap = story_with_tags(supplementary='["热血"]')
    errors, _ = validate_exact_story_schema(overlap, mode="final")
    assert any("不得重复" in error for error in errors)


def test_empty_tag_set_passes_final_schema_validation():
    story = story_with_tags(tags=[])
    errors, warnings = validate_exact_story_schema(story, mode="final")
    assert errors == []
    assert not any("标签集 为空" in warning for warning in warnings)
