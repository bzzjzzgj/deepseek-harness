from __future__ import annotations

import logging
import os
import re
import threading
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field
from deepseek_harness import DeepSeekHarness

# 全局日志格式配置
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)


# 本模块日志器
log = logging.getLogger("agent-api")


# ======================== 常量 =========================== *

SERVICE_ROOT = Path(__file__).resolve().parent
SITE_DASHBOARD_DIR = SERVICE_ROOT.parents[1] / "plugins" / "site-dashboard"


# harness 持久化根：一律走环境变量 DSH_HOME（容器由 Dockerfile ENV 设 /data/dsh-home，
# 宿主直跑自设 DSH_HOME），代码不写死路径。
CONFIG_DSH_HOME = ""
# 源码模式运行时
CONFIG_DASHBOARD_DSH_BIN = ""

# 默认模型提供者
DEFAULT_PROVIDER = "zai"
DEFAULT_MODEL = "glm-5.3-flash"
UPSTREAM_API_KEY_ENV = "ZAI_API_KEY"

OUTPUT_DIRNAME = "output"
SKILL_TASK_TEMPLATE = "请使用 `{skill}` 技能完成以下任务："
OUTPUT_RULE_TEMPLATE = (
    "所有产出文件必须写入 `{output_dir}` 目录（workspace 相对路径）。"
)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# 服务监听
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8100


# settings.yaml 里为 GLM 激活 pi-ai 路由的分节（启动时缺失则自动追加）
PI_AI_SETTINGS_SECTION = """\
# dashboard-api 自动添加：智谱 GLM 路由（端点/模型目录由 pi-ai 内置目录提供）
llm-pi-ai:
  providers:
    zai:
      apiKeyEnv: ZAI_API_KEY
"""


# task_id -> 任务状态字典
TASKS: dict[str, dict[str, Any]] = {}


class TaskCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    skill: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)


# 读取环境变量并做默认值/清洗处理
def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


# 如果代码中没有配置，直接取环境变量
def _cfg(env_key: str, hardcoded: str, default: str = "") -> str:
    return hardcoded.strip() or _env(env_key, default)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_app(*, harness_factory: Callable[[], Any] | None = None) -> FastAPI:
    dsh_home_raw = _cfg("DSH_HOME", CONFIG_DSH_HOME)
    if not dsh_home_raw:
        raise RuntimeError(
            "DSH_HOME 未配置：请设置环境变量，或在常量区 CONFIG_DSH_HOME 直接写死"
        )

    # 自己开发的插件地址
    dsh_home = Path(dsh_home_raw).resolve()

    # agent 工作目录（技能与产物都相对它)
    workspace = Path(
        _env("DASHBOARD_WORKSPACE_DIR", str(SERVICE_ROOT / "workspace"))
    ).resolve()
    workspace.mkdir(parents=True, exist_ok=True)  # agent 子进程 cwd 必须已存在（否则 Popen 报 WinError 267）

    # 插件配置
    patch_file = Path(
        _env("DASHBOARD_PATCH_FILE", str(SITE_DASHBOARD_DIR / "cordis.yml"))
    ).resolve()

    # 模型提供者
    provider = _env("DASHBOARD_PROVIDER", DEFAULT_PROVIDER)

    # 默认模型
    model = _env("DASHBOARD_MODEL", DEFAULT_MODEL)

    # 模型 key
    auth_key = _env("DASHBOARD_API_KEY") or None
    # 模型 key 只从环境变量读取，缺失时仅告警不阻断
    upstream_key = os.environ.get(UPSTREAM_API_KEY_ENV, "").strip()

    concurrency_lock = threading.Lock()

    if not patch_file.is_file():
        raise RuntimeError(f"cordis patch 不存在：{patch_file}")

    # 确保 settings.yaml 带上 GLM 路由配置
    ensure_settings(dsh_home)

    harness: Any | None = None

    def get_harness() -> Any:
        nonlocal harness
        if harness is None:
            harness = (harness_factory or default_harness_factory)()
        return harness

    def default_harness_factory() -> DeepSeekHarness:
        return DeepSeekHarness(
            dsh_home=str(dsh_home),
            cwd=str(workspace),
            profile="sdk",
            patches=(str(patch_file),),
            dsh_bin=_cfg("DASHBOARD_DSH_BIN", CONFIG_DASHBOARD_DSH_BIN) or None,
            provider=provider,
            model=model,
        )

    def reset_harness() -> None:
        nonlocal harness
        if harness is not None:
            try:
                harness.close()
            except Exception:
                log.debug("harness 关闭异常（忽略）", exc_info=True)
        harness = None

    def run_task(task_id: str, full_prompt: str, session_id: str) -> None:
        TASKS[task_id]["status"] = "running"
        TASKS[task_id]["started_at"] = _utcnow()

        with concurrency_lock:
            try:
                result = get_harness().run(full_prompt, session_id=session_id)
            except Exception as ex:
                reset_harness()
                log.exception("任务 %s 执行异常", task_id)  # 完整堆栈打服务终端
                TASKS[task_id].update(status="failed", error=str(ex))
                return

        if result.finish_reason == "error":
            log.error("任务 %s 以 error 结束：%s", task_id, result.final_response)
            TASKS[task_id].update(
                status="failed",
                error=f"agent 以 error 结束：{result.final_response[:500]}",
            )
            return

        TASKS[task_id].update(
            status="completed",
            final_response=result.final_response,
            finish_reason=result.finish_reason,
            artifacts=list_artifacts(workspace, task_id),
        )
        TASKS[task_id]["finished_at"] = _utcnow()

    app = FastAPI(title="OnlyEdu Agent API", version="0.0.1")

    # 认证
    def require_auth(
        x_api_key: str | None = Header(default=None, alias="X-Api-Key")
    ) -> None:
        if auth_key and x_api_key != auth_key:
            raise HTTPException(status_code=401, detail="无效的 X-Api-Key")

    # API 接口
    @app.post("/api/v1/tasks", status_code=201, dependencies=[Depends(require_auth)])
    def submit(body: TaskCreate) -> dict:
        if body.skill is not None:
            # 先校验名字格式，再校验技能已在工作区同步
            if SKILL_NAME_RE.fullmatch(body.skill) is None:
                raise HTTPException(
                    status_code=400, detail=f"技能名非法：{body.skill!r}"
                )
            if not (dsh_home / "skills" / body.skill / "SKILL.md").is_file():
                raise HTTPException(status_code=400, detail=f"技能不存在：{body.skill}")
        task_id = uuid.uuid4().hex
        output_dir = f"{OUTPUT_DIRNAME}/{task_id}"
        sections: list[str] = []
        if body.skill is not None:
            sections.append(SKILL_TASK_TEMPLATE.format(skill=body.skill))
        sections.append(body.prompt)
        sections.append(OUTPUT_RULE_TEMPLATE.format(output_dir=output_dir))
        full_prompt = "\n\n".join(sections)
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "skill": body.skill,
            "session_id": body.session_id or f"task-{uuid.uuid4().hex[:12]}",
            "created_at": _utcnow(),
            "started_at": None,
            "finished_at": None,
            "final_response": None,
            "finish_reason": None,
            "error": None,
            "artifacts": [],
        }
        threading.Thread(
            target=run_task,
            args=(task_id, full_prompt, TASKS[task_id]["session_id"]),
            daemon=True,
        ).start()
        return {
            "task_id": task_id,
            "status": "queued",
            "status_url": f"/api/v1/tasks/{task_id}",
        }

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(require_auth)])
    def status(task_id: str) -> dict:
        task = TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
        return task

    @app.get("/healthz")
    def health() -> dict:
        counts = {"queued": 0, "running": 0, "completed": 0, "failed": 0}
        for task in TASKS.values():
            counts[task["status"]] += 1
        return {"status": "ok", **counts}

    return app


# 确保 settings.yaml 带 llm-pi-ai 路由分节（GLM 生效前提），幂等
def ensure_settings(dsh_home: Path) -> None:
    settings_path = dsh_home / "settings.yaml"
    if settings_path.exists():
        header = re.compile(r"^llm-pi-ai:\s*$", re.M)
        if header.search(settings_path.read_text(encoding="utf-8")):
            return
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("a", encoding="utf-8") as stream:
        stream.write(PI_AI_SETTINGS_SECTION)
    log.info("已在 %s 追加 llm-pi-ai 路由配置", settings_path)


# 枚举 workspace/output/<task_id>/ 下的产物（workspace 相对 posix 路径）
def list_artifacts(workspace: Path, task_id: str) -> list[dict[str, Any]]:
    task_output = workspace / OUTPUT_DIRNAME / task_id
    if not task_output.is_dir():
        return []
    artifacts = []
    for path in sorted(task_output.rglob("*")):
        if path.is_file():
            artifacts.append(
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "size_bytes": path.stat().st_size,
                }
            )
    return artifacts


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        create_app,
        host=_env("DASHBOARD_HOST", DEFAULT_HOST),
        port=int(_env("DASHBOARD_PORT", str(DEFAULT_PORT))),
    )
