"""arisk 安装引导页（`services/arisk_guide.py`）的纯渲染测试。

约束：只喂输入、断言输出 —— 不触网、不读系统时钟、不写磁盘。
"""

from services.arisk_guide import GITHUB_URL, render_install_guide


def _guide(missing=None, port_web=True, port_live=True):
    return render_install_guide(
        arisk_dir="/Users/xinxin/workspace/arisk",
        live_url="http://127.0.0.1:8899",
        missing=missing or [],
        port_web=port_web,
        port_live=port_live,
    )


class TestInstallGuide:
    def test_shows_github_and_install_steps(self):
        html = _guide(["安装目录不存在：/Users/xinxin/workspace/arisk"])
        assert GITHUB_URL in html
        # 克隆 / 依赖 / 首次拉数 / 启动，四步缺一不可
        assert "git clone" in html
        assert "pip install -r requirements.txt" in html
        assert "update_arisk_data.py" in html
        assert "bash start.sh" in html
        # 期望路径与实时代理地址都要写出来（用户才知道装哪里）
        assert "/Users/xinxin/workspace/arisk" in html
        assert "http://127.0.0.1:8899" in html

    def test_missing_list_is_rendered(self):
        html = _guide(["面板文件缺失：/x/arisk_monitor_local.html"])
        assert "缺少："
        assert "arisk_monitor_local.html" in html

    def test_port_badges_follow_probe(self):
        html = _guide([], port_web=False, port_live=False)
        assert "面板服务 :8788" in html
        # 两个端口都不通 → 两行 ❌；目录/面板/数据三项正常 → 三行 ✅
        assert html.count(">❌</span>") == 2
        assert html.count(">✅</span>") == 3

    def test_html_escaped_paths(self):
        """路径里带特殊字符时不能被当成标签注入。"""
        html = render_install_guide(
            arisk_dir='/tmp/a<b>"x"',
            live_url="http://127.0.0.1:8899",
            missing=["目录 <script>"],
            port_web=True,
            port_live=True,
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
