# 全书/局部分析工作台常用命令

生成章节梗概任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task summary --chapters 1-50 --include-original none
```

生成人物分析任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task characters --chapters 1-120 --targets 角色:林婉 角色:沈墨 --include-original sample
```

生成剧情线/伏笔任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task plot --chapters all --max-pack-chars 70000
```

生成文风节奏任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task style --chapters 1-30 --include-original sample
```

生成视觉资产任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task visual_assets --chapters all --include-original sample
```

生成世界观提取任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task worldview --chapters all --include-original sample
```

生成设定体系任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task settings --chapters all --include-original sample
```

生成剧情线梳理任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task plotlines --chapters all
```

生成大纲反推任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task outline --chapters all
```

生成细纲反推任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task detailed_outline --chapters 1-80
```

生成自定义分析任务包：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task custom --chapters 1-80 --question "分析主角成长线是否断裂"
```

查看分析状态：

```bash
python scripts/run_pipeline.py analysis-status <项目目录>
```

## 上下文空间控制

`analysis_context_pack.py` 默认不会把全书一次性塞给模型，而是按字符预算自动切包：

```bash
python scripts/analysis_context_pack.py <项目目录> \
  --task characters \
  --chapters all \
  --include-original sample \
  --max-pack-chars 70000 \
  --max-original-chars 6000 \
  --max-analysis-chars 12000
```

输出目录：

```text
全书分析/_任务包/<时间戳>_<task>/
├── manifest.json
├── pack_001.md
├── pack_002.md
└── reduce_prompt.md
```

执行方式：

```text
pack_001.md -> partial_result_001.md
pack_002.md -> partial_result_002.md
...
reduce_prompt.md + 所有 partial_result -> 最终报告
```
