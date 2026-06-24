# v6 深度修复说明

本版本针对 v5 暴露的两个核心问题做工程级修复：

1. 强 Schema 可能导致历史信息被模型手动删掉。
2. `run_pipeline.py` 名称超前，但缺少真正逐章主控，导致运行几章后停止且恢复路径不清晰。

## 1. 保全式规范化：normalize_story_schema.py

新增：

```bash
python scripts/normalize_story_schema.py <story.json> --out <story.normalized.json>
python scripts/normalize_story_schema.py <story.json> --in-place --backup
python scripts/normalize_story_schema.py <story.json> --in-place --quarantine-invalid-refs
```

它不会删除非标准信息，而是迁移：

- 顶层规范外字段 → `介绍.描述`
- 元素规范外字段 → `详情`
- `详情` 中的数字、布尔、对象 → 字符串
- 非“类型:名称”的详情数组 → 合并为字符串
- 缺失标准字段 → 补默认值
- 别名引用 → 尝试替换为正式名称
- 无效结构引用 → 可选迁入 `详情.待确认引用`

因此强 Schema 不再直接制造“删字段才能通过”的压力。

## 2. 三档校验模式

`validate_structure.py` 改为三档：

```bash
python scripts/validate_structure.py --mode process 故事结构_增量.json
python scripts/validate_structure.py --mode governance 故事结构_增量.json
python scripts/validate_structure.py --mode final 故事结构.json
```

含义：

- `process`：逐章过程库。字段、类型、详情规范硬卡；前向引用和数量不足为 warning。
- `governance`：周期/按需治理后。引用错误升级为 error；数量不足仍提醒。
- `final`：最终交付。强 Schema、引用、最低数量、介绍完整性均为 error。

`--strict` 等价于 `--mode final`。

## 3. Delta 校验也支持三档

```bash
python scripts/validate_delta.py --mode process 当前结构.json 本章Delta.json
python scripts/validate_delta.py --mode governance 当前结构.json 治理补丁.json
```

逐章阶段不再因为未来事件/未来关系暂未入库而硬停；但会留下 warning，最终前必须治理。

## 4. 真正的逐章主控器

`run_pipeline.py` 已从“分析任务包工具”升级为逐章主控器：

```bash
python scripts/run_pipeline.py split <项目目录> <原文文件>
python scripts/run_pipeline.py resume <项目目录>
python scripts/run_pipeline.py prepare-chapter <项目目录> --chapter 12
python scripts/run_pipeline.py commit-chapter <项目目录> --chapter 12
python scripts/run_pipeline.py run <项目目录>
```

`run` 的真实语义：

- 如果某章的章节分析MD和Delta JSON已经存在，则自动校验、合并、规范化、快照、diff、更新进度。
- 如果缺章节分析MD，则生成 `task_chNNN_analysis.md`；MD完成后缺Delta JSON，则生成 `task_chNNN_delta.md`。两个任务包分别对应两次模型分析。
- 脚本不假装能自己调用大模型；它保证的是“可断点、可恢复、知道停在哪里、知道缺什么”。

## 5. 失败修复任务包

当 Delta 校验失败、合并失败或合并后结构校验失败时，会生成：

```text
章节处理/_修复任务/repair_chNNN.md
```

模型只需要根据修复任务包修本章 Delta，不需要重跑整章，也不允许重写完整故事结构。

## 6. 快照与 diff 闭环

每章成功提交后必须生成：

```text
故事结构版本/story_before_chNNN.json
故事结构版本/story_after_chNNN.json
结构变更日志/diff_chNNN.json
质量治理/delta校验/第NNN章_xxx.txt
质量治理/章节校验/第NNN章_xxx.txt
```

## 7. 章节拆分增强

`split_chapters.py` 支持：

```bash
--pattern 自定义章节正则
--preface-mode separate|attach|chapter|drop
--sample-lines 500
--min-matches 2
```

默认 `separate` 会把前言写为 `_前言.md`，不再挤占第001章编号。

## 8. 治理补丁提交

新增/保留治理操作执行：

```bash
python scripts/run_pipeline.py commit-governance <项目目录> <治理补丁.json>
```

会自动完成：

1. 保存治理前快照
2. `validate_delta.py --mode governance`
3. `merge_delta.py`
4. `apply_governance_ops.py`
5. `normalize_story_schema.py`
6. `validate_structure.py --mode governance`
7. 保存治理后快照和 diff

## 9. 推荐运行方式

逐章拆书：

```bash
python scripts/run_pipeline.py split <项目目录> <原文文件>
python scripts/run_pipeline.py run <项目目录>
```

本版本改为 Agent-first 接力：周期点生成审计任务包后，`run` 返回 `2`；主控Agent直接产出真实 `correction_XXX.json` 并再次运行 `run`。随后脚本执行 `validate_delta`、`merge_delta`、`apply_governance_ops`、`validate_structure`，全部通过后继续下一章。

周期审计不再依赖外部 worker。缺少或无效补丁时，流程保持在待办状态，不会越过审计进入下一章。`commit-governance` 仍可用于单独提交按需治理补丁，其报告保存到 `质量治理/按需治理/`；周期补丁的报告保存在 `质量治理/周期审计/`。

当 `run` 暂停时，打开任务包，产出它要求的两个文件：

```text
章节处理/第NNN章_xxx.md
章节处理/第NNN章_xxx.json
```

然后再次执行：

```bash
python scripts/run_pipeline.py run <项目目录>
```

最终交付前：

```bash
python scripts/run_pipeline.py normalize <项目目录> --quarantine-invalid-refs
python scripts/validate_structure.py --mode final <项目目录>/故事结构.json
```
