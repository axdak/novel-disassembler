---
name: novel-disassembler
description: 拆书技能——将小说原文按章节拆分，逐章生成MD章节分析，再基于当前故事结构JSON、本章原文和章节分析提取Delta，并通过validate_delta.py、merge_delta.py、validate_structure.py形成可校验、可回滚、可审计的故事结构JSON。适用于拆解小说、分析小说结构、提取角色/事件/地点/线索/阵营/物品、生成高质量故事JSON，并沉淀视觉资产/分镜/AI绘图素材。
agent_created: true
---

# Novel Disassembler — 拆书技能

你是拆书流程主控。不要一次读完整本书。根据用户任务**按需读取** references 目录中的详细规则。

## 固定原则

1. 每次只处理一个章节切片。
2. 章节分析 MD 和 Delta JSON 分开生成。
3. 模型只输出 Delta，不重写完整故事结构。
4. 合并、校验、断点、回滚由脚本负责。
5. 遇到具体任务时，按下表加载对应文档。
6. 用户要求持续拆书时，主控Agent必须自主循环：`run_pipeline.py run` 返回 `2` 表示有待办任务包，不是失败；立即完成任务包指定产物并再次运行，不在章节、修复或周期审计之间等待用户指令。若用户显式提供 `--chapter-command` / `--audit-command`，则由 `run_pipeline.py` 调用外部 worker 写当前任务包指定产物，主控Agent只监督总进度和硬阻塞。
7. 周期审计必须由主控Agent读取任务包后产出真实 `correction_XXX.json`；不得通过空 Delta、伪造 worker 或跳过审计来解除待办。
8. 单章必须分两次独立模型分析：`task_chNNN_analysis.md` 只产出章节分析MD；重新运行后生成的 `task_chNNN_delta.md` 才读取该MD并只产出Delta JSON。不得在分析任务中写Delta，也不得在Delta任务中重写分析MD。
   - 如需先批量完成全部章节分析，再进入结构 JSON 阶段，使用 `run_pipeline.py run --phase analysis` 循环到返回 `0`，再使用 `run_pipeline.py run --phase delta` 循环推进 Delta 与提交。未指定 `--phase` 时保持逐章交错流程。
9. 章节分析MD和Delta JSON的语义内容必须来自模型对本章原文、章节分析和当前结构索引的阅读理解。禁止编写或运行 Python/Bash/PowerShell 等脚本来批量生成、补全、扩写或替代角色、事件、地点、线索、阵营、物品、其他事项等语义内容。
10. 允许使用项目已有确定性修复脚本，或必要时使用一次性 format-only 修复脚本，处理已经存在的当前章节 Delta JSON 的语法、字符串转义、字段类型、章节时间、标签压缩等机械格式问题；这类脚本不得新增剧情事实、不得新增语义元素、不得读取原文生成内容、不得以提高效率或推进多章为目的复用。
11. 外部 worker 自动生成模式默认关闭；未显式配置 `--chapter-command` / `--audit-command` 前，`run_pipeline.py` 只生成任务包并由主控Agent/人工按任务包产出。配置后，脚本仍只信任校验结果，不信任 worker 自述成功。

## 按需加载表 (Router)

当你要执行对应的任务时，请**务必先读取**右侧指定的参考文档和模板：

| 任务阶段 | 需要读取的参考文件 / 模板 / Schema |
| :--- | :--- |
| **了解全局流程** | 读取 `references/process_overview.md`。 |
| **初始化项目 / 步骤1 拆章 / 每章硬产物** | 读取 `references/file_structure.md`。 |
| **持续自主执行 / 进度汇报** | 读取 `references/autonomous_loop.md` 与 `prompts/autonomous_run.j2`。 |
| **`run_pipeline.py` 命令、步骤3/4 命令、资源清单** | 读取 `references/commands_and_resources.md`。 |
| **断点续传 / 恢复** | 读取 `references/checkpoint_mechanism.md`。 |
| **执行章节分析** | 读取 `references/chapter_analysis.md` (规则) 和 `prompts/chapter_analysis.j2` (模板)。 |
| **提取本章 Delta** | 读取 `references/delta_extraction.md` (规则), `prompts/delta_extract.j2` (模板) 以及 `schemas/delta.schema.json`（仅作轻量格式提示；强校验见 `scripts/validate_delta.py` + `scripts/story_schema_rules.py`）。 |
| **周期性结构审计** | 读取 `references/governance.md` (规则), `prompts/audit.j2` (模板) 以及 `schemas/audit_correction.schema.json`（同上，仅作格式提示；强校验见 `scripts/validate_delta.py`）。 |
| **整理最终交付物** | 读取 `references/finalization.md` (规则) 和 `prompts/final_draft.j2` (模板)。 |
| **查阅全局结构规范** | 读取 `references/story_structure_spec.md`。 |
| **查阅故事质量治理** | 读取 `references/story_structure_quality_governance.md`。 |
| **提取视觉资产/分镜** | 读取 `references/visual_asset_spec.md`。 |
| **按章生成视觉资产 / 视觉资产自主循环** | 读取 `references/visual_asset_spec.md` 的「分章独立产物」段，以及 `references/analysis_workbench_commands.md` 中的 `--per-chapter` / `visual-assets-auto` 命令。 |
| **进行全书综合分析** | 读取 `references/fullbook_analysis_workbench.md` 或 `references/analysis_four_dimensions.md`，执行命令参考 `references/analysis_workbench_commands.md`。 |
| **执行故事结构维度（chapter_structure / narrative_structure）** | 读取 `references/analysis_narrative_structure.md`；执行命令参考 `references/analysis_workbench_commands.md` 的 chapter_structure / narrative_structure 段。 |
