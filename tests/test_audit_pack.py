#!/usr/bin/env python3
"""周期审计任务包与 run 自动暂停行为测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "scripts"
RUNNER = ROOT / "run_pipeline.py"
sys.path.insert(0, str(ROOT))

from run_pipeline import cmd_run, commit_governance


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": "周期审计测试"},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def empty_delta(seq):
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {"章节": f"第{seq:03d}章", "新增元素": empty, "修改元素": empty}


def valid_analysis(seq):
    return f"""# 第{seq:03d}章分析

## 1. 剧情梗概
本章围绕主角收到一封来信后前往约定地点展开。来信揭示旧承诺存在漏洞，主角决定主动求证，陌生人的出现又为后续留下悬念。

## 2. 出场人物
- 主角：从犹豫转为主动追查来信真相。

## 3. 核心冲突
主角必须在相信旧承诺和面对新证据之间作出选择。

## 4. 信息增量
来信说明旧日约定含有未公开条件。

## 5. 伏笔与悬念
陌生人的身份和真实目的尚待后续揭示。

## 6. 爽点 / 虐点 / 情绪点
主动追查带来期待、紧张与危机感。

## 7. 章节功能判断
本章承担冲突启动和悬念铺垫功能。

## 8. 事件分组与标签建议
来信求证承诺线冲突启动主动追查身份悬念。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 |
|----------|----------|----------|
| V01 | 主角阅读来信 | 中 |

## 10. 结构提取提示
应提取来信事件、约定地点和陌生人线索。
"""


def write_chapter_source(project, seq):
    path = project / "原文拆解" / f"第{seq:03d}章_测试.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
    return path


def write_completed_chapter(project, seq):
    chapter = write_chapter_source(project, seq)
    base = chapter.stem
    (project / "章节处理").mkdir(parents=True, exist_ok=True)
    (project / "章节处理" / chapter.name).write_text(valid_analysis(seq), encoding="utf-8")
    write_json(project / "章节处理" / f"{base}.json", empty_delta(seq))
    for rel in [
        f"质量治理/delta校验/{base}.json",
        f"质量治理/章节校验/{base}.json",
        f"故事结构版本/story_before_ch{seq:03d}.json",
        f"故事结构版本/story_after_ch{seq:03d}.json",
        f"结构变更日志/diff_ch{seq:03d}.json",
    ]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if "校验" in str(path):
            write_json(path, {"passed": True, "mode": "process", "errors": [], "warnings": []})
        elif path.suffix == ".json":
            write_json(path, empty_story())
        else:
            path.write_text("ok\n", encoding="utf-8")
    return chapter


def test_audit_pack_cli_creates_review_package():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 6):
            write_completed_chapter(project, seq)

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, str(RUNNER), "audit-pack", str(project), "--chapters", "1-5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert result.returncode == 0, result.stdout

        pack = project / "质量治理" / "周期审计" / "audit_001-005.md"
        correction = project / "质量治理" / "周期审计" / "correction_001-005.json"
        assert pack.is_file(), result.stdout
        text = pack.read_text(encoding="utf-8")
        assert "周期结构审计任务包" in text
        assert str(correction) in text
        assert "commit-governance" in text
        assert "第001章_测试.md" in text
        assert "第005章_测试.json" in text
        assert "顶层介绍" in text
        assert "阶段性故事简介" in text

    print("[OK] audit-pack CLI 生成周期审计任务包")


def test_run_hands_periodic_audit_to_agent_then_continues_after_real_patch():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 5)
        (project / "章节处理" / "第005章_测试.md").write_text(valid_analysis(5), encoding="utf-8")
        write_json(project / "章节处理" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text(valid_analysis(6), encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))
        rc = cmd_run(project)
        assert rc == 2
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        assert load_status(status)["status"] == "awaiting_agent"
        assert not (project / "故事结构版本" / "story_after_ch006.json").is_file()

        correction = project / "质量治理" / "周期审计" / "correction_001-005.json"
        empty = {key: [] for key in ["角色集", "事件集", "地点集", "线索集", "阵营集", "物品集"]}
        write_json(correction, {
            "章节范围": "第001章-第005章",
            "治理类型": "no_change",
            "质量说明": "已检查角色、事件、地点、线索、阵营、物品，无需合并或降级。",
            "证据范围": ["第001章", "第002章", "第003章", "第004章", "第005章"],
            "新增元素": empty,
            "修改元素": empty,
        })

        rc = cmd_run(project)
        assert rc == 0
        assert load_status(status)["status"] == "committed"
        assert list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "按需治理").glob("*governance_*.txt"))
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()

    print("[OK] run 将周期审计交给Agent，提交真实补丁后继续下一章")


def load_status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manual_governance_reports_remain_separate():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "manual_correction.json"
        write_json(patch, {
            "章节": "手工治理",
            "治理类型": "no_change",
            "质量说明": "手工治理回归检查，无需修改。",
            "证据范围": ["第001章"],
            "新增元素": empty_delta(1)["新增元素"],
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))
        assert rc == 0
        assert list((project / "质量治理" / "按需治理").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))

    print("[OK] 手工治理报告保持在按需治理目录")


def test_governance_patch_compresses_tags_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        write_json(project / "故事结构_增量.json", empty_story())

        patch = project / "质量治理" / "周期审计" / "correction_001-005.json"
        write_json(patch, {
            "章节范围": "001-005",
            "新增元素": {
                "角色集": [
                    {
                        "名称": "客栈掌柜",
                        "是否主角": False,
                        "性别": 2,
                        "年龄": 0,
                        "生日": "0001-01-01T00:00:00",
                        "所属阵营": [],
                        "关系": [],
                        "分组": "路人",
                        "别名": [],
                        "标签集": ["人物类型:掌柜", "客栈"],
                        "介绍": "客栈掌柜。",
                        "详情": {
                            "首次章节": "0001",
                            "最近章节": "0001",
                            "提取理由": "测试治理补丁标签压缩。",
                        },
                    }
                ],
                "事件集": [],
                "地点集": [],
                "线索集": [],
                "阵营集": [],
                "物品集": [],
                "其他事项集": [],
            },
            "修改元素": empty_delta(1)["修改元素"],
        })

        rc = commit_governance(project, str(patch))

        assert rc == 0
        patched = json.loads(patch.read_text(encoding="utf-8"))
        character = patched["新增元素"]["角色集"][0]
        assert character["标签集"] == ["客栈"]
        assert json.loads(character["详情"]["补充标签"]) == ["人物类型:掌柜"]
        assert list((project / "质量治理" / "周期审计").glob("compress_governance_*.json"))

    print("[OK] 治理补丁校验前执行标签压缩")


def test_chapter_delta_repairs_llm_json_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        chapter = write_chapter_source(project, 1)
        base = chapter.stem
        (project / "章节处理").mkdir(parents=True, exist_ok=True)
        (project / "章节处理" / chapter.name).write_text(valid_analysis(1), encoding="utf-8")
        malformed_delta = """```json
{
  '章节': '第001章',
  '新增元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
  '修改元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
}
```
"""
        (project / "章节处理" / f"{base}.json").write_text(malformed_delta, encoding="utf-8")

        rc = cmd_run(project)

        assert rc == 0
        repaired = json.loads((project / "章节处理" / f"{base}.json").read_text(encoding="utf-8"))
        assert repaired["章节"] == "第001章"
        assert list((project / "质量治理" / "delta校验").glob("repair_json_ch001.json"))

    print("[OK] 章节 Delta 校验前修复 LLM JSON 语法")


def test_governance_patch_repairs_llm_json_before_validation():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "manual_correction.json"
        patch.write_text(
            """```json
{
  '章节': '手工治理',
  '治理类型': 'no_change',
  '质量说明': '手工治理回归检查，无需修改。',
  '证据范围': ['第001章',],
  '新增元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
  '修改元素': {
    '角色集': [], '事件集': [], '地点集': [], '线索集': [],
    '阵营集': [], '物品集': [], '其他事项集': [],
  },
}
```
""",
            encoding="utf-8",
        )

        rc = commit_governance(project, str(patch))

        assert rc == 0
        repaired = json.loads(patch.read_text(encoding="utf-8"))
        assert repaired["章节"] == "手工治理"
        assert list((project / "质量治理" / "按需治理").glob("repair_json_governance_*.json"))

    print("[OK] 治理补丁校验前修复 LLM JSON 语法")


if __name__ == "__main__":
    test_audit_pack_cli_creates_review_package()
    test_run_hands_periodic_audit_to_agent_then_continues_after_real_patch()
    test_manual_governance_reports_remain_separate()
    test_governance_patch_compresses_tags_before_validation()
    test_chapter_delta_repairs_llm_json_before_validation()
    test_governance_patch_repairs_llm_json_before_validation()
    print("\n全部测试通过 [PASS]")
