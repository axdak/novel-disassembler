#!/usr/bin/env python3
"""
拆书主控器（可断点、可恢复、不会假装自动调用大模型）。

它负责：
- 初始化/拆章/目录/进度；
- 生成单章任务包，让模型填写章节分析MD和Delta JSON；
- 对已经填写好的章节产物自动校验、合并、规范化、快照、diff、更新进度；
- 失败时回滚并生成 repair_prompt；
- 全书/局部分析任务包生成。

重要：脚本本身不直接调用大模型。所谓“持续运行”指：它会持续提交已具备产物的章节；遇到缺章节分析或Delta时，生成任务包并明确暂停点。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chronology import chapter_time
from story_schema_rules import COLLECTION_KEYS
from validate_structure import validate_structure_file

REQUIRED_DIRS = [
    "原文", "原文拆解", "章节处理", "章节处理/_任务包", "章节处理/_修复任务",
    "质量治理/delta校验", "质量治理/章节校验", "质量治理/规范化", "质量治理/周期审计", "质量治理/按需治理", "质量治理/最终审计",
    "故事结构版本", "结构变更日志",
    "全书分析/_任务包", "全书分析/剧情结构", "全书分析/人物分析", "全书分析/风格分析", "全书分析/世界观", "全书分析/视觉资产", "全书分析/迭代版本", "全书分析/局部分析",
]
ANALYSIS_OUTPUTS = {
    "summary": ["全书分析/剧情结构/章节梗概汇总.md"],
    "characters": ["全书分析/人物分析/人物档案.md", "全书分析/人物分析/人物关系.md", "全书分析/人物分析/人物提及.json"],
    "plot": ["全书分析/剧情结构/剧情线索.md", "全书分析/剧情结构/伏笔追踪.md", "全书分析/剧情结构/冲突图谱.md"],
    "style": ["全书分析/风格分析/文风分析.md", "全书分析/风格分析/节奏分析.md", "全书分析/风格分析/高光场景.md"],
    "report": ["全书分析/拆书总报告.md"],
    "worldview": ["全书分析/世界观/世界观档案.md"],
    "settings": ["全书分析/世界观/设定档案.md"],
    "plotlines": ["全书分析/剧情结构/剧情线总表.md", "全书分析/剧情结构/剧情线交汇矩阵.md"],
    "outline": ["全书分析/剧情结构/全书大纲.md"],
    "detailed_outline": ["全书分析/剧情结构/章节细纲.md"],
    "visual_assets": ["全书分析/视觉资产/视觉资产清单.md", "全书分析/视觉资产/关键场景分镜表.md", "全书分析/视觉资产/AI绘图提示词素材.md", "全书分析/视觉资产/角色外观一致性表.md", "全书分析/视觉资产/场景氛围表.md"],
}
MULTI_UNIT_ARTIFACT_RE = re.compile(r"^第\d+\s*(?:-|—|–|~|～|至|到)\s*\d+章_.*\.(?:md|json)$")
INTERNAL_COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_AUDIT_TIMEOUT_SECONDS = 900


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
    return parsed


def audit_timeout_seconds_arg(value: str) -> int:
    parsed = int(value)
    if not 600 <= parsed <= 1800:
        raise argparse.ArgumentTypeError("必须在 600 到 1800 秒之间")
    return parsed


def script_path(name: str) -> Path:
    return Path(__file__).resolve().parent / name


def timeout_output(exc: subprocess.TimeoutExpired, timeout_seconds: float) -> str:
    """Return any partial output together with a stable timeout diagnostic."""
    output = exc.stdout or ""
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return f"{output}\n命令执行超时（{timeout_seconds:g} 秒），已终止。\n"


def run_cmd(
    cmd: List[str],
    report_path: Optional[Path] = None,
    timeout: float = INTERNAL_COMMAND_TIMEOUT_SECONDS,
) -> int:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        output = proc.stdout or ""
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = timeout_output(exc, timeout)
        returncode = 124
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(output or "", encoding="utf-8")
    else:
        print(output)
    return returncode


def init_dirs(project_dir: Path) -> None:
    for d in REQUIRED_DIRS:
        (project_dir / d).mkdir(parents=True, exist_ok=True)


def find_multi_unit_artifacts(project_dir: Path) -> List[Path]:
    """查找把多个拆分单元合并成一个产物的非法章节处理文件。"""
    chapter_dir = project_dir / "章节处理"
    if not chapter_dir.is_dir():
        return []
    return sorted(
        p for p in chapter_dir.iterdir()
        if p.is_file() and MULTI_UNIT_ARTIFACT_RE.match(p.name)
    )


def guard_single_unit_artifacts(project_dir: Path) -> bool:
    bad = find_multi_unit_artifacts(project_dir)
    if not bad:
        return True
    print("发现非法多切片合并产物，已停止。步骤2必须逐切片处理，每个Delta只能对应 _索引.json 中一个 seq。")
    for path in bad:
        print(f"  - {path}")
    print("请删除或拆分这些文件，再使用 run_pipeline.py prepare-chapter/run 生成并提交单切片产物。")
    return False


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def ensure_story(project_dir: Path) -> Path:
    init_dirs(project_dir)
    story = project_dir / "故事结构_增量.json"
    if not story.is_file():
        run_cmd([sys.executable, str(script_path("progress_manager.py")), "init-incremental", str(project_dir)])
    # 保全式规范化，避免历史字段被强 schema 卡死。
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place"], project_dir / "质量治理" / "规范化" / "normalize_latest.txt")
    return story


def chapter_files(project_dir: Path) -> List[Path]:
    split_dir = project_dir / "原文拆解"
    index_path = split_dir / "_索引.json"
    if index_path.is_file():
        index = load_json(index_path, {})
        records = index.get("chapters", []) if isinstance(index, dict) else []
        indexed: List[Tuple[int, Path]] = []
        seen = set()
        for record in records:
            if not isinstance(record, dict) or record.get("is_preface"):
                continue
            seq, filename = record.get("seq"), record.get("filename")
            if not isinstance(seq, int) or seq < 1 or not isinstance(filename, str) or not filename:
                raise ValueError("_索引.json 包含无效章节记录；seq 和 filename 必须有效")
            if seq in seen:
                raise ValueError(f"_索引.json 存在重复seq[{seq}]，无法作为唯一章节依据")
            seen.add(seq)
            path = split_dir / filename
            if path.is_file():
                indexed.append((seq, path))
        if indexed:
            return [path for _, path in sorted(indexed)]
    files = [p for p in split_dir.glob("第*章_*.md") if p.is_file()]
    def key(p: Path):
        m = re.match(r"第(\d+)章", p.name)
        return int(m.group(1)) if m else 10**9
    return sorted(files, key=key)


def seq_from_file(path: Path) -> int:
    m = re.match(r"第(\d+)章", path.name)
    if not m:
        raise ValueError(f"无法从文件名提取章节号: {path.name}")
    return int(m.group(1))


def chapter_by_seq(project_dir: Path, seq: int) -> Optional[Path]:
    index_path = project_dir / "原文拆解" / "_索引.json"
    if index_path.is_file():
        index = load_json(index_path, {})
        records = index.get("chapters", []) if isinstance(index, dict) else []
        matches = [record for record in records if isinstance(record, dict) and not record.get("is_preface") and record.get("seq") == seq]
        if len(matches) > 1:
            raise ValueError(f"_索引.json 存在重复seq[{seq}]，已停止章节处理")
        if len(matches) == 1:
            filename = matches[0].get("filename")
            if isinstance(filename, str) and filename:
                path = project_dir / "原文拆解" / filename
                return path if path.is_file() else None
            raise ValueError(f"_索引.json 的seq[{seq}]缺少filename")
    for p in chapter_files(project_dir):
        if seq_from_file(p) == seq:
            return p
    return None


def artifact_paths(project_dir: Path, chapter: Path) -> Dict[str, Path]:
    seq = seq_from_file(chapter)
    base = chapter.stem
    return {
        "chapter": chapter,
        "analysis": project_dir / "章节处理" / chapter.name,
        "delta": project_dir / "章节处理" / f"{base}.json",
        "delta_report": project_dir / "质量治理" / "delta校验" / f"{base}.json",
        "chapter_report": project_dir / "质量治理" / "章节校验" / f"{base}.json",
        "before": project_dir / "故事结构版本" / f"story_before_ch{seq:03d}.json",
        "after": project_dir / "故事结构版本" / f"story_after_ch{seq:03d}.json",
        "diff": project_dir / "结构变更日志" / f"diff_ch{seq:03d}.json",
        "task": project_dir / "章节处理" / "_任务包" / f"task_ch{seq:03d}.md",
        "repair": project_dir / "章节处理" / "_修复任务" / f"repair_ch{seq:03d}.md",
    }


def is_chapter_complete(project_dir: Path, chapter: Path) -> bool:
    p = artifact_paths(project_dir, chapter)
    required = ["analysis", "delta", "delta_report", "chapter_report", "before", "after", "diff"]
    if not all(p[k].is_file() and p[k].stat().st_size > 0 for k in required):
        return False

    try:
        for report_key in ("delta_report", "chapter_report"):
            report = load_json(p[report_key])
            if not isinstance(report, dict) or report.get("passed") is not True or report.get("mode") != "process":
                return False
            if not isinstance(report.get("errors"), list) or not isinstance(report.get("warnings"), list):
                return False
        diff = load_json(p["diff"])
        if not isinstance(diff, dict):
            return False
        after_ok, _, _ = validate_structure_file(str(p["after"]), mode="process")
        return after_ok
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def next_incomplete(project_dir: Path) -> Optional[Path]:
    for ch in chapter_files(project_dir):
        if not is_chapter_complete(project_dir, ch):
            return ch
    return None


def latest_snapshot_seq(project_dir: Path) -> Optional[int]:
    version_dir = project_dir / "故事结构版本"
    if not version_dir.is_dir():
        return None
    seqs = []
    for path in version_dir.glob("story_after_ch*.json"):
        m = re.match(r"story_after_ch(\d+)\.json$", path.name)
        if m:
            seqs.append(int(m.group(1)))
    return max(seqs) if seqs else None


def max_available_delta_seq(project_dir: Path) -> int:
    seqs = []
    for path in (project_dir / "章节处理").glob("第*章_*.json"):
        m = re.match(r"第(\d+)章", path.name)
        if m:
            seqs.append(int(m.group(1)))
    return max(seqs) if seqs else 0


def replay_one_delta(project_dir: Path, seq: int) -> int:
    story = project_dir / "故事结构_增量.json"
    chapter = chapter_by_seq(project_dir, seq)
    if not chapter:
        print(f"重放失败: 找不到第{seq:03d}章原文拆解文件")
        return 1
    p = artifact_paths(project_dir, chapter)
    if not p["delta"].is_file():
        print(f"重放失败: 缺少第{seq:03d}章Delta: {p['delta']}")
        return 1

    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "process", "--report-json", str(p["delta_report"]), str(story), str(p["delta"])])
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章Delta校验失败，见: {p['delta_report']}")
        return 1

    shutil.copy2(story, p["before"])
    rollback_tmp = project_dir / "故事结构版本" / f"_replay_rollback_ch{seq:03d}.json"
    shutil.copy2(story, rollback_tmp)

    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(p["delta"])], project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt")
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并失败，已回滚")
        return 1

    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--report", str(project_dir / "质量治理" / "规范化" / f"normalize_ch{seq:03d}.txt")])
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--chapter-check", "--mode", "process", "--report-json", str(p["chapter_report"]), str(story), str(p["delta"])])
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并后结构校验失败，已回滚，见: {p['chapter_report']}")
        return 1

    shutil.copy2(story, p["after"])
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(p["before"]), str(p["after"]), str(p["diff"])])
    print(f"第{seq:03d}章Delta重放完成。")
    return 0


def recover_story_from_snapshot(project_dir: Path, snapshot_seq: Optional[int] = None, to_seq: Optional[int] = None) -> int:
    """从 story_after_chNNN 快照恢复过程库，并重放后续章节 Delta。"""
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    if snapshot_seq is None:
        snapshot_seq = latest_snapshot_seq(project_dir)
    if snapshot_seq is None:
        print("恢复失败: 没有可用的 story_after_chNNN.json 快照")
        return 1
    snapshot = project_dir / "故事结构版本" / f"story_after_ch{snapshot_seq:03d}.json"
    if not snapshot.is_file():
        print(f"恢复失败: 快照不存在: {snapshot}")
        return 1

    if to_seq is None:
        to_seq = max_available_delta_seq(project_dir)
    if to_seq < snapshot_seq:
        print(f"恢复失败: 目标章节 {to_seq:03d} 早于快照章节 {snapshot_seq:03d}")
        return 1

    story = project_dir / "故事结构_增量.json"
    shutil.copy2(snapshot, story)
    print(f"已从快照恢复过程库: story_after_ch{snapshot_seq:03d}.json -> 故事结构_增量.json")

    for seq in range(snapshot_seq + 1, to_seq + 1):
        rc = replay_one_delta(project_dir, seq)
        if rc != 0:
            return rc

    progress_path = project_dir / "进度.json"
    progress = load_json(progress_path, None)
    if isinstance(progress, dict):
        step2 = progress.setdefault("步骤状态", {}).setdefault("2", {"状态": "in_progress", "详情": {}, "完成时间": None})
        details = step2.setdefault("详情", {})
        done = set(details.setdefault("已完成章节", []))
        for ch in chapter_files(project_dir):
            seq = seq_from_file(ch)
            if seq <= to_seq and is_chapter_complete(project_dir, ch):
                done.add(ch.name)
        details["已完成章节"] = sorted(done)
        details["总章节数"] = len(chapter_files(project_dir))
        progress["当前步骤"] = "2"
        progress["更新时间"] = datetime.now().isoformat()
        save_json(progress_path, progress)

    print(f"恢复完成: 基线第{snapshot_seq:03d}章，重放至第{to_seq:03d}章。")
    return 0


def build_lightweight_index(story_path: Path, max_names: int = 80) -> str:
    data = load_json(story_path, {}) or {}
    lines = []
    intro = data.get("介绍", {}) if isinstance(data, dict) else {}
    if isinstance(intro, dict):
        if intro.get("标题"):
            lines.append(f"标题：{intro.get('标题')}")
    for key in COLLECTION_KEYS:
        items = data.get(key, []) if isinstance(data, dict) else []
        names = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if not name:
                continue
            aliases = item.get("别名", []) or []
            if aliases:
                names.append(f"{name}[{'/'.join(str(a) for a in aliases[:3])}]")
            else:
                names.append(name)
        lines.append(f"{key}({len(names)}): " + "、".join(names[:max_names]))
        if len(names) > max_names:
            lines[-1] += f" ……另有{len(names)-max_names}个"
    return "\n".join(lines)


def story_summary(story_path: Path, max_names: int = 80) -> str:
    """Backward-compatible name for the story index used by older callers.

    ``build_lightweight_index`` is the current descriptive API name, but the
    test and any external automation that still imports ``story_summary``
    should receive the identical serialized index.
    """
    return build_lightweight_index(story_path, max_names=max_names)


def parse_chapter_range(spec: str) -> Tuple[int, int]:
    text = (spec or "").strip()
    m = re.match(r"^(\d+)\s*(?:-|—|–|~|～|至|到)\s*(\d+)$", text)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
    elif re.match(r"^\d+$", text):
        start = end = int(text)
    else:
        raise ValueError(f"章节范围格式错误: {spec}，示例: 1-5")
    if start <= 0 or end <= 0 or start > end:
        raise ValueError(f"章节范围无效: {spec}")
    return start, end


def audit_paths(project_dir: Path, start: int, end: int) -> Dict[str, Path]:
    base = f"{start:03d}-{end:03d}"
    audit_dir = project_dir / "质量治理" / "周期审计"
    return {
        "dir": audit_dir,
        "pack": audit_dir / f"audit_{base}.md",
        "correction": audit_dir / f"correction_{base}.json",
        "status": audit_dir / f"audit_{base}.status.json",
        "feedback": audit_dir / f"audit_{base}.feedback.md",
    }


def read_excerpt(path: Path, limit: int = 12000) -> str:
    if not path.is_file():
        return f"[缺失] {path}"
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) > limit:
        return text[:limit] + f"\n\n...[已截断，原文件 {len(text)} 字符]"
    return text


def get_previous_correction(project_dir: Path, current_start: int) -> str:
    if current_start <= 1:
        return ""
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return ""
    for p in sorted(audit_dir.glob("correction_*.json"), reverse=True):
        m = re.match(r"correction_(\d+)-(\d+)\.json", p.name)
        if m and int(m.group(2)) < current_start:
            return f"\n\n## 上一个审计周期治理结果 ({p.name})\n\n```json\n{read_excerpt(p, 5000)}\n```\n"
    return ""


def extract_relevant_elements_for_audit(story_data: dict, project_dir: Path, start: int, end: int) -> str:
    names_mentioned = set()
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if not chapter:
            continue
        p = artifact_paths(project_dir, chapter)
        if p["delta"].is_file():
            delta_data = load_json(p["delta"], {})
            for key in ("新增元素", "修改元素"):
                bucket = delta_data.get(key, {})
                if not isinstance(bucket, dict):
                    continue
                for c_key in COLLECTION_KEYS:
                    for item in bucket.get(c_key, []):
                        if isinstance(item, dict) and item.get("名称"):
                            names_mentioned.add(item["名称"])
                            
    if not names_mentioned:
        return "无"
        
    relevant = []
    for key in COLLECTION_KEYS:
        for item in story_data.get(key, []):
            if not isinstance(item, dict):
                continue
            name = item.get("名称")
            if name in names_mentioned:
                relevant.append(json.dumps(item, ensure_ascii=False, indent=2))
                continue
            aliases = item.get("别名", [])
            if isinstance(aliases, list) and any(a in names_mentioned for a in aliases):
                relevant.append(json.dumps(item, ensure_ascii=False, indent=2))

    if not relevant:
        return "无"
    res = "\n\n".join(relevant)
    if len(res) > 80000:
        res = res[:80000] + "\n...[内容过长截断]"
    return res


def write_audit_pack(project_dir: Path, start: int, end: int, force: bool = False) -> Path:
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    paths = audit_paths(project_dir, start, end)
    if paths["pack"].exists() and not force:
        if not paths["status"].is_file():
            save_json(paths["status"], {
                "status": "queued",
                "range": f"{start:03d}-{end:03d}",
                "audit_pack": str(paths["pack"]),
                "correction_path": str(paths["correction"]),
                "created_at": datetime.now().isoformat(),
            })
        print(f"周期审计任务包已存在: {paths['pack']}")
        return paths["pack"]

    sections = [
        f"# 周期结构审计任务包 {start:03d}-{end:03d}",
        "",
        "## 产出要求",
        "",
        f"请基于本任务包审计第{start:03d}-{end:03d}章的结构累积质量，输出治理补丁：",
        "",
        f"- 补丁路径：`{paths['correction']}`",
        "- 补丁必须使用 Delta 格式，只写 `新增元素`、`修改元素` 和可选 `治理操作`。",
        "- 不要直接重写完整故事结构 JSON。",
        "",
        "自动治理运行时，配置的审计器会读取本任务包并将补丁写入上述路径。",
        "需要手工覆盖自动补丁时，再运行：",
        "",
        "```bash",
        f"python {script_path('run_pipeline.py')} commit-governance {project_dir} {paths['correction']}",
        "```",
        "",
        "## 审计重点",
        "",
        "- 合并跨章节重复角色、地点、线索、阵营、物品。",
        "- 将路人、临时称谓、误识别对象降级或合并到详情。",
        "- 整理伏笔、关系和事件引用，避免名称漂移。",
        "- 标签治理时，仅保留有跨元素检索、筛选、聚合或导航价值的标签；纯描述、一次性情境或与介绍重复的标签可放入 `治理操作.治理标签`。无法明确判断时保留。",
        "- `治理标签` 每项必须给出 类型、名称、保留标签、降级标签、理由；保留与降级必须完整覆盖该元素当前标签集。降级项会自动写入可逆的 `详情.补充标签`。",
        "- 事件数组顺序代表章节逻辑顺序：治理新增的历史聚合事件必须按最小涉及章节插入；修改既有事件不得改变其时间、涉及章节或数组位置。",
        "- 事件分组推荐使用 段号-剧情段名+剧情线主题+叙事功能+爽点情绪点+冲突悬念类型，例如0010-退婚事件+情感尊严线+冲突爆发+羞辱反击+身份与尊严；段号后必须写剧情结构信息，严禁0010-0001这类编号套编号。治理后按事件数组顺序全量重排段号，并拆分非连续重复剧情段。",
        "- 只修正结构质量问题，不凭空补充原文没有的信息。",
        "",
    ]
    
    story_size = story.stat().st_size if story.is_file() else 0
    story_data = load_json(story, {})
    
    if story_size < 100 * 1024:
        sections.extend([
            "## 完整故事结构",
            "",
            "```json",
            json.dumps(story_data, ensure_ascii=False, indent=2),
            "```",
            "",
        ])
    elif story_size <= 200 * 1024:
        sections.extend([
            "## 当前故事结构索引",
            "",
            "```text",
            build_lightweight_index(story),
            "```",
            get_previous_correction(project_dir, start),
        ])
    else:
        relevant_details = extract_relevant_elements_for_audit(story_data, project_dir, start, end)
        sections.extend([
            "## 当前故事结构索引",
            "",
            "```text",
            build_lightweight_index(story),
            "```",
            "",
            "## 本周期相关老元素详情",
            "",
            "```json",
            relevant_details,
            "```",
            get_previous_correction(project_dir, start),
        ])

    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        sections.append(f"## 第{seq:03d}章材料")
        sections.append("")
        if not chapter:
            sections.append(f"[缺失] 原文拆解文件：第{seq:03d}章")
            sections.append("")
            continue
        p = artifact_paths(project_dir, chapter)
        sections.extend([
            f"- 原文：`{p['chapter']}`",
            f"- 章节分析：`{p['analysis']}`",
            f"- Delta：`{p['delta']}`",
            "",
            "### 章节分析",
            "",
            "```markdown",
            read_excerpt(p["analysis"]),
            "```",
            "",
            "### Delta",
            "",
            "```json",
            read_excerpt(p["delta"]),
            "```",
            "",
        ])

    paths["pack"].parent.mkdir(parents=True, exist_ok=True)
    paths["pack"].write_text("\n".join(sections), encoding="utf-8")
    save_json(paths["status"], {
        "status": "queued",
        "range": f"{start:03d}-{end:03d}",
        "audit_pack": str(paths["pack"]),
        "correction_path": str(paths["correction"]),
        "created_at": datetime.now().isoformat(),
    })
    print(f"已生成周期审计任务包: {paths['pack']}")
    print(f"请产出治理补丁: {paths['correction']}")
    return paths["pack"]


def unresolved_audits(project_dir: Path) -> List[Dict[str, Any]]:
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return []
    audits = []
    for path in sorted(audit_dir.glob("audit_*.status.json")):
        data = load_json(path, None)
        if isinstance(data, dict) and data.get("status") != "committed":
            data["_status_path"] = str(path)
            m = re.match(r"^(\d{3})-(\d{3})$", str(data.get("range", "")))
            if m:
                data["_start"] = int(m.group(1))
                data["_end"] = int(m.group(2))
                audits.append(data)
    return audits


def update_audit_status(paths: Dict[str, Path], **updates: Any) -> None:
    data = load_json(paths["status"], {}) if paths["status"].is_file() else {}
    if not isinstance(data, dict):
        data = {}
    data.update(updates)
    data["updated_at"] = datetime.now().isoformat()
    save_json(paths["status"], data)


def write_audit_feedback(paths: Dict[str, Path], attempt: int, reason: str, worker_report: Path) -> None:
    parts = [
        f"# 周期审计自动修复反馈（第 {attempt} 次）",
        "",
        "## 本轮失败原因",
        "",
        reason,
        "",
        "## 审计器输出",
        "",
        "```text",
        read_excerpt(worker_report, 30000),
        "```",
        "",
        "请基于上述失败信息重写 correction Delta；不得重写完整故事结构 JSON。",
    ]
    paths["feedback"].write_text("\n".join(parts), encoding="utf-8")


def run_audit_worker(
    command_template: str,
    project_dir: Path,
    paths: Dict[str, Path],
    start: int,
    end: int,
    attempt: int,
    timeout_seconds: float,
) -> Tuple[int, Path]:
    report = paths["dir"] / f"audit_{start:03d}-{end:03d}.worker_{attempt:02d}.txt"
    try:
        command = command_template.format(
            project_dir=str(project_dir),
            audit_pack=str(paths["pack"]),
            correction_path=str(paths["correction"]),
            feedback_path=str(paths["feedback"]),
            chapter_start=start,
            chapter_end=end,
            attempt=attempt,
        )
    except KeyError as exc:
        report.write_text(f"审计器命令模板字段无效: {exc}\n", encoding="utf-8")
        return 1, report
    try:
        proc = subprocess.run(
            command,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        report.write_text(timeout_output(exc, timeout_seconds), encoding="utf-8")
        return 124, report
    report.write_text(proc.stdout, encoding="utf-8")
    if proc.returncode != 0:
        return proc.returncode, report
    if not paths["correction"].is_file() or paths["correction"].stat().st_size == 0:
        report.write_text(proc.stdout + "\n审计器未生成 correction Delta。\n", encoding="utf-8")
        return 1, report
    return 0, report


def run_periodic_governance(
    project_dir: Path,
    start: int,
    end: int,
    audit_command: str,
    governance_retries: int,
    retry_delay: float,
    audit_timeout_seconds: float,
) -> int:
    paths = audit_paths(project_dir, start, end)
    write_audit_pack(project_dir, start, end)
    if not audit_command:
        reason = "未配置自动审计器。请通过 --audit-command 或 NOVEL_AUDIT_COMMAND 提供生成 correction Delta 的命令。"
        update_audit_status(paths, status="failed", failure_reason=reason)
        print(reason)
        return 1

    # retries excludes the initial audit; 3 retries therefore permits 4 attempts.
    attempt = 0
    while attempt <= governance_retries:
        attempt += 1
        update_audit_status(paths, status="running", attempt=attempt, correction_path=str(paths["correction"]))
        worker_rc, worker_report = run_audit_worker(
            audit_command, project_dir, paths, start, end, attempt, audit_timeout_seconds
        )
        if worker_rc == 0:
            governance_rc = commit_governance(project_dir, str(paths["correction"]))
            if governance_rc == 0:
                update_audit_status(paths, status="committed", auto_attempts=attempt, worker_report=str(worker_report))
                print(f"周期审计 {start:03d}-{end:03d} 已自动治理并通过校验。")
                return 0
            reason = "治理补丁未通过提交链路；审计器须根据当前结构和校验结果生成新的 correction Delta。"
        else:
            reason = (
                f"自动审计器执行超时（{audit_timeout_seconds:g} 秒）。"
                if worker_rc == 124 else f"自动审计器退出码为 {worker_rc}。"
            )

        write_audit_feedback(paths, attempt, reason, worker_report)
        update_audit_status(
            paths,
            status="retrying",
            attempt=attempt,
            failure_reason=reason,
            worker_report=str(worker_report),
            feedback_path=str(paths["feedback"]),
        )
        if attempt <= governance_retries:
            if retry_delay > 0:
                time.sleep(retry_delay)

    update_audit_status(paths, status="failed", attempt=attempt, failure_reason=reason)
    print(f"周期审计 {start:03d}-{end:03d} 自动治理未成功；未进入后续章节。")
    return 1


def mark_periodic_audit_committed(project_dir: Path, patch: Path, before: Path, after: Path, diff: Path) -> None:
    resolved_patch = patch.resolve()
    audit_dir = (project_dir / "质量治理" / "周期审计").resolve()
    if resolved_patch.parent != audit_dir:
        return
    m = re.match(r"^correction_(\d{3})-(\d{3})\.json$", resolved_patch.name)
    if not m:
        return
    status_path = audit_dir / f"audit_{m.group(1)}-{m.group(2)}.status.json"
    data = load_json(status_path, {}) if status_path.is_file() else {}
    if not isinstance(data, dict):
        data = {}
    data.update({
        "status": "committed",
        "range": f"{m.group(1)}-{m.group(2)}",
        "correction_path": str(resolved_patch),
        "committed_at": datetime.now().isoformat(),
        "before": str(before),
        "after": str(after),
        "diff": str(diff),
    })
    save_json(status_path, data)


def prepare_chapter(project_dir: Path, seq: Optional[int] = None, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    story = ensure_story(project_dir)
    chapter = chapter_by_seq(project_dir, seq) if seq else next_incomplete(project_dir)
    if not chapter:
        print("没有待处理章节。")
        return 0
    p = artifact_paths(project_dir, chapter)
    if p["task"].exists() and not force:
        print(f"任务包已存在: {p['task']}")
        print(f"请填写：\n  章节分析MD: {p['analysis']}\n  Delta JSON: {p['delta']}")
        return 0
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    summary = build_lightweight_index(story)
    chapter_seq = seq_from_file(chapter)
    chapter_id = str(chapter_seq).zfill(4)
    content = f"""# 第{chapter_seq:03d}章拆书任务包

## 你需要产出两个文件

1. 章节分析MD：`{p['analysis']}`
2. 本章Delta JSON：`{p['delta']}`

请先写章节分析MD，再根据“当前故事结构索引 + 本章原文 + 章节分析MD”写Delta JSON。不要输出完整故事结构JSON。

## 当前故事结构索引

```text
{summary}
```

## 章节分析MD模板

章节分析器先输出 MD，不输出 JSON。建议使用以下 10 个部分：

```markdown
# 第{chapter_seq:03d}章 {{标题}}

## 1. 剧情梗概
约500字，概括本章主要事件、人物行为、情节转折。

## 2. 出场人物
- 人物A：本章行为、情绪状态、关系变化、是否首次出现。

## 3. 核心冲突
冲突双方、冲突目标、冲突推进方式。

## 4. 信息增量
新增世界观、人物背景、地点、阵营、规则、线索或设定。

## 5. 伏笔与悬念
伏笔、悬念、后续可能回收点、证据原文。

## 6. 爽点 / 虐点 / 情绪点
压抑、期待、羞辱、反击、打脸、逆袭、热血、悬念、感动、虐心、危机感、成就感等。

## 7. 章节功能判断
开篇铺垫/冲突升级/人物塑造/世界观展开/高潮推进/转折/收束/过渡等。

## 8. 事件分组与标签建议
事件分组推荐：`段号-剧情段名+剧情线主题+叙事功能+爽点情绪点+冲突悬念类型`。
示例：`0010-退婚事件+情感尊严线+冲突爆发+羞辱反击+身份与尊严`。
可复用分类写入标签：`剧情线主题:*`、`爽点情绪点:*`、`冲突悬念类型:*`、`画面类型:*`、`视觉用途:*`。

## 9. 画面 / 分镜 / 视觉资产候选
| 候选编号 | 对应事件 | 画面价值 | 画面类型 | 核心画面 | 出场人物 | 地点 | 关键物品 | 情绪氛围 | 镜头建议 | 是否进入视觉资产 |
|----------|----------|----------|----------|----------|----------|------|----------|----------|----------|------------------|

## 10. 结构提取提示
列出最应该进入故事结构JSON的角色、事件、地点、线索、阵营、物品、其他事项。
```

## Delta JSON硬格式

```json
{{
  "章节": "第{chapter_seq:03d}章",
  "新增元素": {{"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}},
  "修改元素": {{"角色集": [], "事件集": [], "地点集": [], "线索集": [], "阵营集": [], "物品集": [], "其他事项集": []}}
}}
```

要求：
- 这是逐切片任务；本Delta只能对应第{chapter_seq:03d}个拆分单元，禁止合并多个章节/编号/小节。
- 新增元素必须补齐所有标准字段。
- 非标准信息必须放入详情，不要放在元素顶层。
- 详情键名不能有标点、空格、下划线；详情值只能是字符串或“类型:名称”的字符串数组。
- 每个新增或修改元素的详情必须填写非空 `提取理由`。它用于Delta、章节分析和治理补丁追溯，不会合并进最终元素JSON。
- 角色必须有 `生日`：未知固定写 `0001-01-01T00:00:00`。角色、地点、线索、阵营、物品必须在 `详情` 内提供四位字符串 `首次章节`、`最近章节`，例如 `"{chapter_id}"`。
- 事件必须在 `详情` 内提供 `涉及章节`（固定宽度、升序、中文逗号，例如 `"{chapter_id}"`）并在顶层提供 `时间`。本章新事件时间固定为 `{chapter_time(chapter_seq)}`；时间是章节时间，不是原著日历。
- 事件分组使用连续剧情段。延续既有剧情段时复用其完整 `分组`；新剧情段推荐写 `剧情段名+剧情线主题+叙事功能+爽点情绪点+冲突悬念类型`，不可只写章节号或纯编号，合并后自动生成 `0010-退婚事件+情感尊严线+冲突爆发+羞辱反击+身份与尊严` 形式的段号。
- `详情.剧情段` 可自由使用 `-` 补充人物、势力、地点、冲突、目标、功能、爽点情绪点和冲突悬念类型；有正式线索关联时写 `详情.关联线索`，例如 `["线索:退婚约定"]`。
- 视觉资产入口写入 `标签集`，如 `画面类型:冲突对峙`、`视觉用途:封面候选`；具体视觉生产资料写入字符串详情字段：`视觉等级`、`画面类型`、`核心画面`、`镜头建议`、`氛围`、`视觉理由`、`视觉用途`。
- 过程阶段允许未来事件/人物暂未出现，但最终前必须补齐或移入详情.待确认信息。
- 不能为了满足数量要求编造原文没有的元素。

## 本章原文

```markdown
{chapter_text}
```
"""
    p["task"].parent.mkdir(parents=True, exist_ok=True)
    p["task"].write_text(content, encoding="utf-8")
    print(f"已生成章节任务包: {p['task']}")
    print(f"请填写：\n  {p['analysis']}\n  {p['delta']}")
    return 0


def write_repair_pack(project_dir: Path, chapter: Path, reason: str, report_paths: List[Path]) -> None:
    p = artifact_paths(project_dir, chapter)
    parts = [f"# 第{seq_from_file(chapter):03d}章修复任务", "", f"失败原因：{reason}", ""]
    for rp in report_paths:
        if rp.is_file():
            parts.append(f"## 报告：{rp.name}")
            parts.append("```text")
            parts.append(rp.read_text(encoding="utf-8", errors="ignore")[:30000])
            parts.append("```")
    parts.append("请只修复本章 Delta JSON，不要重写完整故事结构JSON。")
    p["repair"].parent.mkdir(parents=True, exist_ok=True)
    p["repair"].write_text("\n".join(parts), encoding="utf-8")
    print(f"已生成修复任务包: {p['repair']}")


def commit_chapter(project_dir: Path, seq: int, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    story = ensure_story(project_dir)
    chapter = chapter_by_seq(project_dir, seq)
    if not chapter:
        print(f"找不到第{seq:03d}章")
        return 1
    p = artifact_paths(project_dir, chapter)
    if is_chapter_complete(project_dir, chapter) and not force:
        print(f"第{seq:03d}章已完成，跳过。使用 --force 可重新提交。")
        return 0
    if not p["analysis"].is_file():
        print(f"缺章节分析MD: {p['analysis']}")
        prepare_chapter(project_dir, seq)
        return 2
    if not p["delta"].is_file():
        print(f"缺Delta JSON: {p['delta']}")
        prepare_chapter(project_dir, seq)
        return 2

    # 入库前校验
    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "process", "--report-json", str(p["delta_report"]), str(story), str(p["delta"])])
    if rc != 0:
        print(f"Delta校验失败，见: {p['delta_report']}")
        write_repair_pack(project_dir, chapter, "Delta校验失败", [p["delta_report"]])
        return 1

    shutil.copy2(story, p["before"])
    backup_tmp = project_dir / "故事结构版本" / f"_rollback_ch{seq:03d}.json"
    shutil.copy2(story, backup_tmp)

    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(p["delta"])], project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt")
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print("合并失败，已回滚。")
        write_repair_pack(project_dir, chapter, "merge_delta.py合并失败", [project_dir / "结构变更日志" / f"merge_ch{seq:03d}.txt"])
        return 1

    # 合并后保全式规范化，不隔离前向引用，避免过程信息丢失。
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--report", str(project_dir / "质量治理" / "规范化" / f"normalize_ch{seq:03d}.txt")])

    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--chapter-check", "--mode", "process", "--report-json", str(p["chapter_report"]), str(story), str(p["delta"])])
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print(f"章节合并后校验失败，已回滚，见: {p['chapter_report']}")
        write_repair_pack(project_dir, chapter, "合并后结构校验失败", [p["delta_report"], p["chapter_report"]])
        return 1

    shutil.copy2(story, p["after"])
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(p["before"]), str(p["after"]), str(p["diff"])])

    # 更新进度
    detail = json.dumps({"已完成章节": [chapter.name]}, ensure_ascii=False)
    # 这里不用直接覆盖列表，调用 progress_manager update 不适合追加；手动合并更稳。
    progress_path = project_dir / "进度.json"
    progress = load_json(progress_path, None)
    if isinstance(progress, dict):
        step2 = progress.setdefault("步骤状态", {}).setdefault("2", {"状态": "in_progress", "详情": {}, "完成时间": None})
        details = step2.setdefault("详情", {})
        done = details.setdefault("已完成章节", [])
        if chapter.name not in done:
            done.append(chapter.name)
        done.sort()
        details["总章节数"] = len(chapter_files(project_dir))
        step2["状态"] = "in_progress"
        progress["当前步骤"] = "2"
        progress["更新时间"] = datetime.now().isoformat()
        save_json(progress_path, progress)

    print(f"第{seq:03d}章提交完成。")
    return 0


def cmd_resume(project_dir: Path) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    files = chapter_files(project_dir)
    if not files:
        print("原文拆解目录中没有章节。先执行 split。")
        return 1
    done = [p for p in files if is_chapter_complete(project_dir, p)]
    nxt = next_incomplete(project_dir)
    print(f"章节总数: {len(files)}")
    print(f"可信完成: {len(done)}")
    if nxt:
        p = artifact_paths(project_dir, nxt)
        print(f"下一章: 第{seq_from_file(nxt):03d}章 {nxt.name}")
        for key in ["analysis", "delta", "delta_report", "chapter_report", "before", "after", "diff"]:
            status = "[x]" if p[key].is_file() and p[key].stat().st_size > 0 else "[ ]"
            print(f"  {status} {key}: {p[key]}")
        print(f"任务包: {p['task']}")
    else:
        print("所有章节均已可信完成。")
    return 0


def cmd_run(
    project_dir: Path,
    max_chapters: int = 0,
    audit_interval: int = 5,
    audit_command: str = "",
    governance_retries: int = 3,
    governance_retry_delay: float = 0,
    audit_timeout_seconds: float = DEFAULT_AUDIT_TIMEOUT_SECONDS,
) -> int:
    if governance_retries < 0:
        print("governance_retries 必须为非负整数。")
        return 2
    if not 600 <= audit_timeout_seconds <= 1800:
        print("audit_timeout_seconds 必须在 600 到 1800 秒之间。")
        return 2
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    audit_command = audit_command or os.environ.get("NOVEL_AUDIT_COMMAND", "")
    for audit in unresolved_audits(project_dir):
        rc = run_periodic_governance(
            project_dir,
            audit["_start"],
            audit["_end"],
            audit_command,
            governance_retries,
            governance_retry_delay,
            audit_timeout_seconds,
        )
        if rc != 0:
            return rc
    processed = 0
    while True:
        nxt = next_incomplete(project_dir)
        if not nxt:
            print("所有章节均已完成。")
            return 0
        seq = seq_from_file(nxt)
        p = artifact_paths(project_dir, nxt)
        if not p["analysis"].is_file() or not p["delta"].is_file():
            prepare_chapter(project_dir, seq)
            print("已暂停：等待模型/人工填写章节分析MD和Delta JSON。")
            return 2
        rc = commit_chapter(project_dir, seq)
        if rc != 0:
            return rc
        processed += 1
        if audit_interval and seq % audit_interval == 0:
            start = seq - audit_interval + 1
            rc = run_periodic_governance(
                project_dir,
                start,
                seq,
                audit_command,
                governance_retries,
                governance_retry_delay,
                audit_timeout_seconds,
            )
            if rc != 0:
                return rc
        if max_chapters and processed >= max_chapters:
            print(f"已达到本次最大提交章节数: {max_chapters}")
            return 0



def commit_governance(project_dir: Path, patch_path: str) -> int:
    """提交按需/周期治理补丁：合并新增修改 + 执行治理操作 + 规范化 + governance校验 + diff。"""
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    patch = Path(patch_path)
    if not patch.is_file():
        print(f"治理补丁不存在: {patch}")
        return 1
    audit_dir = (project_dir / "质量治理" / "周期审计").resolve()
    is_periodic_patch = (
        patch.resolve().parent == audit_dir
        and re.match(r"^correction_\d{3}-\d{3}\.json$", patch.name) is not None
    )
    governance_dir = audit_dir if is_periodic_patch else project_dir / "质量治理" / "按需治理"
    governance_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    before = project_dir / "故事结构版本" / f"story_before_governance_{ts}.json"
    after = project_dir / "故事结构版本" / f"story_after_governance_{ts}.json"
    diff = project_dir / "结构变更日志" / f"diff_governance_{ts}.json"
    report_delta = governance_dir / f"validate_delta_governance_{ts}.txt"
    report_schema = governance_dir / f"validate_schema_governance_{ts}.txt"
    shutil.copy2(story, before)
    rc = run_cmd([sys.executable, str(script_path("validate_delta.py")), "--mode", "governance", str(story), str(patch)], report_delta)
    if rc != 0:
        print(f"治理补丁Delta校验失败: {report_delta}")
        return 1
    rc = run_cmd([sys.executable, str(script_path("merge_delta.py")), str(story), str(patch)], governance_dir / f"merge_governance_{ts}.txt")
    if rc != 0:
        shutil.copy2(before, story); print("治理新增/修改合并失败，已回滚。") ; return 1
    rc = run_cmd([sys.executable, str(script_path("apply_governance_ops.py")), str(story), str(patch), "--backup"], governance_dir / f"apply_ops_{ts}.txt")
    if rc not in (0,2):
        shutil.copy2(before, story); print("治理操作失败，已回滚。") ; return 1
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--report", str(project_dir / "质量治理" / "规范化" / f"normalize_governance_{ts}.txt")])
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")), "--mode", "governance", str(story)], report_schema)
    if rc != 0:
        shutil.copy2(before, story); print(f"治理后结构校验失败，已回滚: {report_schema}") ; return 1
    shutil.copy2(story, after)
    run_cmd([sys.executable, str(script_path("diff_structure.py")), str(before), str(after), str(diff)])
    mark_periodic_audit_committed(project_dir, patch, before, after, diff)
    print(f"治理补丁提交完成。before={before} after={after} diff={diff}")
    return 0


def cmd_final_pack(project_dir: Path, force: bool = False) -> int:
    """生成最终结构整理任务包：复制增量为草稿，运行 final 校验，打包所有参考材料。"""
    init_dirs(project_dir)
    story = ensure_story(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if draft.is_file() and not force:
        print(f"故事结构_草稿.json 已存在，跳过复制。使用 --force 重新从增量复制。")
    else:
        shutil.copy2(story, draft)
        print(f"已复制 故事结构_增量.json → 故事结构_草稿.json")

    # 规范化
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")),
             str(draft), "--in-place", "--backup",
             "--report", str(audit_dir / "normalize_final_init.txt")])

    # 运行 final 校验
    report = audit_dir / "validate_report.txt"
    run_cmd([sys.executable, str(script_path("validate_structure.py")),
             "--mode", "final", str(draft)], report)

    # 收集材料生成任务包
    task_pack = audit_dir / "final_task_pack.md"
    sections: List[str] = [
        "# 最终结构整理任务包",
        "",
        "## 目标",
        "",
        "将过程型 `故事结构_草稿.json` 整理为最终交付型 `故事结构.json`。",
        "修正所有校验错误和警告，完成全局去重、别名合并、路人降级、事件粒度统一等整理工作。",
        "",
        "## 产出要求",
        "",
        "- 直接修改 `故事结构_草稿.json`",
        "- 修改完成后运行：",
        "",
        "```bash",
        f"python {script_path('run_pipeline.py')} commit-final-draft {project_dir}",
        "```",
        "",
        "- 如果校验失败，根据修复任务包继续修正，再次提交",
        "- 最终通过后运行：",
        "",
        "```bash",
        f"python {script_path('run_pipeline.py')} finalize {project_dir}",
        "```",
        "",
        "## 当前校验报告",
        "",
    ]
    if report.is_file():
        sections.extend(["```text", read_excerpt(report, 30000), "```", ""])
    else:
        sections.extend(["（校验报告未生成）", ""])

    sections.extend([
        "## 当前故事结构索引",
        "",
        "```text",
        build_lightweight_index(draft),
        "```",
        "",
    ])

    # 全书分析报告
    analysis_refs = [
        ("全书分析/剧情结构/章节梗概汇总.md", "章节梗概汇总"),
        ("全书分析/人物分析/人物档案.md", "人物档案"),
        ("全书分析/人物分析/人物关系.md", "人物关系"),
        ("全书分析/剧情结构/伏笔追踪.md", "伏笔追踪"),
        ("全书分析/剧情结构/冲突图谱.md", "冲突图谱"),
        ("全书分析/视觉资产/视觉资产清单.md", "视觉资产清单"),
        ("全书分析/视觉资产/关键场景分镜表.md", "关键场景分镜表"),
    ]
    has_analysis = False
    for rel, title in analysis_refs:
        path = project_dir / rel
        if path.is_file() and path.stat().st_size > 0:
            if not has_analysis:
                sections.extend(["## 全书分析参考", ""])
                has_analysis = True
            sections.extend([f"### {title}", "", f"文件：`{path}`", "",
                             "```markdown", read_excerpt(path, 15000), "```", ""])

    # 周期审计报告
    pa_dir = project_dir / "质量治理" / "周期审计"
    if pa_dir.is_dir():
        audit_mds = sorted(pa_dir.glob("audit_*.md"))
        corr_jsons = sorted(pa_dir.glob("correction_*.json"))
        if audit_mds or corr_jsons:
            sections.extend(["## 周期审计参考", ""])
            for md in audit_mds:
                sections.extend([f"### {md.name}", "",
                                 "```markdown", read_excerpt(md, 8000), "```", ""])
            for cj in corr_jsons:
                sections.extend([f"### {cj.name}", "",
                                 "```json", read_excerpt(cj, 8000), "```", ""])

    sections.extend([
        "## 整理清单",
        "",
        "请重点处理以下方面：",
        "",
        "1. 交叉引用缺失或指向别名而非正式名称",
        "2. 重复元素合并",
        "3. 别名合并到主元素",
        "4. 路人角色降级或清理",
        "5. 事件粒度不统一",
        "6. 伏笔/线索泛化",
        "7. 详情字段不规范（键名不能有标点/空格/下划线，值只能是字符串或字符串数组）",
        "8. 非标准字段迁入详情",
        "9. 地点/阵营层级整理、分组统筹；事件分组优先采用 段号-剧情段名+剧情线主题+叙事功能+爽点情绪点+冲突悬念类型",
        "10. 数量仅作为丰富度参考，不作为拆书提取硬门槛。",
        "11. 如果原文不足，不得为达标新增元素。",
        "",
    ])

    task_pack.write_text("\n".join(sections), encoding="utf-8")
    print(f"已生成最终整理任务包: {task_pack}")
    print(f"校验报告: {report}")
    print(f"草稿文件: {draft}")
    print(f"\n请根据任务包修改 故事结构_草稿.json，完成后运行:")
    print(f"  python {script_path('run_pipeline.py')} commit-final-draft {project_dir}")
    return 0


def cmd_commit_final_draft(project_dir: Path) -> int:
    """提交模型修好的最终草稿：规范化 → strict 校验 → 失败生成修复包。"""
    init_dirs(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if not draft.is_file():
        print("故事结构_草稿.json 不存在。请先运行 final-pack 生成草稿。")
        return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")),
             str(draft), "--in-place",
             "--report", str(audit_dir / f"normalize_final_{ts}.txt")])

    report = audit_dir / "validate_report.txt"
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(draft)], report)

    if rc != 0:
        print(f"最终校验失败，见: {report}")
        repair = audit_dir / "repair_final.md"
        parts = [
            "# 最终结构修复任务",
            "",
            "## 校验失败",
            "",
            "`故事结构_草稿.json` 未通过 `--strict` 校验。请根据以下报告修正后再次提交。",
            "",
            "## 校验报告",
            "",
            "```text",
            read_excerpt(report, 30000),
            "```",
            "",
            "## 修正要求",
            "",
            "- 直接修改 `故事结构_草稿.json`",
            "- 只修正校验报告中指出的问题",
            "- 不要为了满足数量要求编造原文没有的元素",
            "- 修正后再次运行：",
            "",
            "```bash",
            f"python {script_path('run_pipeline.py')} commit-final-draft {project_dir}",
            "```",
        ]
        repair.write_text("\n".join(parts), encoding="utf-8")
        print(f"已生成修复任务包: {repair}")
        return 1

    print("最终校验通过！")
    print(f"校验报告: {report}")
    print(f"\n可以运行以下命令完成最终交付:")
    print(f"  python {script_path('run_pipeline.py')} finalize {project_dir}")
    return 0


def cmd_validate_final(project_dir: Path, file: str = "") -> int:
    """独立最终校验：定位目标文件 → strict 校验 → 输出报告。"""
    target = project_dir / (file or "故事结构_草稿.json")
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)
    report = audit_dir / "validate_report.txt"

    if not target.is_file():
        print(f"目标文件不存在: {target}")
        return 1

    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(target)], report)
    print(f"校验报告: {report}")
    if rc == 0:
        print("最终校验通过。")
    else:
        print("最终校验失败。")
    return rc


def cmd_finalize(project_dir: Path) -> int:
    """最终交付：前置 strict 校验 → 复制草稿为最终 JSON → 完成步骤4 → 审计日志。"""
    init_dirs(project_dir)
    draft = project_dir / "故事结构_草稿.json"
    final = project_dir / "故事结构.json"
    audit_dir = project_dir / "质量治理" / "最终审计"
    audit_dir.mkdir(parents=True, exist_ok=True)

    if not draft.is_file():
        print("故事结构_草稿.json 不存在。请先运行 final-pack 生成草稿。")
        return 1

    report = audit_dir / "validate_report.txt"
    rc = run_cmd([sys.executable, str(script_path("validate_structure.py")),
                  "--strict", str(draft)], report)
    if rc != 0:
        print(f"最终校验未通过，无法 finalize。见: {report}")
        print("请先修正草稿并运行 commit-final-draft。")
        return 1

    shutil.copy2(draft, final)
    print(f"已生成最终交付: {final}")

    run_cmd([sys.executable, str(script_path("progress_manager.py")),
             "complete", str(project_dir), "4"])

    finalize_log = {
        "finalize_time": datetime.now().isoformat(),
        "draft": str(draft),
        "final": str(final),
        "validate_report": str(report),
        "draft_size_bytes": draft.stat().st_size,
        "final_size_bytes": final.stat().st_size,
    }
    save_json(audit_dir / "finalize_log.json", finalize_log)
    print(f"交付日志: {audit_dir / 'finalize_log.json'}")
    print(f"\n步骤4已完成。最终故事结构: {final}")
    return 0


def cmd_audit_pack(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    try:
        start, end = parse_chapter_range(args.chapters)
    except ValueError as exc:
        print(exc)
        return 1
    write_audit_pack(project_dir, start, end, force=args.force)
    return 0

def cmd_split(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    cmd = [sys.executable, str(script_path("split_chapters.py")), args.source_file, str(project_dir / "原文拆解")]
    if args.pattern:
        cmd.extend(["--pattern", args.pattern])
    if args.preface_mode:
        cmd.extend(["--preface-mode", args.preface_mode])
    rc = run_cmd(cmd)
    if rc == 0:
        ensure_story(project_dir)
    return rc


def cmd_analysis_pack(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    cmd = [sys.executable, str(script_path("analysis_context_pack.py")), str(project_dir), "--task", args.task, "--chapters", args.chapters, "--include-original", args.include_original, "--max-pack-chars", str(args.max_pack_chars), "--max-original-chars", str(args.max_original_chars), "--max-analysis-chars", str(args.max_analysis_chars)]
    if args.targets:
        cmd.extend(["--targets", *args.targets])
    if args.question:
        cmd.extend(["--question", args.question])
    if args.out_dir:
        cmd.extend(["--out-dir", args.out_dir])
    return run_cmd(cmd)


def cmd_analysis_status(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    print("=== 全书/局部分析状态 ===")
    for task, outputs in ANALYSIS_OUTPUTS.items():
        print(f"\n[{task}]")
        for rel in outputs:
            path = project_dir / rel
            status = "[x]" if path.is_file() and path.stat().st_size > 0 else "[ ]"
            size = path.stat().st_size if path.is_file() else 0
            print(f"  {status} {rel} ({size} bytes)")
    return 0


def cmd_normalize(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    story = project_dir / (args.file or "故事结构_增量.json")
    cmd = [sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place", "--backup", "--report", str(project_dir / "质量治理" / "规范化" / "normalize_manual.txt")]
    if args.quarantine_invalid_refs:
        cmd.append("--quarantine-invalid-refs")
    return run_cmd(cmd)


def cmd_migrate_chronology(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir)
    cmd = [sys.executable, str(script_path("migrate_chronology.py")), str(project_dir)]
    if args.file:
        cmd.extend(["--file", args.file])
    if args.dry_run:
        cmd.append("--dry-run")
    return run_cmd(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="拆书主控器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dirs = sub.add_parser("init-dirs", help="补齐项目目录结构")
    p_dirs.add_argument("project_dir")

    p_split = sub.add_parser("split", help="拆分原文并初始化故事结构")
    p_split.add_argument("project_dir")
    p_split.add_argument("source_file")
    p_split.add_argument("--pattern", default="")
    p_split.add_argument("--preface-mode", choices=["separate", "attach", "chapter", "drop"], default="")

    p_prepare = sub.add_parser("prepare-chapter", help="生成单章模型任务包")
    p_prepare.add_argument("project_dir")
    p_prepare.add_argument("--chapter", type=int, default=0)
    p_prepare.add_argument("--force", action="store_true")

    p_commit = sub.add_parser("commit-chapter", help="提交已填写好的章节分析MD和Delta")
    p_commit.add_argument("project_dir")
    p_commit.add_argument("--chapter", type=int, required=True)
    p_commit.add_argument("--force", action="store_true")

    p_run = sub.add_parser("run", help="持续提交已具备产物的章节，并在周期点自动审计、治理和校验")
    p_run.add_argument("project_dir")
    p_run.add_argument("--max-chapters", type=int, default=0)
    p_run.add_argument("--audit-interval", type=int, default=5, help="每N章执行一次自动周期治理；0表示关闭")
    p_run.add_argument("--audit-command", default=os.environ.get("NOVEL_AUDIT_COMMAND", ""), help="自动审计器命令模板，可使用 {project_dir}、{audit_pack}、{correction_path}、{feedback_path}、{chapter_start}、{chapter_end}、{attempt}")
    p_run.add_argument("--governance-retries", type=non_negative_int, default=3, help="自动治理失败后的最大重试次数；默认3（最多共尝试4次），0表示不重试")
    p_run.add_argument("--governance-retry-delay", type=float, default=0, help="自动治理重试间隔秒数")
    p_run.add_argument("--audit-timeout-seconds", type=audit_timeout_seconds_arg, default=DEFAULT_AUDIT_TIMEOUT_SECONDS, help="单次外部自动审计器超时秒数，范围600-1800，默认900")

    p_resume = sub.add_parser("resume", help="查看下一步缺什么")
    p_resume.add_argument("project_dir")

    p_recover = sub.add_parser("recover-story", help="从 story_after_chNNN 快照恢复过程库，并重放后续Delta")
    p_recover.add_argument("project_dir")
    p_recover.add_argument("--snapshot", type=int, default=0, help="作为基线的快照章节号；默认使用最新 story_after_chNNN")
    p_recover.add_argument("--to", type=int, default=0, help="重放到的章节号；默认使用已有Delta的最大章节号")

    p_norm = sub.add_parser("normalize", help="保全式规范化故事结构JSON")
    p_norm.add_argument("project_dir")
    p_norm.add_argument("--file", default="")
    p_norm.add_argument("--quarantine-invalid-refs", action="store_true")

    p_migrate = sub.add_parser("migrate-chronology", help="按_索引.json回填章节时间、紧凑追溯字段并重排存量事件")
    p_migrate.add_argument("project_dir")
    p_migrate.add_argument("--file", default="")
    p_migrate.add_argument("--dry-run", action="store_true")

    p_gov = sub.add_parser("commit-governance", help="提交按需/周期治理补丁")
    p_gov.add_argument("project_dir")
    p_gov.add_argument("patch_json")

    p_audit = sub.add_parser("audit-pack", help="生成周期结构审计任务包")
    p_audit.add_argument("project_dir")
    p_audit.add_argument("--chapters", required=True, help="章节范围，例如 1-5 或 1-10")
    p_audit.add_argument("--force", action="store_true")

    p_pack = sub.add_parser("analysis-pack", help="生成全书/局部分析任务包")
    p_pack.add_argument("project_dir")
    p_pack.add_argument("--task", choices=["summary", "characters", "plot", "style", "visual_assets", "report", "custom", "worldview", "plotlines", "outline", "detailed_outline"], required=True)
    p_pack.add_argument("--chapters", default="all")
    p_pack.add_argument("--targets", nargs="*", default=[])
    p_pack.add_argument("--question", default="")
    p_pack.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_pack.add_argument("--max-pack-chars", type=int, default=70000)
    p_pack.add_argument("--max-original-chars", type=int, default=6000)
    p_pack.add_argument("--max-analysis-chars", type=int, default=12000)
    p_pack.add_argument("--out-dir", default="")

    p_status = sub.add_parser("analysis-status", help="查看全书/局部分析状态")
    p_status.add_argument("project_dir")

    p_fpack = sub.add_parser("final-pack", help="生成最终结构整理任务包（复制增量为草稿+校验+打包参考材料）")
    p_fpack.add_argument("project_dir")
    p_fpack.add_argument("--force", action="store_true", help="强制重新从增量复制草稿")

    p_cfd = sub.add_parser("commit-final-draft", help="提交模型修好的最终草稿（规范化+strict校验）")
    p_cfd.add_argument("project_dir")

    p_vf = sub.add_parser("validate-final", help="独立最终强校验")
    p_vf.add_argument("project_dir")
    p_vf.add_argument("--file", default="", help="校验目标文件名，默认 故事结构_草稿.json")

    p_fin = sub.add_parser("finalize", help="通过校验后复制草稿为最终 故事结构.json")
    p_fin.add_argument("project_dir")

    args = parser.parse_args()
    project_dir = Path(getattr(args, "project_dir", "."))

    if args.cmd == "init-dirs":
        init_dirs(project_dir); print(f"目录已补齐: {project_dir}"); return 0
    if args.cmd == "split":
        return cmd_split(args)
    if args.cmd == "prepare-chapter":
        return prepare_chapter(project_dir, args.chapter or None, args.force)
    if args.cmd == "commit-chapter":
        return commit_chapter(project_dir, args.chapter, args.force)
    if args.cmd == "run":
        return cmd_run(project_dir, args.max_chapters, args.audit_interval, args.audit_command, args.governance_retries, args.governance_retry_delay, args.audit_timeout_seconds)
    if args.cmd == "resume":
        return cmd_resume(project_dir)
    if args.cmd == "recover-story":
        return recover_story_from_snapshot(project_dir, args.snapshot or None, args.to or None)
    if args.cmd == "normalize":
        return cmd_normalize(args)
    if args.cmd == "migrate-chronology":
        return cmd_migrate_chronology(args)
    if args.cmd == "commit-governance":
        return commit_governance(project_dir, args.patch_json)
    if args.cmd == "audit-pack":
        return cmd_audit_pack(args)
    if args.cmd == "analysis-pack":
        return cmd_analysis_pack(args)
    if args.cmd == "analysis-status":
        return cmd_analysis_status(args)
    if args.cmd == "final-pack":
        return cmd_final_pack(project_dir, getattr(args, "force", False))
    if args.cmd == "commit-final-draft":
        return cmd_commit_final_draft(project_dir)
    if args.cmd == "validate-final":
        return cmd_validate_final(project_dir, getattr(args, "file", ""))
    if args.cmd == "finalize":
        return cmd_finalize(project_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
