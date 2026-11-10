"""数据目录解析（``paths.py``）的测试。

约定：纯逻辑、不触网、不写真实数据目录——只折腾环境变量与 tmp_path。
覆盖两件事：

1. ``data_dir()`` 的解析规则（默认值 / 覆盖 / ``~`` / 相对路径 / 空串）；
2. 各服务模块的落盘常量**跟随环境变量**——它们都在导入期求值，
   桌面壳与容器正是"先设环境变量、再起进程"的用法。
"""

from __future__ import annotations

import importlib
import os

import pytest

import paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDataDir:
    def test_default_is_repo_data(self, monkeypatch):
        monkeypatch.delenv(paths.ENV, raising=False)
        assert paths.data_dir() == os.path.join(ROOT, "data")

    def test_blank_env_falls_back_to_default(self, monkeypatch):
        """空串 / 纯空白都当作没设置，避免 shell 里 `ETF_DATA_DIR=` 把数据写到当前目录。"""
        monkeypatch.setenv(paths.ENV, "   ")
        assert paths.data_dir() == os.path.join(ROOT, "data")

    def test_absolute_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, str(tmp_path))
        assert paths.data_dir() == str(tmp_path)

    def test_expanduser(self, monkeypatch):
        monkeypatch.setenv(paths.ENV, "~/etf-data-test")
        assert paths.data_dir() == os.path.join(os.path.expanduser("~"), "etf-data-test")

    def test_relative_resolved_from_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, "rel-data")
        monkeypatch.chdir(tmp_path)
        assert paths.data_dir() == str(tmp_path / "rel-data")

    def test_whitespace_trimmed(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, f"  {tmp_path}  ")
        assert paths.data_dir() == str(tmp_path)


class TestDataPath:
    def test_join_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, str(tmp_path))
        assert paths.data_path("a.json") == str(tmp_path / "a.json")

    def test_empty_name_returns_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, str(tmp_path))
        assert paths.data_path("") == str(tmp_path)

    def test_nested_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv(paths.ENV, str(tmp_path))
        assert paths.data_path(os.path.join("sub", "b.db")) == str(tmp_path / "sub" / "b.db")


class TestModuleConstants:
    """落盘常量必须是 `paths.data_path(...)` 求值出来的，不能各自再拼 `<repo>/data`。"""

    @pytest.mark.parametrize("module,attr,name", [
        ("services.board_map", "_MAP_FILE", "tape_board_map.json"),
        ("services.boardflow", "_DB_FILE", "board_flow.db"),
        ("services.deploy", "TOKEN_PATH", ".deploy_token"),
        ("services.em", "_OPT_PATH", "opt_history.jsonl"),
        ("services.em", "_FLOW_PATH", "flow_history.jsonl"),
        ("services.em", "_IDX_PATH", "index_history.jsonl"),
        ("services.em", "_OPT_UNIVERSE_PATH", "option_universe.json"),
        ("services.iv_history", "_PATH", "iv_history.jsonl"),
        ("services.index_levels_manual", "_PATH", "index_levels_manual.json"),
        ("services.limitup", "_DB_FILE", "limitup.db"),
        ("services.pick_tracker", "_PATH", "pick_history.json"),
        ("services.push", "_PATH", "push_settings.json"),
        ("services.reversal", "_SIG_PATH", "reversal_signals.json"),
        ("services.user_settings", "_PATH", "settings.json"),
        ("services.wangwang", "_DB_FILE", "wangwang.db"),
    ])
    def test_constant_follows_env(self, monkeypatch, tmp_path, module, attr, name):
        monkeypatch.setenv(paths.ENV, str(tmp_path))
        mod = importlib.import_module(module)
        try:
            mod = importlib.reload(mod)
            assert getattr(mod, attr) == str(tmp_path / name)
        finally:
            # 还原：否则后续用例会对着临时目录读写
            monkeypatch.delenv(paths.ENV, raising=False)
            importlib.reload(mod)

    def test_default_layout_unchanged(self, monkeypatch):
        monkeypatch.delenv(paths.ENV, raising=False)
        from services import push

        importlib.reload(push)
        assert push._PATH == os.path.join(ROOT, "data", "push_settings.json")
