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


def valid_analysis(seq=1):
    return f"""# 第{seq:03d}章分析

## 1. 剧情梗概
本章围绕主角接到意外来信后前往约定地点展开。来信暴露了旧日承诺的漏洞，主角在犹豫后决定当面求证，结尾以陌生人出现留下新的悬念。

## 2. 出场人物
- 主角：阅读来信后从迟疑转为主动求证。

## 3. 核心冲突
主角需要在相信旧承诺和面对新证据之间作出选择。

## 4. 信息增量
来信说明旧日承诺存在未公开的条件。

## 5. 伏笔与悬念
陌生人的身份及其掌握的信息待后续揭示。

## 6. 爽点 / 虐点 / 情绪点
主角主动追查带来期待与紧张。

## 7. 章节功能判断
本章承担冲突启动与悬念铺垫功能。

## 8. 事件分组与标签建议
来信求证承诺线冲突启动主动追查身份悬念。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 |
|----------|----------|----------|
| V01 | 主角阅读来信 | 中 |

## 10. 结构提取提示
应提取来信事件、约定地点与陌生人线索。
"""


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

        paths["analysis"].write_text(valid_analysis(), encoding="utf-8")

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["delta_task"].is_file()
        delta_task = paths["delta_task"].read_text(encoding="utf-8")
        assert "## 1. 剧情梗概" in delta_task
        assert not paths["delta"].is_file()

        write_json(paths["delta"], empty_delta(1))

        assert cmd_run(project, audit_interval=0) == 0
        assert paths["after"].is_file()

    print("[OK] run 依次交接章节分析和Delta任务后才提交章节")


def test_run_regenerates_structurally_shrunken_analysis_before_delta_handoff():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = project / "原文拆解" / "第001章_测试.md"
        chapter.parent.mkdir(parents=True, exist_ok=True)
        chapter.write_text("# 第001章\n测试原文。\n", encoding="utf-8")
        paths = artifact_paths(project, chapter)

        assert cmd_run(project, audit_interval=0) == 2
        paths["analysis"].write_text("# 第001章分析\n本章发生了一件事。\n", encoding="utf-8")

        assert cmd_run(project, audit_interval=0) == 2
        assert paths["analysis_regenerate_task"].is_file()
        assert not paths["delta_task"].is_file()
        regenerate_task = paths["analysis_regenerate_task"].read_text(encoding="utf-8")
        assert "重新生成" in regenerate_task
        assert "不要参考当前不合格的章节分析MD" in regenerate_task

        paths["analysis"].write_text(valid_analysis(), encoding="utf-8")
        assert cmd_run(project, audit_interval=0) == 2
        assert paths["delta_task"].is_file()

    print("[OK] 缩水章节分析会重新生成，不能直接交接Delta")


if __name__ == "__main__":
    test_run_hands_off_analysis_then_delta_before_committing()
    test_run_regenerates_structurally_shrunken_analysis_before_delta_handoff()
    print("\n全部测试通过 [PASS]")
