#!/usr/bin/env python3
"""
拆书主控器（可断点、可恢复、不会假装自动调用大模型）。

它负责：
- 初始化/拆章/目录/进度；
- 分别生成章节分析任务与Delta提取任务，让模型先写MD、再写JSON；
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
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chronology import chapter_time
from story_schema_rules import COLLECTION_KEYS
from validate_structure import validate_structure_file

from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _ensure_utf8_console() -> None:
    """把 stdout/stderr 切到 UTF-8。

    Windows 控制台默认 GBK，遇到中文/校验报告中的 \\ufffd 替换字符会抛
    UnicodeEncodeError。reconfigure 是 Python 3.7+ 的标准做法；在 Linux/macOS
    上等价于 no-op（本来就是 UTF-8）。模块导入时立即生效，确保 main() 入口、
    被测试直接调用的 cmd_* 函数、以及任何 print 调用都受保护。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


_ensure_utf8_console()

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_JINJA_ENV = Environment(
    loader=FileSystemLoader(str(PROMPTS_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


def render_prompt(name: str, **context: Any) -> str:
    """渲染 prompts/<name>.j2 任务包模板。

    所有任务包都从 prompts/ 单一来源生成，避免脚本和模板漂移。
    缺变量时 StrictUndefined 立刻报错，不静默渲染成空。
    """
    return _JINJA_ENV.get_template(name).render(**context)

REQUIRED_DIRS = [
    "原文", "原文拆解", "章节处理", "章节处理/_任务包", "章节处理/_修复任务",
    "质量治理/delta校验", "质量治理/章节校验", "质量治理/规范化", "质量治理/周期审计", "质量治理/按需治理", "质量治理/最终审计",
    "故事结构版本", "结构变更日志",
    "全书分析/_任务包", "全书分析/剧情结构", "全书分析/人物分析", "全书分析/风格分析", "全书分析/世界观", "全书分析/视觉资产", "全书分析/故事结构", "全书分析/迭代版本", "全书分析/局部分析",
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
    "narrative_structure": [
        "全书分析/故事结构/三幕式结构图.md",
        "全书分析/故事结构/Brooks四部分结构图.md",
        "全书分析/故事结构/Freytag五段结构图.md",
        "全书分析/故事结构/故事七要素档案.md",
        "全书分析/故事结构/故事力学评估.md",
        "全书分析/故事结构/故事工程学评估.md",
        "全书分析/故事结构/小说骨架.md",
    ],
    "visual_assets": ["全书分析/视觉资产/视觉资产清单.md", "全书分析/视觉资产/关键场景分镜表.md", "全书分析/视觉资产/AI绘图提示词素材.md", "全书分析/视觉资产/角色外观一致性表.md", "全书分析/视觉资产/场景氛围表.md"],
}
MULTI_UNIT_ARTIFACT_RE = re.compile(r"^第\d+\s*(?:-|—|–|~|～|至|到)\s*\d+章_.*\.(?:md|json)$")
# 单次子进程调用超时（秒）。生产场景默认 600；测试可用 ND_COMMAND_TIMEOUT=10
# 之类的更短值，让卡死的子进程更快暴露而不是等满 10 分钟。
INTERNAL_COMMAND_TIMEOUT_SECONDS = int(os.getenv("ND_COMMAND_TIMEOUT", "600"))


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须为非负整数")
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


# 进程内 normalize 去抖缓存：project_dir → (mtime_ns, size) of story after last normalize。
# 同一进程里 ensure_story 会被反复调用（测试、GUI、连续提交），每次都拉起
# normalize_story_schema 子进程在 Windows 上要 ~2 秒冷启动。只要 story 自上次
# normalize 后没有被任何 in-place 子进程改动（merge_delta、apply_governance 等
# 改完后 mtime 变化会自动让缓存失效），就跳过这次 subprocess。
_ENSURE_STORY_NORMALIZE_CACHE: Dict[str, Tuple[int, int]] = {}


def _story_signature(story: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = story.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def ensure_story(project_dir: Path) -> Path:
    init_dirs(project_dir)
    story = project_dir / "故事结构_增量.json"
    if not story.is_file():
        run_cmd([sys.executable, str(script_path("progress_manager.py")), "init-incremental", str(project_dir)])
    cache_key = str(project_dir.resolve())
    current_sig = _story_signature(story)
    if current_sig is not None and _ENSURE_STORY_NORMALIZE_CACHE.get(cache_key) == current_sig:
        return story
    # 保全式规范化，避免历史字段被强 schema 卡死。
    run_cmd([sys.executable, str(script_path("normalize_story_schema.py")), str(story), "--in-place"], project_dir / "质量治理" / "规范化" / "normalize_latest.txt")
    post_sig = _story_signature(story)
    if post_sig is not None:
        _ENSURE_STORY_NORMALIZE_CACHE[cache_key] = post_sig
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
    task_dir = project_dir / "章节处理" / "_任务包"
    return {
        "chapter": chapter,
        "analysis": project_dir / "章节处理" / chapter.name,
        "delta": project_dir / "章节处理" / f"{base}.json",
        "delta_report": project_dir / "质量治理" / "delta校验" / f"{base}.json",
        "chapter_report": project_dir / "质量治理" / "章节校验" / f"{base}.json",
        "before": project_dir / "故事结构版本" / f"story_before_ch{seq:03d}.json",
        "after": project_dir / "故事结构版本" / f"story_after_ch{seq:03d}.json",
        "diff": project_dir / "结构变更日志" / f"diff_ch{seq:03d}.json",
        "analysis_task": task_dir / f"task_ch{seq:03d}_analysis.md",
        "delta_task": task_dir / f"task_ch{seq:03d}_delta.md",
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

    compress_delta_report = project_dir / "质量治理" / "delta校验" / f"compress_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(p["delta"]), "--report", str(compress_delta_report)])
    if rc != 0:
        print(f"重放失败: 第{seq:03d}章 Delta 标签压缩失败，见: {compress_delta_report}")
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
    compress_struct_report = project_dir / "质量治理" / "规范化" / f"compress_after_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--structure", str(story), "--report", str(compress_struct_report)])
    if rc != 0:
        shutil.copy2(rollback_tmp, story)
        print(f"重放失败: 第{seq:03d}章合并后标签压缩失败，已回滚，见: {compress_struct_report}")
        return 1
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


def get_previous_correction(project_dir: Path, current_start: int) -> Optional[Dict[str, str]]:
    """返回上一周期 correction JSON 的 {name, content}，无则 None。"""
    if current_start <= 1:
        return None
    audit_dir = project_dir / "质量治理" / "周期审计"
    if not audit_dir.is_dir():
        return None
    for p in sorted(audit_dir.glob("correction_*.json"), reverse=True):
        m = re.match(r"correction_(\d+)-(\d+)\.json", p.name)
        if m and int(m.group(2)) < current_start:
            return {"name": p.name, "content": read_excerpt(p, 5000)}
    return None


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

    story_size = story.stat().st_size if story.is_file() else 0
    story_data = load_json(story, {})

    full_story_json = ""
    story_index = ""
    relevant_details = ""
    if story_size < 100 * 1024:
        full_story_json = json.dumps(story_data, ensure_ascii=False, indent=2)
    elif story_size <= 200 * 1024:
        story_index = build_lightweight_index(story)
    else:
        story_index = build_lightweight_index(story)
        relevant_details = extract_relevant_elements_for_audit(story_data, project_dir, start, end)

    chapters: List[Dict[str, Any]] = []
    for seq in range(start, end + 1):
        chapter = chapter_by_seq(project_dir, seq)
        if not chapter:
            chapters.append({"seq": seq, "missing": True})
            continue
        p = artifact_paths(project_dir, chapter)
        chapters.append({
            "seq": seq,
            "missing": False,
            "chapter_path": p["chapter"],
            "analysis_path": p["analysis"],
            "delta_path": p["delta"],
            "analysis_excerpt": read_excerpt(p["analysis"]),
            "delta_excerpt": read_excerpt(p["delta"]),
        })

    content = render_prompt(
        "audit.j2",
        start=start,
        end=end,
        correction_path=paths["correction"],
        run_pipeline_path=script_path("run_pipeline.py"),
        project_dir=project_dir,
        full_story_json=full_story_json,
        story_index=story_index,
        relevant_details=relevant_details,
        previous_correction=get_previous_correction(project_dir, start),
        chapters=chapters,
    )

    paths["pack"].parent.mkdir(parents=True, exist_ok=True)
    paths["pack"].write_text(content, encoding="utf-8")
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


def run_periodic_governance(
    project_dir: Path,
    start: int,
    end: int,
) -> int:
    paths = audit_paths(project_dir, start, end)
    write_audit_pack(project_dir, start, end)
    if not paths["correction"].is_file() or paths["correction"].stat().st_size == 0:
        update_audit_status(
            paths,
            status="awaiting_agent",
            action="读取审计任务包，产出真实 correction Delta，然后再次运行 run_pipeline.py run。",
        )
        print(f"周期审计 {start:03d}-{end:03d} 待Agent处理：{paths['pack']}")
        print(f"请产出真实治理补丁：{paths['correction']}")
        return 2

    rc = commit_governance(project_dir, str(paths["correction"]))
    if rc == 0:
        print(f"周期审计 {start:03d}-{end:03d} 治理补丁已提交并通过校验。")
        return 0

    update_audit_status(
        paths,
        status="awaiting_agent",
        action="读取治理校验报告，修复 correction Delta，然后再次运行 run_pipeline.py run。",
    )
    print(f"周期审计 {start:03d}-{end:03d} 治理补丁未通过；待Agent修复后重试。")
    return 2


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


def prepare_chapter_analysis(project_dir: Path, chapter: Path, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    p = artifact_paths(project_dir, chapter)
    if p["analysis_task"].exists() and not force:
        print(f"章节分析任务包已存在: {p['analysis_task']}")
        print(f"请只填写章节分析MD: {p['analysis']}")
        return 0
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    chapter_seq = seq_from_file(chapter)
    content = render_prompt(
        "chapter_analysis.j2",
        chapter_seq=chapter_seq,
        analysis_path=p["analysis"],
        chapter_text=chapter_text,
    )
    p["analysis_task"].parent.mkdir(parents=True, exist_ok=True)
    p["analysis_task"].write_text(content, encoding="utf-8")
    print(f"已生成章节分析任务包: {p['analysis_task']}")
    print(f"请填写章节分析MD: {p['analysis']}")
    return 0


def prepare_chapter_delta(project_dir: Path, chapter: Path, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    story = ensure_story(project_dir)
    p = artifact_paths(project_dir, chapter)
    if not p["analysis"].is_file():
        print(f"缺章节分析MD: {p['analysis']}")
        return 2
    if p["delta_task"].exists() and not force:
        print(f"Delta提取任务包已存在: {p['delta_task']}")
        print(f"请只填写本章Delta JSON: {p['delta']}")
        return 0
    chapter_seq = seq_from_file(chapter)
    chapter_id = str(chapter_seq).zfill(4)
    chapter_time_str = chapter_time(chapter_seq)
    chapter_text = chapter.read_text(encoding="utf-8", errors="ignore")
    analysis_text = p["analysis"].read_text(encoding="utf-8", errors="ignore")
    summary = build_lightweight_index(story)

    content = render_prompt(
        "delta_extract.j2",
        chapter_seq=chapter_seq,
        chapter_id=chapter_id,
        delta_path=p["delta"],
        summary=summary,
        analysis_text=analysis_text,
        chapter_text=chapter_text,
        chapter_time=chapter_time_str,
    )

    p["delta_task"].parent.mkdir(parents=True, exist_ok=True)
    p["delta_task"].write_text(content, encoding="utf-8")
    print(f"已生成Delta提取任务包: {p['delta_task']}")
    print(f"请填写本章Delta JSON: {p['delta']}")
    return 0


def prepare_chapter(project_dir: Path, seq: Optional[int] = None, force: bool = False) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    chapter = chapter_by_seq(project_dir, seq) if seq else next_incomplete(project_dir)
    if not chapter:
        print("没有待处理章节。")
        return 0
    p = artifact_paths(project_dir, chapter)
    if not p["analysis"].is_file():
        return prepare_chapter_analysis(project_dir, chapter, force)
    if not p["delta"].is_file():
        return prepare_chapter_delta(project_dir, chapter, force)
    print(f"第{seq_from_file(chapter):03d}章的章节分析MD和Delta JSON均已存在。")
    return 0


def write_repair_pack(project_dir: Path, chapter: Path, reason: str, report_paths: List[Path]) -> None:
    p = artifact_paths(project_dir, chapter)
    reports: List[Dict[str, str]] = []
    for rp in report_paths:
        if rp.is_file():
            reports.append({
                "name": rp.name,
                "excerpt": rp.read_text(encoding="utf-8", errors="ignore")[:30000],
            })
    content = render_prompt(
        "repair_chapter.j2",
        chapter_seq=seq_from_file(chapter),
        reason=reason,
        reports=reports,
    )
    p["repair"].parent.mkdir(parents=True, exist_ok=True)
    p["repair"].write_text(content, encoding="utf-8")
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
        print("已交接：等待章节分析MD。")
        return 2
    if not p["delta"].is_file():
        print(f"缺Delta JSON: {p['delta']}")
        prepare_chapter(project_dir, seq)
        print("已交接：等待基于章节分析的Delta JSON。")
        return 2

    # 入库前先做确定性标签压缩，把受控前缀标签和超限自由标签搬到 详情.补充标签，
    # 让原本因 taxonomy 失配进 repair 循环的报错根本不发生。
    compress_delta_report = project_dir / "质量治理" / "delta校验" / f"compress_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--delta", str(p["delta"]), "--report", str(compress_delta_report)])
    if rc != 0:
        print(f"Delta 标签压缩失败，见: {compress_delta_report}")
        write_repair_pack(project_dir, chapter, "compress_tags.py(Delta)失败", [compress_delta_report])
        return 1

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

    # 合并后再压一次：跨章累积的标签集即使每章入口已合规，也可能因新 Delta 与既有元素合并而越过上限。
    compress_struct_report = project_dir / "质量治理" / "规范化" / f"compress_after_ch{seq:03d}.json"
    rc = run_cmd([sys.executable, str(script_path("compress_tags.py")), "--structure", str(story), "--report", str(compress_struct_report)])
    if rc != 0:
        shutil.copy2(backup_tmp, story)
        print(f"合并后标签压缩失败，已回滚，见: {compress_struct_report}")
        write_repair_pack(project_dir, chapter, "compress_tags.py(Structure)失败", [compress_struct_report])
        return 1

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
        if not p["analysis"].is_file():
            print(f"章节分析任务包: {p['analysis_task']}")
        elif not p["delta"].is_file():
            print(f"Delta提取任务包: {p['delta_task']}")
    else:
        print("所有章节均已可信完成。")
    return 0


def cmd_run(
    project_dir: Path,
    max_chapters: int = 0,
    audit_interval: int = 5,
) -> int:
    init_dirs(project_dir)
    if not guard_single_unit_artifacts(project_dir):
        return 1
    ensure_story(project_dir)
    for audit in unresolved_audits(project_dir):
        rc = run_periodic_governance(project_dir, audit["_start"], audit["_end"])
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
        if not p["analysis"].is_file():
            prepare_chapter(project_dir, seq)
            print("已交接：等待章节分析MD。")
            return 2
        if not p["delta"].is_file():
            prepare_chapter(project_dir, seq)
            print("已交接：等待基于章节分析的Delta JSON。")
            return 2
        rc = commit_chapter(project_dir, seq)
        if rc != 0:
            return rc
        processed += 1
        if audit_interval and seq % audit_interval == 0:
            start = seq - audit_interval + 1
            rc = run_periodic_governance(project_dir, start, seq)
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

    report_excerpt = read_excerpt(report, 30000) if report.is_file() else ""

    analysis_specs = [
        ("全书分析/剧情结构/章节梗概汇总.md", "章节梗概汇总"),
        ("全书分析/人物分析/人物档案.md", "人物档案"),
        ("全书分析/人物分析/人物关系.md", "人物关系"),
        ("全书分析/剧情结构/伏笔追踪.md", "伏笔追踪"),
        ("全书分析/剧情结构/冲突图谱.md", "冲突图谱"),
        ("全书分析/视觉资产/视觉资产清单.md", "视觉资产清单"),
        ("全书分析/视觉资产/关键场景分镜表.md", "关键场景分镜表"),
        ("全书分析/故事结构/三幕式结构图.md", "三幕式结构图"),
        ("全书分析/故事结构/Brooks四部分结构图.md", "Brooks四部分结构图"),
        ("全书分析/故事结构/Freytag五段结构图.md", "Freytag五段结构图"),
        ("全书分析/故事结构/故事七要素档案.md", "故事七要素档案"),
        ("全书分析/故事结构/故事力学评估.md", "故事力学评估"),
        ("全书分析/故事结构/故事工程学评估.md", "故事工程学评估"),
        ("全书分析/故事结构/小说骨架.md", "小说骨架"),
    ]
    analysis_refs: List[Dict[str, Any]] = []
    for rel, title in analysis_specs:
        path = project_dir / rel
        if path.is_file() and path.stat().st_size > 0:
            analysis_refs.append({
                "title": title,
                "path": path,
                "excerpt": read_excerpt(path, 15000),
            })

    audit_refs: List[Dict[str, Any]] = []
    pa_dir = project_dir / "质量治理" / "周期审计"
    if pa_dir.is_dir():
        for md in sorted(pa_dir.glob("audit_*.md")):
            audit_refs.append({"name": md.name, "lang": "markdown",
                               "excerpt": read_excerpt(md, 8000)})
        for cj in sorted(pa_dir.glob("correction_*.json")):
            audit_refs.append({"name": cj.name, "lang": "json",
                               "excerpt": read_excerpt(cj, 8000)})

    content = render_prompt(
        "final_draft.j2",
        run_pipeline_path=script_path("run_pipeline.py"),
        project_dir=project_dir,
        report_excerpt=report_excerpt,
        story_index=build_lightweight_index(draft),
        analysis_refs=analysis_refs,
        audit_refs=audit_refs,
    )

    task_pack.write_text(content, encoding="utf-8")
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
        content = render_prompt(
            "repair_final.j2",
            report_excerpt=read_excerpt(report, 30000),
            run_pipeline_path=script_path("run_pipeline.py"),
            project_dir=project_dir,
        )
        repair.write_text(content, encoding="utf-8")
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
    if getattr(args, "per_chapter", False):
        cmd.append("--per-chapter")
    if getattr(args, "aggregate", False):
        cmd.append("--aggregate")
    return run_cmd(cmd)


def visual_assets_per_chapter_relpaths(seq: int) -> List[str]:
    """与 analysis_context_pack.visual_assets_per_chapter_outputs 保持同步的相对路径。"""
    base = f"全书分析/视觉资产/分章/ch{seq:03d}"
    return [
        f"{base}/视觉资产清单.md",
        f"{base}/关键场景分镜表.md",
        f"{base}/AI绘图提示词素材.md",
        f"{base}/角色外观一致性表.md",
        f"{base}/场景氛围表.md",
    ]


def chapter_has_per_chapter_visual(project_dir: Path, seq: int) -> bool:
    for rel in visual_assets_per_chapter_relpaths(seq):
        path = project_dir / rel
        if not (path.is_file() and path.stat().st_size > 0):
            return False
    return True


def chapters_eligible_for_visual(project_dir: Path) -> List[int]:
    """已经有章节分析MD的章节才能跑视觉资产。"""
    result: List[int] = []
    for ch in chapter_files(project_dir):
        seq = seq_from_file(ch)
        analysis = project_dir / "章节处理" / ch.name
        if analysis.is_file() and analysis.stat().st_size > 0:
            result.append(seq)
    return result


def cmd_visual_assets_auto(args: argparse.Namespace) -> int:
    """按章自主推进视觉资产生成；遇到缺产物的章节返回 2 等主控Agent写入。"""
    project_dir = Path(args.project_dir)
    init_dirs(project_dir)
    eligible = chapters_eligible_for_visual(project_dir)
    if not eligible:
        print("尚无任何章节具备章节分析MD；请先完成章节分析后再运行 visual-assets-auto。")
        return 1

    pending = [seq for seq in eligible if not chapter_has_per_chapter_visual(project_dir, seq)]
    if pending:
        # 取第一个待办章节生成单章任务包
        next_seq = pending[0]
        cmd = [
            sys.executable,
            str(script_path("analysis_context_pack.py")),
            str(project_dir),
            "--task", "visual_assets",
            "--chapters", str(next_seq),
            "--per-chapter",
            "--include-original", args.include_original,
            "--max-pack-chars", str(args.max_pack_chars),
            "--max-original-chars", str(args.max_original_chars),
            "--max-analysis-chars", str(args.max_analysis_chars),
        ]
        rc = run_cmd(cmd)
        if rc != 0:
            return rc
        remaining = len(pending) - 1
        print(f"已生成第{next_seq:03d}章视觉资产任务包；剩余 {remaining} 章待生成。")
        print("请主控Agent按 reduce_prompt 写入分章五件套后再次运行。")
        return 2

    # 全部分章产物齐全，生成 aggregate 任务包
    aggregate_marker = project_dir / "全书分析" / "视觉资产" / "视觉资产清单.md"
    if aggregate_marker.is_file() and aggregate_marker.stat().st_size > 0 and not getattr(args, "force_aggregate", False):
        print("所有章节分章视觉资产已生成，且顶层汇总已存在。使用 --force-aggregate 重新生成 aggregate 任务包。")
        return 0

    cmd = [
        sys.executable,
        str(script_path("analysis_context_pack.py")),
        str(project_dir),
        "--task", "visual_assets",
        "--aggregate",
    ]
    rc = run_cmd(cmd)
    if rc != 0:
        return rc
    print("所有章节分章视觉资产已生成；已交接：等待主控Agent按 aggregate 任务包写入顶层五件套。")
    return 2


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

    p_prepare = sub.add_parser("prepare-chapter", help="根据当前产物状态生成章节分析或Delta提取任务包")
    p_prepare.add_argument("project_dir")
    p_prepare.add_argument("--chapter", type=int, default=0)
    p_prepare.add_argument("--force", action="store_true")

    p_commit = sub.add_parser("commit-chapter", help="提交已填写好的章节分析MD和Delta")
    p_commit.add_argument("project_dir")
    p_commit.add_argument("--chapter", type=int, required=True)
    p_commit.add_argument("--force", action="store_true")

    p_run = sub.add_parser("run", help="持续提交已具备产物的章节；周期审计任务由主控Agent处理后续提交")
    p_run.add_argument("project_dir")
    p_run.add_argument("--max-chapters", type=int, default=0)
    p_run.add_argument("--audit-interval", type=int, default=5, help="每N章执行一次自动周期治理；0表示关闭")

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
    p_pack.add_argument("--task", choices=["summary", "characters", "plot", "style", "visual_assets", "report", "custom", "worldview", "plotlines", "outline", "detailed_outline", "chapter_structure", "narrative_structure"], required=True)
    p_pack.add_argument("--chapters", default="all")
    p_pack.add_argument("--targets", nargs="*", default=[])
    p_pack.add_argument("--question", default="")
    p_pack.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_pack.add_argument("--max-pack-chars", type=int, default=70000)
    p_pack.add_argument("--max-original-chars", type=int, default=6000)
    p_pack.add_argument("--max-analysis-chars", type=int, default=12000)
    p_pack.add_argument("--out-dir", default="")
    p_pack.add_argument("--per-chapter", action="store_true", help="每章一份独立任务包（visual_assets / chapter_structure），产物写入对应的 全书分析/<维度>/分章/chNNN/ 目录")
    p_pack.add_argument("--aggregate", action="store_true", help="（仅 visual_assets）生成顶层汇总任务包，从 分章/chNNN/ 合并顶层五件套")

    p_status = sub.add_parser("analysis-status", help="查看全书/局部分析状态")
    p_status.add_argument("project_dir")

    p_vauto = sub.add_parser("visual-assets-auto", help="按章自主推进视觉资产生成（每次返回2交接一章，全部完成后交接aggregate）")
    p_vauto.add_argument("project_dir")
    p_vauto.add_argument("--include-original", choices=["none", "sample", "full"], default="sample")
    p_vauto.add_argument("--max-pack-chars", type=int, default=70000)
    p_vauto.add_argument("--max-original-chars", type=int, default=6000)
    p_vauto.add_argument("--max-analysis-chars", type=int, default=12000)
    p_vauto.add_argument("--force-aggregate", action="store_true", help="即便顶层汇总已存在，也重新生成 aggregate 任务包")

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
        return cmd_run(project_dir, args.max_chapters, args.audit_interval)
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
    if args.cmd == "visual-assets-auto":
        return cmd_visual_assets_auto(args)
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
