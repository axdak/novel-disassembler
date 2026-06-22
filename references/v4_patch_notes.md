# v4 修补说明：全书分析工作台化

## 核心变化

原先“步骤3：全书维度分析”被设计成固定顺序：

```text
3a -> 3b -> 3c -> 3d -> 3e
```

v4 改为：

```text
A：全书/局部分析工作台，随时可唤起、可分包、可归约、可局部刷新
步骤3：只负责把某一版分析结果固化为基准全书报告
```

## 新增脚本

### scripts/analysis_context_pack.py

用于生成分析任务包，支持：

- summary：章节梗概汇总
- characters：人物分析
- plot：剧情线/伏笔/冲突分析
- style：节奏与文风分析
- report：拆书总报告
- custom：自定义分析

特点：

1. 可指定章节范围。
2. 可指定目标元素。
3. 可包含原文 none/sample/full。
4. 可按 max-pack-chars 自动分包。
5. 自动生成 reduce_prompt.md。
6. 输出 manifest.json 记录任务信息。

### scripts/run_pipeline.py

新增主控辅助命令：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task characters --chapters 1-80
python scripts/run_pipeline.py analysis-status <项目目录>
python scripts/run_pipeline.py init-dirs <项目目录>
```

## 为什么这样改

大模型产出全书分析时，如果一次性输入：

```text
全书原文 + 全部章节分析 + 全量故事结构JSON
```

容易出现：

1. 上下文爆炸。
2. 早期章节被遗忘。
3. 人物和伏笔漏记。
4. 报告难以局部刷新。
5. 质量无法追踪证据。

v4 通过任务包解决：

```text
pack_001 -> partial_result_001
pack_002 -> partial_result_002
reduce_prompt + partial_results -> final report
```

## 质量规则

1. 所有分析结论必须带章节证据。
2. 不确定写“待确认”。
3. 不允许为了完整性编造人物关系、伏笔、冲突。
4. 文风分析必须看原文抽样。
5. 人物/剧情线分析必须结合故事结构JSON对应元素集。
