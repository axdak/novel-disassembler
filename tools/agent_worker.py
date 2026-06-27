#!/usr/bin/env python3
"""Generic external-agent worker for novel-disassembler task packages.

The pipeline supplies ND_* environment variables. This wrapper calls one
headless CLI provider, captures stdout, and writes exactly ND_EXPECTED_OUTPUT.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List


def default_agy_bin() -> str:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    return str(local) if local.is_file() else "agy"


def split_command(command: str) -> List[str]:
    return shlex.split(command, posix=False)


def provider_command(provider: str, project_dir: Path, timeout: str) -> List[str]:
    model = os.environ.get("ND_AGENT_MODEL", "").strip()
    if provider == "claude":
        parts = split_command(os.environ.get("ND_CLAUDE_BIN", "claude")) + [
            "-p",
            "--output-format",
            "text",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
        ]
        if model:
            parts.extend(["--model", model])
        return parts
    if provider == "agy":
        parts = split_command(os.environ.get("ND_AGY_BIN", default_agy_bin())) + [
            "--print",
            "--print-timeout",
            timeout,
            "--add-dir",
            str(project_dir),
        ]
        if os.environ.get("ND_AGY_SKIP_PERMISSIONS", "1") != "0":
            parts.append("--dangerously-skip-permissions")
        if model:
            parts.extend(["--model", model])
        return parts
    raise ValueError(f"unsupported provider: {provider}")


def build_prompt(task_type: str, task_pack: Path, expected_output: Path) -> str:
    task_text = task_pack.read_text(encoding="utf-8", errors="ignore")
    suffix = "JSON 本体" if expected_output.suffix.lower() == ".json" else "Markdown 本体"
    return f"""你是 novel-disassembler 的受控外部 worker。

当前任务类型：{task_type}
任务包路径：{task_pack}
期望输出文件：{expected_output}

硬规则：
1. 只根据下面任务包生成期望输出文件的完整内容。
2. 不要解释，不要总结，不要输出额外旁白。
3. 目标文件类型要求：只输出{suffix}。
4. 不要修改 故事结构_增量.json。
5. 不要创建脚本、批处理器、调度器或额外产物。
6. 不要处理任务包以外的章节。
7. 如果任务包要求修复，只重写当前期望输出文件对应的内容。

任务包内容：

{task_text}
"""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Run claude/agy as a controlled task-package worker.")
    parser.add_argument("--provider", choices=["claude", "agy"], default=os.environ.get("ND_AGENT_PROVIDER", "claude"))
    parser.add_argument("--print-timeout", default=os.environ.get("ND_AGENT_PRINT_TIMEOUT", "30m"))
    args = parser.parse_args()

    task_type = required_env("ND_TASK_TYPE")
    project_dir = Path(required_env("ND_PROJECT_DIR"))
    task_pack = Path(required_env("ND_TASK_PACK"))
    expected_output = Path(required_env("ND_EXPECTED_OUTPUT"))
    if not task_pack.is_file():
        print(f"task pack does not exist: {task_pack}", file=sys.stderr)
        return 1

    prompt = build_prompt(task_type, task_pack, expected_output)
    command = provider_command(args.provider, project_dir, args.print_timeout)
    result = subprocess.run(
        command,
        input=prompt,
        cwd=str(project_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode

    content = (result.stdout or "").strip()
    if not content:
        print("provider produced empty output", file=sys.stderr)
        return 1
    expected_output.parent.mkdir(parents=True, exist_ok=True)
    expected_output.write_text(content + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
