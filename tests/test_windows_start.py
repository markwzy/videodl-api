"""用独立子进程验证启动脚本确实向服务传递登录文件，而不读取真实凭证。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要系统自带的命令解释器")


@pytest.fixture
def launch(monkeypatch, tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    script = project / "start-windows.ps1"
    shutil.copyfile(Path(__file__).parents[1] / "start-windows.ps1", script)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")], check=True)
    # 替代服务入口只报告环境变量，避免测试访问账号、视频平台或实际启动监听服务。
    (project / "uvicorn.py").write_text(
        'import json, os; print(json.dumps({"cookie_file": os.getenv("X_COOKIES_FILE")}))', encoding="utf-8",
    )
    monkeypatch.delenv("X_COOKIES_FILE", raising=False)
    monkeypatch.delenv("BROWSER_PATH", raising=False)
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

    def run(*args):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
            cwd=tmp_path, env=os.environ.copy(), capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        )
        return result

    return project, run


@pytest.mark.parametrize("source", ["default", "environment", "argument"])
def test_cookie_file_is_selected_and_passed_as_absolute_path(launch, monkeypatch, source):
    project, run = launch
    default = project / ".secrets" / "x-cookies.txt"
    default.parent.mkdir()
    default.write_text("fake default session", encoding="utf-8")
    selected = default
    args = []
    if source != "default":
        selected = project / "custom session.txt"
        selected.write_text("fake custom session", encoding="utf-8")
        if source == "environment":
            monkeypatch.setenv("X_COOKIES_FILE", selected.name)
        else:
            monkeypatch.setenv("X_COOKIES_FILE", str(project / "wrong-account.txt"))
            args = ["-XCookiesFile", selected.name]
    result = run(*args)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(data["cookie_file"]) == selected
    assert selected.read_text(encoding="utf-8").startswith("fake ")


def test_no_cookie_file_starts_anonymously(launch):
    project, run = launch
    result = run()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["cookie_file"] is None


def test_explicit_missing_file_stops_startup_even_if_default_exists(launch, monkeypatch):
    project, run = launch
    default = project / ".secrets" / "x-cookies.txt"
    default.parent.mkdir()
    default.write_text("fake default session", encoding="utf-8")
    monkeypatch.setenv("X_COOKIES_FILE", str(project / "missing.txt"))
    result = run()
    assert result.returncode != 0
    assert "X_COOKIES_FILE" in result.stderr
