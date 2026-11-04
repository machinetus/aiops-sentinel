"""补丁应用器单测（design §5.3 第 4 步，确定性安全核心）。"""

from __future__ import annotations

import pytest

from src.workers.patcher import PatchRejected, apply_patch


def _worktree(tmp_path):
    wt = tmp_path / "wt"
    (wt / "target-app" / "demo_app").mkdir(parents=True)
    (wt / "target-app" / "tests").mkdir(parents=True)
    (wt / "target-app" / "demo_app" / "orders.py").write_text(
        "def create_order(amount):\n    total = amount * 2\n    return total\n",
        encoding="utf-8")
    return wt


def test_apply_replacement_and_new_test(tmp_path):
    wt = _worktree(tmp_path)
    stats = apply_patch(wt, [
        {"file": "target-app/demo_app/orders.py",
         "replacements": [{"old_text": "    total = amount * 2",
                           "new_text": "    total = int(amount) * 2"}]},
        {"file": "target-app/tests/test_orders_validation.py",
         "new_file": "def test_ok():\n    assert True\n"},
    ])
    assert "target-app/demo_app/orders.py" in stats.files
    assert stats.touched_tests is True
    assert "int(amount)" in (wt / "target-app/demo_app/orders.py").read_text()


def test_reject_non_whitelisted_path(tmp_path):
    wt = _worktree(tmp_path)
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [{"file": "src/router/main.py",
                          "replacements": [{"old_text": "a", "new_text": "b"}]}])
    assert "白名单" in str(ei.value)


def test_reject_path_escape(tmp_path):
    wt = _worktree(tmp_path)
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [{"file": "target-app/../src/x.py",
                          "replacements": [{"old_text": "a", "new_text": "b"}]}])
    assert "逃逸" in str(ei.value)


def test_reject_zero_match(tmp_path):
    wt = _worktree(tmp_path)
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [
            {"file": "target-app/demo_app/orders.py",
             "replacements": [{"old_text": "not exists", "new_text": "x"}]},
            {"file": "target-app/tests/test_x.py", "new_file": "def test(): pass\n"},
        ])
    assert "0 次" in str(ei.value)


def test_reject_multiple_match(tmp_path):
    wt = _worktree(tmp_path)
    (wt / "target-app" / "demo_app" / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [
            {"file": "target-app/demo_app/dup.py",
             "replacements": [{"old_text": "x = 1", "new_text": "x = 2"}]},
            {"file": "target-app/tests/test_x.py", "new_file": "def test(): pass\n"},
        ])
    assert "唯一" in str(ei.value)


def test_reject_without_tests(tmp_path):
    wt = _worktree(tmp_path)
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [
            {"file": "target-app/demo_app/orders.py",
             "replacements": [{"old_text": "    total = amount * 2",
                               "new_text": "    total = int(amount) * 2"}]},
        ])
    assert "测试" in str(ei.value)


def test_reject_too_many_lines(tmp_path):
    wt = _worktree(tmp_path)
    big = "x = 1\n" * 300
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [
            {"file": "target-app/demo_app/big.py", "new_file": big},
        ])
    assert "上限" in str(ei.value)


def test_reject_new_file_exists(tmp_path):
    wt = _worktree(tmp_path)
    with pytest.raises(PatchRejected) as ei:
        apply_patch(wt, [
            {"file": "target-app/tests/test_orders_validation.py",
             "new_file": "def test(): pass\n"},
            {"file": "target-app/tests/test_orders_validation.py",
             "new_file": "def test(): pass\n"},  # 第二次写同一文件
        ])
    assert "已存在" in str(ei.value)
