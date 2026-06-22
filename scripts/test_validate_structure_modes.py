#!/usr/bin/env python3
"""validate_structure.py 模式语义测试。"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VALIDATOR = ROOT / "validate_structure.py"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def empty_story():
    return {
        "介绍": {"标题": "测试故事", "描述": ""},
        "角色集": [],
        "事件集": [],
        "地点集": [],
        "线索集": [],
        "阵营集": [],
        "物品集": [], "其他事项集": [],
    }


def empty_delta():
    empty = {"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}
    return {"章节": "第001章", "新增元素": empty, "修改元素": empty}


def run(cmd):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_chapter_check_reports_full_process_store_check():
    with tempfile.TemporaryDirectory() as td:
        story = Path(td) / "story.json"
        delta = Path(td) / "delta.json"
        write_json(story, empty_story())
        write_json(delta, empty_delta())

        result = run([sys.executable, str(VALIDATOR), "--chapter-check", str(story), str(delta)])
        assert result.returncode == 0, result.stdout
        assert "合并后全量过程库校验" in result.stdout, result.stdout
        assert "不是仅检查本章触碰元素" in result.stdout, result.stdout

    print("[OK] chapter-check 报告明确为全量过程库校验")


if __name__ == "__main__":
    test_chapter_check_reports_full_process_store_check()
    print("\n全部测试通过 [PASS]")
