"""server.py 集成测试：FakeHarness 注入，不触真实 harness。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # import server

import server
from server import create_app


class FakeHarness:
    """DeepSeekHarness 替身：按提示词里的产物约定写文件，模拟 agent 行为。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls: list[str] = []

    def run(self, prompt: str, session_id: str | None = None):
        self.calls.append(prompt)
        import re

        match = re.search(r"output/([0-9a-f]+)", prompt)
        if match is not None:
            target = self.workspace / "output" / match.group(1) / "dashboard.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("<html>fake dashboard</html>", encoding="utf-8")
        return SimpleNamespace(final_response="fake-ok", finish_reason="completed")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离环境：CONFIG_DSH_HOME/工作区都在 tmp，并预装 onlyedu-dashboard 技能。"""
    monkeypatch.setattr(server, "CONFIG_DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setenv("DASHBOARD_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("DASHBOARD_API_KEY", raising=False)
    skill_dir = tmp_path / "dsh-home" / "skills" / "onlyedu-dashboard"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: onlyedu-dashboard\n---\nfake skill", encoding="utf-8"
    )
    server.TASKS.clear()
    return tmp_path / "workspace"


def _submit(client: TestClient, **overrides: object) -> dict:
    body: dict = {"prompt": "查询学员张三的班级", "skill": "onlyedu-dashboard"}
    body.update(overrides)
    return client.post("/api/v1/tasks", json=body)


def _wait_terminal(client: TestClient, task_id: str) -> dict:
    for _ in range(100):
        view = client.get(f"/api/v1/tasks/{task_id}").json()
        if view["status"] in ("completed", "failed"):
            return view
        time.sleep(0.05)
    pytest.fail("任务未到终态")


def test_full_flow_completed_with_artifacts(env: Path) -> None:
    client = TestClient(create_app(harness_factory=lambda: FakeHarness(env)))

    created = _submit(client)
    assert created.status_code == 201
    task_id = created.json()["task_id"]

    view = _wait_terminal(client, task_id)
    assert view["status"] == "completed"
    assert view["final_response"] == "fake-ok"
    assert view["artifacts"][0]["path"].startswith("output/")


def test_prompt_contains_skill_instruction(env: Path) -> None:
    fake = FakeHarness(env)
    client = TestClient(create_app(harness_factory=lambda: fake))

    _wait_terminal(client, _submit(client).json()["task_id"])

    assert "请使用 `onlyedu-dashboard` 技能完成以下任务" in fake.calls[0]
    assert "output/" in fake.calls[0]


def test_unknown_or_invalid_skill_rejected(env: Path) -> None:
    client = TestClient(create_app(harness_factory=lambda: FakeHarness(env)))

    assert _submit(client, skill="no-such").status_code == 400
    assert _submit(client, skill="../escape").status_code == 400


def test_harness_error_marks_task_failed(env: Path) -> None:
    class Broken:
        def run(self, prompt: str, session_id: str | None = None):
            raise RuntimeError("runtime 死了")

    client = TestClient(create_app(harness_factory=Broken))

    view = _wait_terminal(client, _submit(client).json()["task_id"])

    assert view["status"] == "failed"
    assert "runtime 死了" in view["error"]


def test_auth_and_404(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_KEY", "secret")
    client = TestClient(create_app(harness_factory=lambda: FakeHarness(env)))
    headers = {"X-Api-Key": "secret"}

    assert client.get("/healthz").status_code == 200  # 健康检查豁免
    assert _submit(client).status_code == 401  # 无 key 被拒
    assert client.get("/api/v1/tasks/missing", headers=headers).status_code == 404


def test_hardcoded_config_takes_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """常量区写死优先于环境变量：不设 DSH_HOME/ZAI_API_KEY 也能构建应用。"""
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.setattr(server, "CONFIG_DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setattr(server, "CONFIG_DASHBOARD_DSH_BIN", "")
    monkeypatch.setenv("DASHBOARD_WORKSPACE_DIR", str(tmp_path / "workspace"))
    server.TASKS.clear()

    client = TestClient(create_app(harness_factory=lambda: FakeHarness(tmp_path / "workspace")))

    assert client.get("/healthz").status_code == 200


def test_dsh_home_falls_back_to_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG_DSH_HOME 留空（现口径：路径一律走环境变量）时取 DSH_HOME 环境变量。"""
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "env-home"))
    monkeypatch.setattr(server, "CONFIG_DSH_HOME", "")

    assert server._cfg("DSH_HOME", server.CONFIG_DSH_HOME) == str(tmp_path / "env-home")
