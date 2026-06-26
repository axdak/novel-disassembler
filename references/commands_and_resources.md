# 命令说明与资源清单

## 主控器运行方式

`run_pipeline.py` 不直接调用大模型。它负责推进已经具备产物的章节；遇到需要模型判断的章节、修复或审计工作时，会生成任务包并以返回码 `2` 交接给主控Agent。

```bash
python <skill_path>/scripts/run_pipeline.py split <项目目录> <原文文件>
python <skill_path>/scripts/run_pipeline.py run <项目目录>
```

返回 `2` 后，主控Agent必须根据当前阶段任务包立即产出：
```text
task_chNNN_analysis.md -> 章节处理/第NNN章_xxx.md
task_chNNN_delta.md -> 章节处理/第NNN章_xxx.json
```

然后继续：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录>
```

返回码、任务包优先级、周期审计的处理纪律见 `references/autonomous_loop.md`。

查看断点：
```bash
python <skill_path>/scripts/run_pipeline.py resume <项目目录>
```

提交单章：
```bash
python <skill_path>/scripts/run_pipeline.py commit-chapter <项目目录> --chapter N
```

按需/周期治理补丁提交：
```bash
python <skill_path>/scripts/run_pipeline.py commit-governance <项目目录> <治理补丁.json>
```

## 步骤3：基准全书报告固化

步骤3不再代表"唯一一次全书分析"，而是代表：当主控Agent认为当前阶段已经足够稳定时，把 A 工作台中生成的分析结果固化为一版基准报告。

推荐顺序仍然是：
```text
summary → characters → plot → worldview → settings → plotlines → outline → detailed_outline → visual_assets → chapter_structure → narrative_structure → report
```
但这只是报告依赖顺序，不是限制分析能力只能在步骤3执行。

完成某类报告后，可以记录状态：
```bash
python <skill_path>/scripts/progress_manager.py update <项目目录> 3 '{"已完成报告": ["summary", "characters"]}'
```

全部基准报告完成后：
```bash
python <skill_path>/scripts/progress_manager.py complete <项目目录> 3
```

## 步骤4：最终整理与交付

```bash
# 1. 生成最终整理任务包（复制增量为草稿 + final校验 + 打包全书分析/审计参考材料）
python <skill_path>/scripts/run_pipeline.py final-pack <项目目录>

# 2. 模型修改 故事结构_草稿.json 后，提交最终草稿（规范化 + strict校验，失败生成修复任务包）
python <skill_path>/scripts/run_pipeline.py commit-final-draft <项目目录>

# 3. 独立最终校验（可选，可指定 --file 校验不同文件）
python <skill_path>/scripts/run_pipeline.py validate-final <项目目录>

# 4. 校验通过后正式交付（前置strict校验 → 复制为故事结构.json → 完成步骤4 → 审计日志）
python <skill_path>/scripts/run_pipeline.py finalize <项目目录>
```

典型最终交付流程：
```text
final-pack → 模型根据任务包修改草稿 → commit-final-draft
  ↓ 失败：根据 repair_final.md 修正 → 再次 commit-final-draft
  ↓ 通过：finalize → 输出 故事结构.json，步骤4完成
```

## 资源清单

### scripts/（共 16 个）

**主控 & 进度**
- `run_pipeline.py`：主控器，推进章节、生成任务包、提交章节/治理/最终交付；不直接调用大模型。
- `progress_manager.py`：进度管理脚本，初始化、查询、更新、断点恢复、摘要生成。

**步骤 1：章节拆分**
- `split_chapters.py`：章节拆分脚本，自动检测章节模式并拆分，支持 txt/docx。

**步骤 2：逐章提取 & 合并**
- `repair_llm_json.py`：修复 LLM 生成的 JSON 语法（Markdown 代码块、单引号、尾逗号等），输出严格 JSON。
- `coerce_delta.py`：Delta 入库前机械纠错层，确定性类型/格式修复（性别映射、章节序号补零、时间推导等），减少 repair 循环。
- `compress_tags.py`：标签自动压缩，把带受控前缀的标签和超限自由标签降级到 `详情.补充标签`。
- `validate_delta.py`：Delta 入库前质量校验脚本，检查 schema、字段、类型、重复、交叉引用和新增/修改语义；支持 `--mode process/governance/final`。
- `merge_delta.py`：Delta 合并脚本，将单章 Delta 字段级深度合并到增量 JSON，只增不删，支持别名匹配。
- `validate_structure.py`：结构验证脚本，支持 `--mode process/governance/final` 三档模式、`--strict`（等价 `--mode final`）、`--chapter-check`；其中 `--chapter-check` 是每切片合并后的全量过程库检查，并附带本切片触碰元素统计。
- `normalize_story_schema.py`：保全式故事结构规范化，迁移非标准字段到详情、补齐默认值、别名规范化、无效引用隔离；不删信息。

**治理 & 审计**
- `apply_governance_ops.py`：执行治理补丁中的确定性操作：合并元素、重命名、替换引用、标签治理、降级、删除、设置字段。
- `diff_structure.py`：生成故事结构 before/after 差异报告（新增/删除/修改元素对比）。

**步骤 3：全书分析**
- `analysis_context_pack.py`：全书/局部分析上下文打包器，支持按章节范围/目标元素分包、reduce 归约，驱动全部分析任务类型。

**时间线 & 规则**
- `chronology.py`：章节序号、章节时间和事件分组的统一规则（库模块，被多个脚本引用）。
- `story_schema_rules.py`：故事结构 JSON Schema 常量与校验规则（库模块，被校验器和规范化脚本引用）。
- `migrate_chronology.py`：一次性迁移工具，将既有故事结构迁移到章节时间、紧凑追溯字段和事件顺序规范。

### references/
- `process_overview.md`：流程总览（核心决策、Agent分工、流程表、单章流程）。
- `file_structure.md`：项目文件结构、初始化、步骤1拆章、每章硬产物清单。
- `autonomous_loop.md`：自主循环执行纪律与进度汇报规范。
- `commands_and_resources.md`：本文档，命令说明与资源清单。
- `checkpoint_mechanism.md`：断点续传机制详细说明。
- `story_structure_spec.md`：故事结构JSON完整规范。
- `story_structure_quality_governance.md`：故事结构质量治理说明。
- `visual_asset_spec.md`：视觉资产、分镜和AI绘图素材提取规范。
- `v12_patch_notes.md`：本版分组与视觉资产增强说明。
