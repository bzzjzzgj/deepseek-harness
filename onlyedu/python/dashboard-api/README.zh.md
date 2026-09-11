# OnlyEdu Dashboard API

中文

驱动 DeepSeek Harness 指定技能的 HTTP 服务：外部调用方（.NET Hangfire）提交任务，服务以官方 Python SDK 启动 `dsh` 运行时，加载 `onlyedu-dashboard` 技能完成 agent 轮次并产出看板文件。单文件实现（`server.py`）。模型走智谱 GLM。

## 架构

```
Hangfire Job (.NET)
  │ ① POST /api/v1/tasks       → 201 {task_id}
  │ ② GET  /api/v1/tasks/{id}  → 轮询至 completed | failed
  ▼
FastAPI（内存任务表）→ DeepSeekHarness 单例（串行执行，异常自动重建）
  → provider=zai（智谱 GLM）+ onlyedu-dashboard 技能 → site_api_fetch → OnlyEdu AgentData
  → workspace/output/<task_id>/dashboard.html
```

- **模型**：pi-ai 多提供方适配器的 `zai` 路由（端点 `https://api.z.ai/api/coding/paas/v4`、OpenAI 兼容、模型目录由 pi-ai 内置目录提供）。路由在 `$DSH_HOME/settings.yaml` 的 `llm-pi-ai:` 分节声明，服务启动时缺失则自动补写；凭据读环境变量 `ZAI_API_KEY`。
- **技能**：提示词指示加载（SDK 不解析 slash 命令）：组装后的提示词为「请使用 `<skill>` 技能完成以下任务：<prompt> + 产物目录约定」。技能需**预先安装**到 harness 自动扫描根 `<dsh_home>/skills/<技能名>/`（服务不负责拷贝；提交时校验存在，缺失 → `400`）。

## 配置

### 常量区（server.py 顶部）

启动前在 `server.py` 顶部「常量区」确认以下三项，写死值优先于环境变量，留空则回退读环境变量：

| 常量 | 当前值 | 说明 |
|---|---|---|
| `CONFIG_DSH_HOME` | 留空（口径：harness 根一律走环境变量 `DSH_HOME`，勿写死路径） | harness 配置与状态存储（settings.yaml、会话数据）；`skills/` 子目录 = 技能自动扫描根 |
| `CONFIG_DASHBOARD_DSH_BIN` | 留空 | runtime exe 已构建，SDK 自动发现，无需指定 |
| `ZAI_API_KEY` | **只能走环境变量，不可写死** | 智谱凭据，子进程自动继承 |

### 环境变量

| 变量 | 必填 | 缺省 | 说明 |
|---|---|---|---|
| `ZAI_API_KEY` | ✅ | — | 智谱凭据（**必须环境变量**；缺失时启动仅告警，任务期以 `MISSING_CREDENTIAL` 失败） |
| `DSH_HOME` | ✅（宿主直跑） | — | harness 持久化根（`CONFIG_DSH_HOME` 已留空，此变量为唯一来源；容器由镜像 ENV 设 `/data/dsh-home`） |
| `DASHBOARD_PROVIDER` | | `zai` | 提供方路由（如换回 `deepseek-official`） |
| `DASHBOARD_MODEL` | | `glm-5.3-flash` | 模型 id（目录内还有 glm-5.2 / glm-4.7 等） |
| `DASHBOARD_WORKSPACE_DIR` | | `<服务目录>/workspace` | agent 工作区（技能与产物都相对它） |
| `DASHBOARD_PATCH_FILE` | | `../plugins/site-dashboard/cordis.yml` | 插件 patch（site_api_fetch 工具） |
| `DASHBOARD_DSH_BIN` | | 留空 | 仅源码调试模式：指向 `apps/cli/lib/bin.js`（需先 `pnpm build`） |
| `DASHBOARD_API_KEY` | | — | 服务鉴权 key（空 = 不鉴权） |
| `DASHBOARD_HOST` / `DASHBOARD_PORT` | | `127.0.0.1` / `8100` | 监听地址 |

提示词模板、产物目录名、模型缺省值等常量集中在 `server.py` 顶部「常量区」，后期修改不翻代码。

## 启动

```powershell
cd D:\Workspace\AngLi\Agent\deepseek-harness\onlyedu\python\dashboard-api

# 0) 设置 harness 持久化根（宿主直跑必设；容器由镜像 ENV 提供）
$env:DSH_HOME = "D:/Workspace/AngLi/Agent/deepseek-harness-py"

#    安装技能（一次性：拷到 harness 自动扫描根 $DSH_HOME\skills\，目录名=技能名）
Copy-Item -Recurse -Force ..\..\plugins\site-dashboard "$env:DSH_HOME\skills\onlyedu-dashboard"

# 1) 创建虚拟环境并安装依赖（uv 自动在当前目录创建 .venv；SDK 解析到仓库内 python/sdk，runtime exe 已构建并同步至 python/sdk-runtime）
uv sync

#    注：uv 创建的 .venv 不带激活脚本（Activate.ps1 不存在，属正常现象）；如需在终端直接用 python：
.\.venv\Scripts\python.exe .\server.py

# 2) 设置模型 key（DSH_HOME 见步骤 0）
$env:ZAI_API_KEY = "你的智谱 key"

# 3) 启动
uv run uvicorn server:create_app --factory --host 127.0.0.1 --port 8100
#    等价写法：
#    .\.venv\Scripts\python.exe .\server.py
```

> ⚠️ 不要用其他环境（conda / 全局 Python）直接 `python .\server.py`——依赖只装在本项目 `.venv`，会报 `ModuleNotFoundError: No module named 'fastapi'`。`uv run` 永远安全。

启动成功标志：日志出现 `Uvicorn running on http://127.0.0.1:8100`，`GET /healthz` 返回 `{"status": "ok", ...}`。

`python server.py` 直跑等价（监听地址/端口读 `DASHBOARD_HOST` / `DASHBOARD_PORT`）。

### 运行时 exe（一般无需操作）

`dsh` 运行时可执行文件已构建并同步至 `python/sdk-runtime/src/deepseek_harness_runtime/runtime/`，SDK（editable 安装）自动发现。仅当 harness TS 源码变更后需要重建：

```powershell
cd D:\Workspace\AngLi\Agent\deepseek-harness
$env:npm_execpath = "C:\Program Files\nodejs\node_modules\pnpm\bin\pnpm.mjs"   # 本机 standalone pnpm 需要，会话级
pnpm exec tsx scripts/build-exe-for-python-sdk.ts
```

## Docker 部署

双 Dockerfile 依赖固化：`Dockerfile.deps` 构建 `onlyedu-dashboard-api-deps`（apt 编译工具 + pnpm/node_modules + python3.12/uv + .venv 一次固化），主 `Dockerfile` 的 builder/runtime 均基于它——日常改业务代码只耗 pnpm 编译，零依赖安装。**构建上下文必须是仓库根**：

```powershell
cd D:\Workspace\AngLi\Agent\deepseek-harness

# ① 依赖镜像（首次 / 依赖变更后）
docker build -f onlyedu\python\dashboard-api\Dockerfile.deps -t onlyedu-dashboard-api-deps .

# ② 业务镜像（日常迭代只跑这条，只耗 pnpm 编译 + 产物拷贝）
docker build -f onlyedu\python\dashboard-api\Dockerfile -t onlyedu-dashboard-api .

# ③ 运行（ZAI_API_KEY 必经环境变量注入；DSH_HOME 由镜像 ENV 设 /data/dsh-home）
docker run -d --name dashboard-api -p 8100:8100 -e ZAI_API_KEY=<你的智谱key> -v onlyedu-dsh:/data onlyedu-dashboard-api
```

- 镜像内链路：deps 镜像装好依赖 → 容器内构建 **Linux 版** runtime exe（宿主产物 win-x64 不可用）→ 启动时把技能安装到 `/data/dsh-home/skills/`。
- `server.py` 常量区 `CONFIG_DSH_HOME` 保持留空（路径一律走环境变量），`CONFIG_DASHBOARD_DSH_BIN` 保持留空。
- 持久化：`/data` 卷保存 harness 状态与产物（`dsh-home`、`workspace`）；不挂卷则容器重建后状态丢失。
- 完整构建步骤与排障记录（代理/镜像源/编译工具链/磁盘维护）见 [DOCKER.zh.md](DOCKER.zh.md)。

## 使用

### `POST /api/v1/tasks` — 提交任务

```json
{ "prompt": "查询学员张三的报读班级并生成看板", "skill": "onlyedu-dashboard" }
```

→ `201 {task_id, status: "queued", status_url}`；技能不存在/非法 → `400`。`skill` 可省略（纯任务执行）。可选 `session_id` 复用会话上下文。

### `GET /api/v1/tasks/{task_id}` — 轮询状态

任务视图：`status` ∈ `queued | running | completed | failed`；完成时含 `final_response`、`artifacts`（`workspace` 相对 posix 路径 + `size_bytes` 清单）；失败时含 `error`。不存在 → `404`。

产物实体直接在工作区目录取：`<DASHBOARD_WORKSPACE_DIR>/output/<task_id>/`。

### `GET /healthz` — 健康检查

豁免鉴权，返回 `{"status": "ok", "queued": n, "running": n, "completed": n, "failed": n}`。

### Hangfire 侧对接（C#）

```csharp
// 一次 Job = 提交 + 轮询（模式同通用异步导出框架）
var created = await http.PostAsJsonAsync("/api/v1/tasks", new { prompt = "...", skill = "onlyedu-dashboard" });
var task = await created.Content.ReadFromJsonAsync<TaskCreated>();
while (true)
{
    var view = await http.GetFromJsonAsync<TaskView>($"/api/v1/tasks/{task.TaskId}");
    if (view.Status is "completed" or "failed") break;
    await Task.Delay(TimeSpan.FromSeconds(5));
}
// completed 后按 artifacts 清单（workspace 相对路径）取产物文件，推企微 / 上传 OBS
```

## 测试

```sh
uv run --group test pytest   # 7 个测试：FakeHarness 注入，不触真实 harness
```

## 已知边界

- 任务状态存内存：重启丢失（轮询 404），Hangfire 重试兜底。
- agent 轮次无超时保护，卡死需重启服务；并发 > 1 需评估 SDK 线程安全性（当前串行执行）。
- `ZAI_API_KEY` 未设置时启动仅警告，任务运行到凭据解析才失败（`MISSING_CREDENTIAL`）。
- 本机调试若环境有 HTTP 代理，127.0.0.1 会被 502 拦截：调用方需绕过（httpx `trust_env=False`、curl `--noproxy "*"`、C# `UseProxy=false`）。
