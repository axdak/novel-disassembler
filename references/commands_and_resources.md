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
summary → characters → plot → worldview → settings → plotlines → outline → detailed_outline → visual_assets → report
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

### scripts/
- `split_chapters.py`：章节拆分脚本，自动检测章节模式并拆分，支持 txt/docx。
- `merge_delta.py`：Delta合并脚本，将单章Delta字段级深度合并到增量JSON，只增不删，支持别名匹配。
- `validate_delta.py`：Delta入库前质量校验脚本，检查schema、字段、类型、重复、交叉引用和新增/修改语义。
- `validate_structure.py`：结构验证脚本，支持全量验证、`--strict`、`--clean`、`--chapter-check`；其中 `--chapter-check` 是每切片合并后的全量过程库检查，并附带本切片触碰元素统计。
- `progress_manager.py`：进度管理脚本，初始化、查询、更新、断点恢复、摘要生成。
- `run_pipeline.py`：主控器，推进章节、生成任务包、提交章节/治理/最终交付。
- `test_merge_delta.py`：merge_delta 深度合并行为测试。
- `test_validate_delta.py`：validate_delta 与 chapter-check 测试。

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
