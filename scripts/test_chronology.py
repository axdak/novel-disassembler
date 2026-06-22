#!/usr/bin/env python3
"""章节时间、事件顺序和剧情段分组的回归测试。"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from chronology import (
    BASE_TIME,
    assign_event_group_orders,
    chapter_time,
    is_iso_time,
    normalize_involved_chapters,
)
from merge_delta import deep_merge_item, merge_delta
from migrate_chronology import migrate_story
from normalize_story_schema import normalize_structure
from story_schema_rules import validate_exact_story_schema


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def empty_story():
    return {
        "介绍": {"标题": "测试", "描述": ""},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def event(name, chapters, group="0001-开篇"):
    return {
        "名称": name,
        "发生地点": "",
        "参与成员": [],
        "重量级": 1,
        "目标事件": [],
        "分组": group,
        "时间": chapter_time(int(chapters.split("，")[0])),
        "别名": [],
        "标签集": [],
        "介绍": name,
        "详情": {"涉及章节": chapters},
    }


def test_chapter_time_and_involved_chapters_are_canonical():
    assert BASE_TIME == "0001-01-01T00:00:00"
    assert chapter_time(1) == "0001-01-01T00:00:00"
    assert chapter_time(2) == "0001-01-02T00:00:00"
    assert is_iso_time(chapter_time(1))
    assert not is_iso_time("0001-1-1T0:0:0")
    assert normalize_involved_chapters("0045，0043，0043，0044") == "0043，0044，0045"
    print("[OK] 章节时间和涉及章节字符串规范化")


def test_normalization_adds_birthday_and_compact_chapter_fields():
    normalized, _ = normalize_structure({
        **empty_story(),
        "角色集": [{"名称": "甲", "详情": {"首次出现章节": "第001章", "最近更新章节": "第008章", "来源章节": "第001章；第008章", "提取理由": "首次登场；身份确认"}}],
    })
    person = normalized["角色集"][0]
    assert person["生日"] == BASE_TIME
    assert person["详情"]["首次章节"] == "0001"
    assert person["详情"]["最近章节"] == "0008"
    assert "来源章节" not in person["详情"]
    assert "提取理由" not in person["详情"]
    print("[OK] 人物默认生日和紧凑章节字段")


def test_merge_inserts_historical_event_by_involved_chapter():
    with tempfile.TemporaryDirectory() as directory:
        story_path = os.path.join(directory, "story.json")
        delta_path = os.path.join(directory, "delta.json")
        story = empty_story()
        story["事件集"] = [
            event("第001章事件", "0001", "0001-开篇"),
            event("第050章事件", "0050", "0050-收束"),
        ]
        write_json(story_path, story)
        write_json(delta_path, {
            "章节": "治理补丁_001-050",
            "新增元素": {"事件集": [event("第043-045章聚合事件", "0043，0044，0045", "成人仪式")]},
            "修改元素": {},
        })

        merge_delta(story_path, delta_path)
        with open(story_path, "r", encoding="utf-8") as handle:
            merged = json.load(handle)

        assert [item["名称"] for item in merged["事件集"]] == ["第001章事件", "第043-045章聚合事件", "第050章事件"]
        assert merged["事件集"][1]["分组"] == "0020-成人仪式"
    print("[OK] 历史聚合事件按章节位置插入")


def test_group_order_uses_plot_segment_rank_without_reordering_group_members():
    events = [
        event("第050章事件", "0050", "成人仪式"),
        event("第029章事件", "0029", "成人仪式"),
        event("第043章事件", "0043", "成人仪式"),
    ]
    assign_event_group_orders(events)
    assert [item["名称"] for item in events] == ["第050章事件", "第029章事件", "第043章事件"]
    assert {item["分组"] for item in events} == {"0010-成人仪式"}
    print("[OK] 剧情段序号分组且不改事件原顺序")


def test_migration_drops_legacy_trace_and_stably_orders_events():
    story = empty_story()
    story["角色集"] = [{
        "名称": "甲", "是否主角": True, "性别": 0, "年龄": 20,
        "所属阵营": [], "关系": [], "分组": "主角", "别名": [], "标签集": [], "介绍": "甲",
        "详情": {"来源章节": "第001章；第008章", "提取理由": "首次登场；身份确认", "首次出现章节": "第001章", "最近更新章节": "第008章"},
    }]
    story["事件集"] = [
        event("后续事件", "0050", "后续"),
        {**event("成人仪式", "0043，0044，0045", "成人仪式"), "详情": {"来源章节": "第043章；第044章；第045章", "提取理由": "聚合重复事件"}},
    ]
    migrated, _ = migrate_story(story)
    assert [item["名称"] for item in migrated["事件集"]] == ["成人仪式", "后续事件"]
    assert migrated["事件集"][0]["分组"] == "0010-成人仪式"
    assert migrated["角色集"][0]["生日"] == BASE_TIME
    assert migrated["角色集"][0]["详情"]["首次章节"] == "0001"
    assert migrated["角色集"][0]["详情"]["最近章节"] == "0008"
    assert not {"来源章节", "提取理由", "首次出现章节", "最近更新章节"} & set(migrated["角色集"][0]["详情"])
    print("[OK] 存量迁移回填章节字段、清理最终追溯文本并稳定重排事件")


def test_structure_validator_rejects_event_order_and_group_rank_drift():
    story = empty_story()
    story["事件集"] = [
        event("后续", "0050", "0050-主线"),
        event("历史聚合", "0043", "0050-主线"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("早于前一事件" in error for error in errors), errors
    assert any("段号应为0010" in error for error in errors), errors
    print("[OK] 结构校验器拒绝事件乱序和错误剧情段号")


def test_repeated_trace_updates_do_not_grow_final_element():
    target = {"名称": "甲", "详情": {}}
    for seq in range(1, 1001):
        deep_merge_item(target, {
            "名称": "甲",
            "详情": {"来源章节": f"第{seq:04d}章", "提取理由": f"第{seq}章提取"},
        }, "角色集")
    assert target["详情"] == {}
    assert "来源章节" not in target and "提取理由" not in target
    print("[OK] 千次追溯更新不增加最终元素JSON")


def test_structure_validator_rejects_legacy_trace_in_final_elements():
    story = empty_story()
    story["角色集"] = [{
        "名称": "甲", "是否主角": True, "性别": 0, "年龄": 20, "生日": BASE_TIME,
        "所属阵营": [], "关系": [], "分组": "主角", "首次章节": "0001", "最近章节": "0001",
        "别名": [], "标签集": [], "介绍": "甲", "详情": {"提取理由": "旧格式"},
    }]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("不得存在于最终元素" in error for error in errors), errors
    print("[OK] 结构校验器拒绝最终元素中的旧追溯字段")


def test_group_order_uses_plot_segment_rank_not_chapter_number():
    events = [
        event("退婚冲突", "0003", "退婚事件"),
        event("退婚反击", "0004", "退婚事件"),
        event("魔兽山脉历练", "0006", "历练事件"),
    ]
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == [
        "0010-退婚事件",
        "0010-退婚事件",
        "0020-历练事件",
    ]
    print("[OK] 连续剧情段按段号分组，不按章节号拆分")


def test_group_order_rebuild_preserves_free_plot_segment_detail():
    events = [
        event("坊市筹资", "0003", "坊市筹资"),
        event("魔兽山脉历练", "0006", "历练事件"),
        event("拍卖会竞价", "0018", "拍卖会事件"),
    ]
    events[0]["详情"]["剧情段"] = "萧炎-坊市-药材出售-资源积累"
    events[0]["详情"]["关联线索"] = ["线索:资金缺口"]
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == [
        "0010-坊市筹资",
        "0020-历练事件",
        "0030-拍卖会事件",
    ]
    assert events[0]["详情"]["剧情段"] == "萧炎-坊市-药材出售-资源积累"
    assert events[0]["详情"]["关联线索"] == ["线索:资金缺口"]
    print("[OK] 段号重排不影响自由剧情段与线索关联")


def test_numeric_only_group_uses_plot_segment_detail_instead_of_number_suffix():
    events = [
        event("坊市筹资", "0003", "0001"),
        event("出售药材", "0004", "0001"),
    ]
    for item in events:
        item["详情"]["剧情段"] = "坊市筹资"
    assign_event_group_orders(events)
    assert [item["分组"] for item in events] == ["0010-坊市筹资", "0010-坊市筹资"]
    print("[OK] 纯编号分组改用剧情段信息")


def test_structure_validator_rejects_number_only_plot_segment_name():
    story = empty_story()
    story["事件集"] = [event("坊市筹资", "0003", "0010-0001")]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("不能是纯编号" in error for error in errors), errors
    print("[OK] 结构校验拒绝编号套编号分组")


def test_assigner_does_not_launder_number_only_group_into_ranked_group():
    events = [event("坊市筹资", "0003", "0001")]
    assign_event_group_orders(events)
    assert events[0]["分组"] == "0001"
    print("[OK] 排序器不再把纯编号伪装成剧情分组")


def test_merge_assigns_new_segment_rank_and_governance_reorders_after_insertion():
    with tempfile.TemporaryDirectory() as directory:
        story_path = os.path.join(directory, "story.json")
        delta_path = os.path.join(directory, "delta.json")
        story = empty_story()
        story["事件集"] = [
            event("退婚冲突", "0003", "0010-退婚事件"),
            event("拍卖会竞价", "0018", "0020-拍卖会事件"),
        ]
        write_json(story_path, story)
        write_json(delta_path, {
            "章节范围": "第001章-第020章",
            "新增元素": {"事件集": [event("魔兽山脉历练", "0006", "历练事件")]},
            "修改元素": {},
        })
        merge_delta(story_path, delta_path)
        merged = json.loads(open(story_path, "r", encoding="utf-8").read())
        assert [item["名称"] for item in merged["事件集"]] == ["退婚冲突", "魔兽山脉历练", "拍卖会竞价"]
        assert [item["分组"] for item in merged["事件集"]] == [
            "0010-退婚事件",
            "0020-历练事件",
            "0030-拍卖会事件",
        ]
    print("[OK] 治理插入历史事件后按剧情段顺序全量重排段号")


def test_migration_splits_noncontiguous_legacy_group_runs():
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "0003-经济线"),
        event("魔兽山脉历练", "0006", "0006-历练事件"),
        event("拍卖会竞价", "0018", "0018-经济线"),
    ]
    migrated, _ = migrate_story(story)
    assert [item["分组"] for item in migrated["事件集"]] == [
        "0010-经济线",
        "0020-历练事件",
        "0030-经济线续2",
    ]
    print("[OK] 存量迁移拆分非连续旧分组")


def test_structure_validator_rejects_noncontiguous_reuse_of_plot_segment_name():
    story = empty_story()
    story["事件集"] = [
        event("坊市筹资", "0003", "0010-坊市筹资"),
        event("魔兽山脉历练", "0006", "0020-历练事件"),
        event("拍卖会竞价", "0018", "0030-坊市筹资"),
    ]
    errors, _ = validate_exact_story_schema(story, mode="process")
    assert any("非连续复用" in error for error in errors), errors
    print("[OK] 结构校验拒绝非连续复用同一剧情段名称")


if __name__ == "__main__":
    test_chapter_time_and_involved_chapters_are_canonical()
    test_normalization_adds_birthday_and_compact_chapter_fields()
    test_merge_inserts_historical_event_by_involved_chapter()
    test_group_order_uses_plot_segment_rank_without_reordering_group_members()
    test_migration_drops_legacy_trace_and_stably_orders_events()
    test_structure_validator_rejects_event_order_and_group_rank_drift()
    test_repeated_trace_updates_do_not_grow_final_element()
    test_structure_validator_rejects_legacy_trace_in_final_elements()
    test_group_order_uses_plot_segment_rank_not_chapter_number()
    test_group_order_rebuild_preserves_free_plot_segment_detail()
    test_numeric_only_group_uses_plot_segment_detail_instead_of_number_suffix()
    test_structure_validator_rejects_number_only_plot_segment_name()
    test_assigner_does_not_launder_number_only_group_into_ranked_group()
    test_merge_assigns_new_segment_rank_and_governance_reorders_after_insertion()
    test_migration_splits_noncontiguous_legacy_group_runs()
    test_structure_validator_rejects_noncontiguous_reuse_of_plot_segment_name()
    print("\n全部测试通过 [PASS]")
