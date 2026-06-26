#!/usr/bin/env python3
"""章节分析与 Delta 两阶段交接测试。"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(ROOT))

from run_pipeline import artifact_paths, cmd_run


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": "两阶段章节任务测试"},
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }


def empty_delta(seq):
    empty = {
        "角色集": [], "事件集": [], "地点集": [], "线索集": [],
        "阵营集": [], "物品集": [], "其他事项集": [],
    }
    return {"章节": f"第{seq:03d}章", "新增元素": empty, "修改元素": empty}


def test_run_hands_off_analysis_then_delta_before_committing():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["analysis_task"].is_file()
        assert not paths["delta_task"].is_file()
        assert not paths["delta"].is_file()

        paths["analysis"].write_text("# 第001章分析\n测试章节分析。\n", encoding="utf-8")

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["delta_task"].is_file()
        delta_task = paths["delta_task"].read_text(encoding="utf-8")
        assert "测试章节分析。" in delta_task
        assert not paths["delta"].is_file()

        write_json(paths["delta"], empty_delta(1))

        assert cmd_run(project, audit_interval=0) == 0
        assert paths["after"].is_file()

    print("[OK] run 依次交接章节分析和Delta任务后才提交章节")


if __name__ == "__main__":
    test_run_hands_off_analysis_then_delta_before_committing()
    print("\n全部测试通过 [PASS]")
