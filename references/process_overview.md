# 流程总览与文件结构

## 概述

本 Skill 将小说原文（支持 txt、docx 等格式）拆分为章节，然后采用**逐章串行 + 分层质量治理**的方式生成高质量故事结构。

核心变化：**章节MD分析和JSON结构提取分开执行**。

每章处理不再让一个调用同时写 MD 和 JSON，而是拆成两步：

```text
当前章节原文
↓
章节分析器：输出 第N章_章节分析.md
↓
当前故事结构_增量.json + 当前章节原文 + 第N章_章节分析.md
↓
Delta提取器：输出 第N章_delta.json
↓
validate_delta.py 入库前校验
↓
merge_delta.py 确定性合并
↓
validate_structure.py --chapter-check 合并后全量过程库校验
↓
保存快照、更新进度
```

本 Skill **不允许每章让模型重写完整故事结构JSON**。每章只允许模型输出本章 Delta/Patch。完整 `故事结构.json` 只在最终阶段由全书结构整理器生成草稿，并经过强校验后交付。

## 核心设计决策

| 决策 | 选择 | 原因 |
|:---|:---|:---|
| 处理粒度 | **逐切片/逐单元串行** | 原文可按章、回、节、数字编号、幕等切分；每个处理产物只能对应 `_索引.json` 中一个 `seq`，方便断点恢复 |
| MD与JSON | **分开输出** | 降低单次任务复杂度，提升章节理解和结构化质量 |
| JSON更新 | **Delta/Patch增量** | 避免每章全量重写导致上下文膨胀、旧信息丢失、静默篡改 |
| 入库前治理 | **validate_delta.py** | 低质量Delta不允许合并，防止污染全局结构 |
| 合并方式 | **merge_delta.py确定性合并** | 字段级深度合并，只增不删，别名命中已有元素 |
| 合并后治理 | **validate_structure.py --chapter-check** | 每个切片合并后对整个 `故事结构_增量.json` 跑 `process` 级全量检查，并报告本切片触碰元素数量，保证每个快照都是全局过程库可读、可继续的状态 |
| 周期审计 | **每5-10章结构审计** | 跨章节合并重复角色、降级路人、整理伏笔和关系 |
| 最终交付 | **过程JSON ≠ 最终JSON** | `故事结构_增量.json` 是过程数据库，`故事结构.json` 是最终高质量交付 |
| 断点机制 | **文件 + 快照 + 进度JSON** | 每章产物可追踪、可回滚、可定位问题章节 |

## 主控Agent与执行单元分工

```text
主控Agent（流程控制层）
  ├── 初始化项目、维护进度.json
  ├── 调度章节拆分脚本
  ├── 逐切片串行调度章节分析器与Delta提取器
  ├── 每章调用validate_delta.py、merge_delta.py、validate_structure.py --chapter-check
  ├── 每章保存故事结构快照和质量报告
  ├── 每5-10章触发结构审计
  ├── 随时调度全书/局部分析工作台
  └── 调度最终结构整理与强校验

章节分析器（文学理解层）
  ├── 每次只读1个原文切片（`_索引.json` 中的一个 seq）
  ├── 输出10维度章节分析MD
  └── 不输出JSON、不维护全局结构

Delta提取器（结构提取层）
  ├── 读取当前故事结构_增量.json或摘要
  ├── 读取当前章节原文
  ├── 读取当前章节分析MD
  ├── 只输出本章新增/修改的Delta JSON
  └── 不输出完整故事结构JSON

结构审计器（质量治理层）
  ├── 每5-10章读取当前增量JSON、最近章节分析、最近Delta
  ├── 检查重复元素、低质量元素、伏笔质量、事件粒度、关系变化
  └── 输出 correction_delta.json，不直接重写完整JSON

最终结构整理器（最终交付层）
  ├── 读取故事结构_增量.json、全书分析、审计报告
  ├── 输出故事结构_草稿.json
  ├── 根据validate_structure.py报告修正
  └── 生成最终故事结构.json
```

**核心原则**：主控Agent不依赖对话记忆保存状态；跨章节信息全部沉淀到文件；模型负责文学判断和结构化提取，脚本负责校验、合并、断点、回滚。

## 文件结构

在**小说文件所在目录**下创建 `拆书_<书名>/` 项目目录：

```text
<小说文件所在目录>/
├── <小说文件>.txt
└── 拆书_<书名>/
    ├── 进度.json
    ├── 原文/
    │   └── <小说文件>
    ├── 原文拆解/
    │   ├── _索引.json
    │   ├── 第001章_<标题>.md
    │   └── ...
    ├── 章节处理/
    │   ├── 第001章_<标题>.md          # 章节10维度分析
    │   ├── 第001章_<标题>.json        # 本章Delta
    │   ├── 第001章_<标题>.validation.json  # Delta/章节校验报告，可选
    │   └── ...
    ├── 质量治理/
    │   ├── delta校验/
    │   │   ├── 第001章_<标题>.json
    │   │   └── ...
    │   ├── 章节校验/
    │   │   ├── 第001章_<标题>.json
    │   │   └── ...
    │   ├── 周期审计/
    │   │   ├── audit_001-005.md
    │   │   ├── correction_001-005.json
    │   │   └── ...
    │   └── 最终审计/
    │       ├── final_audit.md
    │       ├── final_correction_delta.json
    │       └── validate_report.txt
    ├── 故事结构版本/
    │   ├── story_after_ch001.json
    │   ├── story_after_ch002.json
    │   └── ...
    ├── 结构变更日志/
    │   ├── diff_ch001.json
    │   ├── diff_ch002.json
    │   └── ...
    ├── 全书分析/
    │   ├── 剧情结构/
    │   │   ├── 章节梗概汇总.md
    │   │   ├── 剧情线索.md
    │   │   ├── 伏笔追踪.md
    │   │   └── 冲突图谱.md
    │   ├── 人物分析/
    │   │   ├── 人物档案.md
    │   │   ├── 人物关系.md
    │   │   └── 人物提及.json
    │   ├── 风格分析/
    │   │   ├── 文风分析.md
    │   │   ├── 节奏分析.md
    │   │   └── 高光场景.md
    │   ├── 视觉资产/
    │   │   ├── 视觉资产清单.md
    │   │   ├── 关键场景分镜表.md
    │   │   ├── AI绘图提示词素材.md
    │   │   ├── 角色外观一致性表.md
    │   │   └── 场景氛围表.md
    │   └── 拆书总报告.md
    ├── 故事结构_增量.json            # 过程数据库
    ├── 故事结构_草稿.json            # 最终整理草稿
    └── 故事结构.json                 # 最终交付
```

## 流程总览

| 步骤 | 名称 | 执行者 | 可中断 | 断点粒度 |
|------|------|--------|--------|----------|
| 1 | 章节拆分 | 主控Agent + split_chapters.py | 否 | 无 |
| 2 | 逐切片分析、提取、质量治理 | 主控Agent + 章节分析器 + Delta提取器 + 校验/合并脚本 | 是 | **逐切片** |
| 2Q | 周期结构审计 | 结构审计器 + validate_delta.py + merge_delta.py | 是 | **每5-10章** |
| A | 全书/局部分析工作台 | analysis_context_pack.py + 分析器 | 是 | **按任务包/按章节范围** |
| 3 | 基准全书报告固化 | 主控Agent调度 + 分析器 | 是 | **按报告类型** |
| 4 | 最终结构整理与强校验 | 最终结构整理器 + validate_structure.py | 是 | 按验证轮次 |

## 前置：初始化项目

1. 确认用户的小说文件路径。
2. 在小说文件所在目录下创建 `拆书_<书名>/` 项目目录。
3. 创建项目子目录结构：`原文/`、`原文拆解/`、`章节处理/`、`质量治理/`、`故事结构版本/`、`结构变更日志/`、`全书分析/` 等。
4. 复制小说文件到 `原文/` 目录。
5. 运行：

```bash
python <skill_path>/scripts/progress_manager.py init <项目目录> <源文件路径>
```

## 步骤1：章节拆分

**目标**：将小说原文按章节标题拆分为多个 md 文件。

```bash
python <skill_path>/scripts/split_chapters.py <原文文件> <项目目录>/原文拆解
```

脚本会：
- 自动检测文件格式，支持 `.txt` 和 `.docx`。
- txt 自动检测编码。
- docx 通过解析 Word XML 提取纯文本。
- 读取前500行检测章节标题模式。
- 支持“第X章”“Chapter X”“数字序号”“序章/尾声/番外”等常见格式。
- 输出章节 md 文件和 `_索引.json`。

完成后：
```bash
python <skill_path>/scripts/progress_manager.py init-incremental <项目目录>
python <skill_path>/scripts/progress_manager.py complete <项目目录> 1
```
注意：`故事结构_增量.json` 必须用 `init-incremental` 初始化，不要由主控Agent手写骨架。

## 单章执行流程

对每个 `_索引.json` 中的切片按行文顺序执行。这里的“第001章”只是系统内部顺序号，不要求原文标题必须是“第X章”；原文标题可以是数字编号、回、节、幕、序章、番外等。**但单个章节分析MD和Delta JSON只能对应一个 seq，禁止把多个切片合并成一个 `第003-005章_*.md/json` 产物。**

```text
1. 读取 原文拆解/_索引.json，确定当前章节文件名
2. 读取当前章节原文
3. 调用章节分析器，输出 章节处理/第N章_<标题>.md
4. 读取当前 故事结构_增量.json 或元素摘要
5. 调用Delta提取器，输入：当前故事结构 + 当前章节原文 + 当前章节分析MD
6. 输出 章节处理/第N章_<标题>.json
7. 运行 validate_delta.py 入库前校验
8. 校验失败：只重跑Delta提取器，不重跑章节分析器
9. 校验通过：合并前复制 故事结构_增量.json 为临时备份
10. 运行 merge_delta.py 合并Delta
11. 运行 validate_structure.py --chapter-check 合并后全量过程库校验
12. 校验失败：用临时备份回滚，重跑Delta提取器
13. 校验通过：保存 story_after_chNNN.json 快照和 diff_chNNN.json
14. 更新 进度.json，将该章加入已完成章节
15. 每5章向用户汇报进度，并触发周期结构审计
```

## 每章成功提交的硬产物

一章可信完成必须同时存在：

```text
章节处理/第NNN章_xxx.md
章节处理/第NNN章_xxx.json
质量治理/delta校验/第NNN章_xxx.txt
质量治理/章节校验/第NNN章_xxx.txt
故事结构版本/story_before_chNNN.json
故事结构版本/story_after_chNNN.json
结构变更日志/diff_chNNN.json
```

缺任何一个，都不算可信完成。

## 步骤3：基准全书报告固化

步骤3不再代表“唯一一次全书分析”，而是代表：当主控Agent认为当前阶段已经足够稳定时，把 A 工作台中生成的分析结果固化为一版基准报告。

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

## 主控器运行方式

`run_pipeline.py` 不直接调用大模型。它负责推进已经具备产物的章节；遇到缺章节分析MD或Delta JSON时，会生成任务包并暂停。

```bash
python <skill_path>/scripts/run_pipeline.py split <项目目录> <原文文件>
python <skill_path>/scripts/run_pipeline.py run <项目目录>
```

暂停后，根据任务包产出：
```text
章节处理/第NNN章_xxx.md
章节处理/第NNN章_xxx.json
```

然后继续：
```bash
python <skill_path>/scripts/run_pipeline.py run <项目目录>
```

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

最终整理与交付（步骤4）：
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

## 断点续传操作指南

当用户再次发起拆书请求且项目已存在时：

1. 读取 `进度.json`。
2. 运行：
```bash
python <skill_path>/scripts/progress_manager.py resume <项目目录>
python <skill_path>/scripts/progress_manager.py reconcile <项目目录>
```
3. 检查 `章节处理/` 中每章 MD 和 JSON 是否齐全。
4. 对文件齐全但进度未记录的章节，先运行 `validate_delta.py`，再决定是否重新合并。
5. 若不确定某章是否污染结构，优先使用 `故事结构版本/story_after_chNNN.json` 快照回滚到最近可信章节。
6. 从第一个未完成章节继续处理。
7. 具体恢复逻辑见 `references/checkpoint_mechanism.md`。

## 进度汇报规范

每完成5章或一次周期审计后，主控Agent应向用户汇报：

```text
进度更新
- 已完成：{已完成章数}/{总章数} 章 ({百分比}%)
- 当前元素：角色{N} | 事件{N} | 地点{N} | 线索{N} | 阵营{N} | 物品{N}
- 最近5章新增：角色{+N} | 事件{+N} | 地点{+N} | 线索{+N} | 阵营{+N} | 物品{+N}
- 质量状态：Delta校验通过{N}章 | 章节校验通过{N}章 | 最近审计{通过/待修正}
- 剩余：{剩余章数} 章
```

## 防止上下文爆炸的原则

1. 主控Agent不读整本书。
2. 章节分析器一次只处理一章。
3. Delta提取器只输出本章Delta。
4. merge由脚本完成，不把完整新JSON交给模型重写。
5. 增量JSON过大时传摘要，按需读取详情。
6. 每章保存快照，出错可回滚。
7. 所有状态写入文件，不依赖对话记忆。

## 资源清单

### scripts/
- `split_chapters.py`：章节拆分脚本，自动检测章节模式并拆分，支持 txt/docx。
- `merge_delta.py`：Delta合并脚本，将单章Delta字段级深度合并到增量JSON，只增不删，支持别名匹配。
- `validate_delta.py`：Delta入库前质量校验脚本，检查schema、字段、类型、重复、交叉引用和新增/修改语义。
- `validate_structure.py`：结构验证脚本，支持全量验证、`--strict`、`--clean`、`--chapter-check`；其中 `--chapter-check` 是每切片合并后的全量过程库检查，并附带本切片触碰元素统计。
- `progress_manager.py`：进度管理脚本，初始化、查询、更新、断点恢复、摘要生成。
- `test_merge_delta.py`：merge_delta 深度合并行为测试。
- `test_validate_delta.py`：validate_delta 与 chapter-check 测试。

### references/
- `story_structure_spec.md`：故事结构JSON完整规范。
- `checkpoint_mechanism.md`：断点续传机制详细说明。
- `story_structure_quality_governance.md`：故事结构质量治理说明。
- `visual_asset_spec.md`：视觉资产、分镜和AI绘图素材提取规范。
- `v12_patch_notes.md`：本版分组与视觉资产增强说明。
