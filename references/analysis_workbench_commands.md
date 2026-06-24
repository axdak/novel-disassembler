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

按章独立生成视觉资产（v12 新增；产物写入 `全书分析/视觉资产/分章/chNNN/`，不动顶层）：

```bash
# 指定单章
python scripts/run_pipeline.py analysis-pack <项目目录> --task visual_assets --chapters 12 --per-chapter

# 指定章节范围
python scripts/run_pipeline.py analysis-pack <项目目录> --task visual_assets --chapters 12-15 --per-chapter
```

从分章产物 reduce 出顶层五件套（手动 aggregate）：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task visual_assets --aggregate
```

自动按章顺序推进（推荐；和 run_pipeline.py run 一样的返回 2 自主循环模式）：

```bash
python scripts/run_pipeline.py visual-assets-auto <项目目录>
# 每次返回 2 交接一章；主控Agent按 reduce_prompt 写完 分章/chNNN/ 五件套后再次运行
# 所有 eligible 章节完成后自动切到 aggregate，再返回 2 交接顶层汇总写入
# 顶层汇总写完后返回 0 表示流程完成
```

### chapter_structure 分章生成

每章独立产出 `章节结构.md`（必须配合 `--per-chapter`）：

```bash
# 单章
python scripts/run_pipeline.py analysis-pack <项目目录> --task chapter_structure --chapters 12 --per-chapter

# 章节范围
python scripts/run_pipeline.py analysis-pack <项目目录> --task chapter_structure --chapters 1-50 --per-chapter

# 全书
python scripts/run_pipeline.py analysis-pack <项目目录> --task chapter_structure --chapters all --per-chapter
```

### narrative_structure 全书故事结构分析

7 份产物（三幕式结构图 / Brooks四部分结构图 / Freytag五段结构图 / 故事七要素档案 / 故事力学评估 / 故事工程学评估 / 小说骨架）：

```bash
python scripts/run_pipeline.py analysis-pack <项目目录> --task narrative_structure --chapters all
```

任务包会自动拼接「全书背景资料」段；如尚未生成 `chapter_structure` 分章产物或 `章节梗概汇总.md` / `全书大纲.md` / `剧情线总表.md`，会显式标注「未生成」，不会静默缺失。

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
