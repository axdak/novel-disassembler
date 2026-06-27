import os
import subprocess
import sys
import tempfile
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tools" / "agent_worker.py"


def write_fake_cli(path: Path) -> None:
    path.write_text(
        """
import json
import os
import sys
from pathlib import Path

record = Path(os.environ["FAKE_CLI_RECORD"])
record.write_text(json.dumps({
    "argv": sys.argv[1:],
    "stdin": sys.stdin.read(),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print("# generated artifact")
print(os.environ.get("ND_TASK_TYPE", ""))
""",
        encoding="utf-8",
    )


def run_worker(provider: str, provider_env: str):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "task.md"
        task.write_text("# Task\nWrite the target file.\n", encoding="utf-8")
        output = root / "out.md"
        fake_cli = root / "fake_cli.py"
        record = root / "record.json"
        write_fake_cli(fake_cli)

        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "ND_TASK_TYPE": "analysis",
            "ND_PROJECT_DIR": str(project),
            "ND_TASK_PACK": str(task),
            "ND_EXPECTED_OUTPUT": str(output),
            "ND_CHAPTER_SEQ": "1",
            "FAKE_CLI_RECORD": str(record),
            provider_env: f"{sys.executable} {fake_cli}",
        })

        result = subprocess.run(
            [sys.executable, str(WORKER), "--provider", provider],
            cwd=str(project),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        output_text = output.read_text(encoding="utf-8") if output.is_file() else ""
        payload = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
        return result, output_text, payload


def test_claude_provider_writes_stdout_to_expected_output():
    result, output_text, payload = run_worker("claude", "ND_CLAUDE_BIN")

    assert result.returncode == 0, result.stdout
    assert output_text.startswith("# generated artifact")
    assert "-p" in payload["argv"]
    assert "--output-format" in payload["argv"]
    assert "任务包内容" in payload["stdin"]
    assert "Write the target file." in payload["stdin"]


def test_agy_provider_uses_print_mode_and_writes_output():
    result, output_text, payload = run_worker("agy", "ND_AGY_BIN")

    assert result.returncode == 0, result.stdout
    assert "analysis" in output_text
    assert "--print" in payload["argv"]
    assert "--print-timeout" in payload["argv"]
    assert "--add-dir" in payload["argv"]
    assert "任务包内容" in payload["stdin"]
