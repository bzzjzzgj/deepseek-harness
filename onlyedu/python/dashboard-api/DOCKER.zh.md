# Dashboard API Docker 构建指南

容器化 `server.py`（FastAPI 驱动 dsh 技能）的双 Dockerfile 依赖固化方案与排障记录。
构建上下文一律为**仓库根**（依赖 `python/sdk`、`python/sdk-runtime`、`onlyedu/plugins`）。

## 架构

```
Dockerfile.deps ──构建──▶ onlyedu-dashboard-api-deps（本地 tag，依赖全部固化）
                            │  apt 编译工具 + pnpm/node_modules + python3.12+uv + .venv
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
Dockerfile [builder]              Dockerfile [runtime]
  COPY . . + pnpm build             FROM deps 镜像，仅精确 COPY 业务产物
  + SEA 打包（Linux exe）
```

- **deps 镜像**：node_modules 与 .venv 一次性装好并固化。改业务源码**不**触发重建。
- **builder**：`pnpm build`（含 build:native-system 的 cc/musl-gcc 编译）+ `pnpm exec tsx scripts/build-exe-for-python-sdk.ts`（SEA 产出 Linux exe + 2.5w 文件 node closure，写入 `python/sdk-runtime/src/deepseek_harness_runtime/runtime/`）。
- **runtime**：同源自 deps 镜像，保证 `.venv` 符号链接有效；只覆盖业务产物。

## 构建步骤

### 1. 首次构建 / 依赖变更后（重建 deps 镜像）

```powershell
docker build -f onlyedu\python\dashboard-api\Dockerfile.deps -t onlyedu-dashboard-api-deps .
```

**重建触发条件**（任一变化）：`pnpm-lock.yaml`、`uv.lock`、`pyproject.toml`（deps 部分）、workspace 包的 `package.json` 依赖声明、Dockerfile.deps 的 apt 包清单。
⚠️ 手动改过依赖后**必须先重建 deps 再构建主镜像**，否则 builder 用的是旧 node_modules。

### 2. 日常迭代（只改业务代码 / server.py / 插件源码）

```powershell
docker build -f onlyedu\python\dashboard-api\Dockerfile -t onlyedu-dashboard-api .
```

只耗 pnpm 编译 + 产物拷贝，无依赖安装耗时。deps 镜像 tag 必须已存在（否则报 `pull access denied`）。

### 3. 运行

```powershell
docker run -d -p 8100:8100 -e ZAI_API_KEY=<你的智谱key> -v onlyedu-dsh:/data --name dashboard-api onlyedu-dashboard-api
```

- `ZAI_API_KEY` 只经环境变量注入，镜像内不落盘。
- `DSH_HOME` 一律走环境变量：容器由镜像 ENV 设 `/data/dsh-home`；**宿主直跑需自设**（会话级 `$env:DSH_HOME="D:/Workspace/AngLi/Agent/deepseek-harness-py"`，或写进系统环境变量）。`server.py` 常量区 `CONFIG_DSH_HOME` 保持留空，不要写死路径。

## 排障记录（按时间序，全部实测踩坑）

### 1. 拉镜像失败：`dial tcp ...:7897 connectex: No connection could be made`

Docker Desktop → Settings → Resources → Proxies 配了容器代理 `http://host.docker.internal:7897`。容器经**宿主网卡 IP**（非 127.0.0.1）访问代理，因此：

- 代理软件必须运行；
- 代理软件必须开 **Allow LAN（允许局域网连接）**，否则只监听 127.0.0.1 必被拒。

验证：`netstat -ano | findstr :7897`，应见 `0.0.0.0:7897`。

### 2. 基础镜像固化到本地

代理通了之后先 `docker pull` 把基础镜像拉到本地缓存，之后构建不再依赖网络：

```powershell
docker pull ghcr.io/astral-sh/uv:python3.12-bookworm-slim
docker pull node:24-bookworm-slim
# 可选留档：docker save -o xxx.tar <镜像> / docker load -i xxx.tar
```

日常构建命令**不要带 `--pull`**（会强制联网检查更新，代理挂了又卡 `load metadata`）。

### 3. apt 极慢（208s+）

Dockerfile.deps 已把源换成阿里云（bookworm 的 deb822 文件 `/etc/apt/sources.list.d/debian.sources`，一条 sed 连 security 源一并替换）。`libasan8`/`libgomp1` 等库是 gcc 硬依赖，装编译器必然带上，无法精简。

### 4. `fatal error: stdint.h: No such file or directory`

精简编译器时踩的坑：**gcc 不依赖 libc6-dev**（只依赖 libc6 运行时），而 gcc 内部 `#include_next <stdint.h>` 需要 libc 开发头文件。原 `build-essential` 元包是显式带上 libc6-dev 的。apt 行必须含 `libc6-dev`：

```
apt-get install -y --no-install-recommends git gcc musl-tools libc6-dev
```

`musl-tools` 同理必带：`build:native-system` 完整构建（`pnpm build` 调用）要编 `landlock-run`（静态 musl）和 musl 版 flock，用 `musl-gcc`。

### 5. `cannot copy to non-directory: .../node_modules/@deepseek-ai/dsh`

runtime 层**不能整目录 COPY** `python/sdk-runtime`、`onlyedu/plugins/site-dashboard`——它们是 pnpm workspace 包，deps 快照里其 `node_modules` 内嵌工作区符号链接，与 COPY 覆盖冲突。解法：精确 COPY（src、pyproject.toml、SKILL.md、cordis.yml、package.json），跳过 node_modules（SEA exe 自含 node 运行时，运行时不消费它）。`python/sdk` 不是 pnpm workspace 包，可整目录 COPY。

### 6. `Read-only file system` / `write metadata_v2.db: read-only file system`

**Docker 虚拟磁盘写满**，overlayfs 被强制只读——表现为任意写操作报 EROFS，且层缓存无法 commit（连带 builder 全量重跑）。SEA 打包 + node closure 的层缓存非常大（本次 14.4GB）。

排查与清理：

```powershell
docker system df            # 看 Build Cache 占用
docker builder prune -af    # 清构建缓存（大头）
docker image prune -f       # 清悬空镜像
```

清理后**必须重建 deps 镜像**（构建缓存被清了），再走主构建。
若 C 盘空间紧张，可在 Docker Desktop → Settings → Resources → Advanced 迁移 Disk image location。

### 7. 其他已知前提（勿破坏）

- `.dockerignore` 排除宿主 `**/node_modules`、`**/.venv`（Windows 产物进镜像会覆盖 Linux 版本）和 `python/sdk-runtime/src/deepseek_harness_runtime/runtime/`（win 版产物；Linux 版由容器内构建产出）。
- `.git` 必须保留在构建上下文（构建脚本跑 `git rev-parse` 注入版本元信息）。
- deps 镜像里删掉了根 `package.json` 的 `postinstall`（lefthook 的所有权 marker 绑定宿主 `.git`，容器内校验必失败）；builder 层 `COPY . .` 会带回宿主版 package.json，但该层只跑 build，不触发 install hooks，无需再删。
- npm 走 `npm_config_registry=https://registry.npmmirror.com`（deps 镜像 ENV，lockfile integrity 校验不受影响）。
