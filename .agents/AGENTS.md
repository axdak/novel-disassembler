# Novel Disassembler - Agent Route Rules

Before running an autonomous pipeline command, confirm the active route flag:
`--run-mode subagent`, `--run-mode serial`, or `--run-mode worker`.

These rules apply to:
- `run_pipeline.py run`
- `run_pipeline.py visual-assets-auto`
- `run_pipeline.py chapter-structure-auto`

Route behavior:
1. `subagent`: when the command returns `2`, the main agent must dispatch a child agent to read the current task pack and write the requested artifact(s). After the child finishes, the main agent immediately reruns the same command.
2. `serial`: when the command returns `2`, the main agent may read the current task pack and write the requested artifact(s) in the main session. After writing them, rerun the same command immediately.
3. `worker`: the task pack must be handled by the configured external worker through `--worker-provider`, `--worker-command`, `--chapter-command`, or `--audit-command`. The main agent monitors terminal output, worker logs, task paths, expected output paths, and status files, then retries or reports a hard blocker. It must not bypass the worker by hand-writing semantic artifacts.

Return code `2` is a normal handoff signal, not completion or failure. The main session must keep monitoring and rerunning until the command returns `0` and there is no follow-up flow, or until a real hard blocker appears.
