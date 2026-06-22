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

## 按需加载表 (Router)

当你要执行对应的任务时，请**务必先读取**右侧指定的参考文档和模板：

| 任务阶段 | 需要读取的参考文件 / 模板 / Schema |
| :--- | :--- |
| **初始化与流程了解** | 读取 `references/process_overview.md` 了解全局流程。 |
| **执行章节分析** | 读取 `references/chapter_analysis.md` (规则) 和 `prompts/chapter_analysis.j2` (模板)。 |
| **提取本章 Delta** | 读取 `references/delta_extraction.md` (规则), `prompts/delta_extract.j2` (模板) 以及 `schemas/delta.schema.json`。 |
| **周期性结构审计** | 读取 `references/governance.md` (规则), `prompts/audit.j2` (模板) 以及 `schemas/audit_correction.schema.json`。 |
| **整理最终交付物** | 读取 `references/finalization.md` (规则) 和 `prompts/final_draft.j2` (模板)。 |
| **查阅全局结构规范** | 读取 `references/story_structure_spec.md`。 |
| **查阅故事质量治理** | 读取 `references/story_structure_quality_governance.md`。 |
| **提取视觉资产/分镜** | 读取 `references/visual_asset_spec.md`。 |
| **进行全书综合分析** | 读取 `references/fullbook_analysis_workbench.md` 或 `references/analysis_four_dimensions.md`，执行命令参考 `references/analysis_workbench_commands.md`。 |
