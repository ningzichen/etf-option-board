"""数据目录的唯一入口：不要在业务模块里各自拼 ``<repo>/data``。

默认行为与历史版本**完全一致**——仍是仓库根下的 ``data/``。
设置环境变量 ``ETF_DATA_DIR`` 可把整个数据目录搬走，用于：

- **桌面版（Windows / macOS）**：数据写用户目录。打包后代码在只读安装目录里，
  直接写 ``<repo>/data`` 会失败；macOS 往签名过的 ``.app`` 里写还会破坏签名。
- **容器**：只挂数据卷，不必把整个仓库挂进来。
- **同机多实例 / 测试隔离**：各指一个临时目录，互不干扰。

解析规则：``~`` 会展开，相对路径按当前工作目录转成绝对路径，空串/纯空白视为没设置。

注意：各模块的 ``_PATH`` 常量在**导入期**求值，所以必须在启动进程前设好环境变量
（桌面壳、systemd、docker run -e 都是这么做的）；进程内改环境变量需要 reload 才生效。
"""

from __future__ import annotations

import os

#: 环境变量名
ENV = "ETF_DATA_DIR"

#: 仓库根目录（本文件所在目录）
ROOT = os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """数据目录的绝对路径（不创建目录）。"""
    raw = (os.environ.get(ENV) or "").strip()
    if not raw:
        return os.path.join(ROOT, "data")
    return os.path.abspath(os.path.expanduser(raw))


def data_path(name: str = "") -> str:
    """数据目录下的文件路径（不创建目录）。``name`` 为空时返回目录本身。"""
    base = data_dir()
    return os.path.join(base, name) if name else base
