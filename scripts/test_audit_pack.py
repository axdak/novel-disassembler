#!/usr/bin/env python3
"""周期审计任务包与 run 自动暂停行为测试。"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "run_pipeline.py"
sys.path.insert(0, str(ROOT))

from run_pipeline import cmd_run, commit_governance, run_audit_worker, run_periodic_governance


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


def write_chapter_source(project, seq):
    path = project / "原文拆解" / f"第{seq:03d}章_测试.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# 第{seq:03d}章\n测试原文。\n", encoding="utf-8")
    return path


def write_completed_chapter(project, seq):
    chapter = write_chapter_source(project, seq)
    base = chapter.stem
    (project / "章节处理").mkdir(parents=True, exist_ok=True)
    (project / "章节处理" / chapter.name).write_text(f"# 第{seq:03d}章分析\n", encoding="utf-8")
    write_json(project / "章节处理" / f"{base}.json", empty_delta(seq))
    for rel in [
        f"质量治理/delta校验/{base}.txt",
        f"质量治理/章节校验/{base}.txt",
        f"故事结构版本/story_before_ch{seq:03d}.json",
        f"故事结构版本/story_after_ch{seq:03d}.json",
        f"结构变更日志/diff_ch{seq:03d}.json",
    ]:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            write_json(path, empty_story())
        else:
            path.write_text("ok\n", encoding="utf-8")
    return chapter


def write_auto_auditor(project):
    auditor = project / "auto_auditor.py"
    auditor.write_text(
        """import json
import sys
from pathlib import Path

correction = Path(sys.argv[1])
attempt_file = Path(sys.argv[2])
attempts = int(attempt_file.read_text(encoding=\"utf-8\")) if attempt_file.exists() else 0
attempts += 1
attempt_file.write_text(str(attempts), encoding=\"utf-8\")
if attempts == 1:
    raise SystemExit(1)
empty = {key: [] for key in [\"角色集\", \"事件集\", \"地点集\", \"线索集\", \"阵营集\", \"物品集\"]}
correction.write_text(json.dumps({\"章节范围\": \"第001章-第005章\", \"新增元素\": empty, \"修改元素\": empty}, ensure_ascii=False), encoding=\"utf-8\")
""",
        encoding="utf-8",
    )
    attempts = project / "audit_attempts.txt"
    command = '"{}" "{}" "{{correction_path}}" "{}"'.format(
        sys.executable, auditor, attempts
    )
    return command, attempts


def test_audit_pack_cli_creates_review_package():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 6):
            write_completed_chapter(project, seq)

        result = subprocess.run(
            [sys.executable, str(RUNNER), "audit-pack", str(project), "--chapters", "1-5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
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

    print("[OK] audit-pack CLI 生成周期审计任务包")


def test_run_retries_periodic_audit_then_continues_to_next_chapter():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 5):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 5)
        (project / "章节处理" / "第005章_测试.md").write_text("# 第005章分析\n", encoding="utf-8")
        write_json(project / "章节处理" / "第005章_测试.json", empty_delta(5))
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text("# 第006章分析\n", encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))
        command, attempts = write_auto_auditor(project)

        rc = cmd_run(project, audit_command=command, governance_retries=2)
        assert rc == 0
        assert attempts.read_text(encoding="utf-8") == "2"
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        assert load_status(status)["status"] == "committed"
        assert list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "按需治理").glob("*governance_*.txt"))
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()

    print("[OK] run 自动重试周期审计、提交治理补丁并继续下一章")


def load_status(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_recovers_legacy_pending_audit_without_manual_pause():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        for seq in range(1, 6):
            write_completed_chapter(project, seq)
        write_chapter_source(project, 6)
        (project / "章节处理" / "第006章_测试.md").write_text("# 第006章分析\n", encoding="utf-8")
        write_json(project / "章节处理" / "第006章_测试.json", empty_delta(6))
        status = project / "质量治理" / "周期审计" / "audit_001-005.status.json"
        write_json(status, {
            "status": "pending",
            "range": "001-005",
            "audit_pack": str(project / "质量治理" / "周期审计" / "audit_001-005.md"),
            "correction_path": str(project / "质量治理" / "周期审计" / "correction_001-005.json"),
        })
        command, attempts = write_auto_auditor(project)

        rc = cmd_run(project, audit_command=command, governance_retries=2)
        assert rc == 0
        assert attempts.read_text(encoding="utf-8") == "2"
        assert load_status(status)["status"] == "committed"
        assert (project / "故事结构版本" / "story_after_ch006.json").is_file()

    print("[OK] run 自动接管旧版 pending 周期审计")


def test_manual_governance_reports_remain_separate():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        patch = project / "manual_correction.json"
        write_json(patch, {"章节": "手工治理", "新增元素": empty_delta(1)["新增元素"], "修改元素": empty_delta(1)["修改元素"]})

        rc = commit_governance(project, str(patch))
        assert rc == 0
        assert list((project / "质量治理" / "按需治理").glob("validate_*governance_*.txt"))
        assert not list((project / "质量治理" / "周期审计").glob("validate_*governance_*.txt"))

    print("[OK] 手工治理报告保持在按需治理目录")


def test_audit_worker_timeout_is_reported():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        paths = {
            "dir": project / "质量治理" / "周期审计",
            "pack": project / "质量治理" / "周期审计" / "audit_001-001.md",
            "correction": project / "质量治理" / "周期审计" / "correction_001-001.json",
            "feedback": project / "质量治理" / "周期审计" / "feedback_001-001.md",
        }
        paths["dir"].mkdir(parents=True)
        rc, report = run_audit_worker(
            f'"{sys.executable}" -c "import time; time.sleep(2)"', project, paths, 1, 1, 1, 0.1
        )
        assert rc == 124
        assert "超时" in report.read_text(encoding="utf-8")

    print("[OK] 外部审计器超时会记录并失败关闭")


def test_governance_retries_means_retries_after_initial_attempt():
    with tempfile.TemporaryDirectory() as td:
        project = Path(td) / "拆书_测试"
        write_json(project / "故事结构_增量.json", empty_story())
        rc = run_periodic_governance(
            project,
            1,
            1,
            f'"{sys.executable}" -c "raise SystemExit(1)"',
            governance_retries=3,
            retry_delay=0,
            audit_timeout_seconds=600,
        )
        assert rc == 1
        reports = list((project / "质量治理" / "周期审计").glob("*.worker_*.txt"))
        assert len(reports) == 4
        status = load_status(project / "质量治理" / "周期审计" / "audit_001-001.status.json")
        assert status["status"] == "failed"
        assert status["attempt"] == 4

    print("[OK] 3 次重试限制为首次执行外加 3 次重试")


if __name__ == "__main__":
    test_audit_pack_cli_creates_review_package()
    test_run_retries_periodic_audit_then_continues_to_next_chapter()
    test_run_recovers_legacy_pending_audit_without_manual_pause()
    test_manual_governance_reports_remain_separate()
    test_audit_worker_timeout_is_reported()
    test_governance_retries_means_retries_after_initial_attempt()
    print("\n全部测试通过 [PASS]")
