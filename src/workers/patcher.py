"""补丁应用器（design §5.3 第 4 步：确定性安全核心）。

LLM 输出的补丁必须逐条通过本模块校验，任何一条不满足 → PatchRejected，
整个任务失败、不建 PR：
1. 路径白名单：仅 ^target-app/（含其 tests/；根级 tests/ 属 agent 自身，禁止）
2. 路径逃逸防护：resolve 后必须仍在 worktree 内
3. search/replace：old_text 在文件中必须**唯一匹配**（0 次/多次均拒绝）
4. 变更行数上限（默认 200）
5. 必须触及测试文件（新增或修改 test_*.py / *_test.py）
6. new_file 目标不得已存在（防覆盖）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ALLOWED_PREFIX = "target-app/"
MAX_CHANGED_LINES = 200


class PatchRejected(Exception):
    """补丁未通过安全/正确性校验。"""


@dataclass
class PatchStats:
    files: list[str] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    touched_tests: bool = False

    @property
    def diff_stat(self) -> str:
        return f"+{self.added} −{self.removed} ({len(self.files)} files)"


def _is_test_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    return "tests/" in rel_path.replace("\\", "/") or name.startswith("test_") \
        or name.endswith("_test.py")


def _safe_target(worktree: Path, rel_path: str) -> Path:
    if not rel_path.startswith(ALLOWED_PREFIX):
        raise PatchRejected(f"路径不在白名单（仅 {ALLOWED_PREFIX}）: {rel_path}")
    target = (worktree / rel_path).resolve()
    allowed_root = (worktree / "target-app").resolve()
    if not str(target).startswith(str(allowed_root)):
        raise PatchRejected(f"路径逃逸（必须落在 target-app/ 内）: {rel_path}")
    return target


def apply_patch(worktree: Path, changes: list[dict]) -> PatchStats:
    """在 worktree 内应用补丁（原地写文件）。changes 来自 LLM 输出。"""
    if not changes:
        raise PatchRejected("补丁为空")
    stats = PatchStats()
    total_changed = 0

    for change in changes:
        rel = str(change.get("file") or "")
        target = _safe_target(worktree, rel)
        stats.files.append(rel)
        if _is_test_file(rel):
            stats.touched_tests = True

        new_file = change.get("new_file")
        if new_file is not None:
            if target.exists():
                raise PatchRejected(f"new_file 目标已存在: {rel}")
            content = str(new_file)
            total_changed += len(content.splitlines())
            stats.added += len(content.splitlines())
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            continue

        replacements = change.get("replacements") or []
        if not replacements:
            raise PatchRejected(f"{rel} 缺少 replacements")
        if not target.is_file():
            raise PatchRejected(f"目标文件不存在: {rel}")
        text = target.read_text(encoding="utf-8")
        for rep in replacements:
            old = str(rep.get("old_text") or "")
            new = str(rep.get("new_text") or "")
            if not old:
                raise PatchRejected(f"{rel} 的 old_text 为空")
            occurrences = text.count(old)
            if occurrences == 0:
                raise PatchRejected(f"{rel} 中 old_text 未匹配（0 次）")
            if occurrences > 1:
                raise PatchRejected(f"{rel} 中 old_text 匹配 {occurrences} 次（需唯一）")
            text = text.replace(old, new, 1)
            old_lines = len(old.splitlines())
            new_lines = len(new.splitlines())
            total_changed += max(old_lines, new_lines)
            stats.removed += old_lines
            stats.added += new_lines
        target.write_text(text, encoding="utf-8")

    if total_changed > MAX_CHANGED_LINES:
        raise PatchRejected(f"变更行数 {total_changed} 超过上限 {MAX_CHANGED_LINES}")
    if not stats.touched_tests:
        raise PatchRejected("补丁必须包含/修改测试文件")
    return stats
