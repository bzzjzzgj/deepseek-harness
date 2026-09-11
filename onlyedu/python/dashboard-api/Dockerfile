# OnlyEdu Dashboard API 容器镜像（业务编译 + 运行时）。
# 依赖（apt 编译工具 / pnpm / node_modules / python3.12+uv / .venv）全部固化在
# onlyedu-dashboard-api-deps 镜像，定义见同目录 Dockerfile.deps。
#
# 构建上下文必须是仓库根（依赖 python/sdk、python/sdk-runtime、onlyedu/plugins）：
#   1) 先固化依赖镜像（一次性；此后仅当依赖变更时重建）：
#        docker build -f onlyedu/python/dashboard-api/Dockerfile.deps -t onlyedu-dashboard-api-deps .
#      重建触发条件 = pnpm-lock.yaml / uv.lock / pyproject.toml / 依赖包列表 任一变化。
#   2) 再编译业务源码（默认 target；只改业务代码时，每次仅耗 pnpm 编译 + 产物拷贝）：
#        docker build -f onlyedu/python/dashboard-api/Dockerfile -t onlyedu-dashboard-api .
#
# 依赖固化原理：deps 镜像里 pnpm install 和 uv sync 各跑一次并固化成本地 tag；
# 本文件 builder/runtime 都 FROM onlyedu-dashboard-api-deps，不再安装任何依赖——
# COPY . . 覆盖的只是源码，pnpm build 直接消费既有 node_modules。手动改过依赖
# （lockfile / pyproject）后必须先重建 deps 镜像，否则 build 用的是旧 node_modules。
#
# 运行（ZAI_API_KEY 必须经环境变量注入，镜像内不落盘）：
#   docker run -d -p 8100:8100 -e ZAI_API_KEY=<你的智谱key> -v onlyedu-dsh:/data --name dashboard-api onlyedu-dashboard-api
#
# ⚠️ DSH_HOME 一律走环境变量：容器由本镜像 ENV 设 /data/dsh-home，宿主直跑自行设置；
#    server.py 常量区 CONFIG_DSH_HOME 保持留空，不要写死宿主 Windows 路径。

# ── 源码编译层（只改业务源码时，此层每次重跑 = pnpm 编译耗时）───────────────────
FROM onlyedu-dashboard-api-deps AS builder
WORKDIR /repo
COPY . .
# build.native-system：flock glibc addon 用 cc、landlock-run 静态 musl 与 flock musl
# addon 用 musl-gcc——deps 镜像已备好 gcc/musl-tools。COPY . . 会带回宿主带 postinstall
# 的 package.json，但此层只跑 build（不触发 install hooks），无需再删。
# pkg-cache 挂 cache mount：pkg 打 SEA exe 要下载目标平台官方 node 二进制做注入底座，
# 源码变更使本层失效时缓存仍命中，免每次重下。
RUN --mount=type=cache,target=/root/.pkg-cache \
    pnpm build \
    && pnpm exec tsx scripts/build-exe-for-python-sdk.ts

# ── 运行时层（仅覆盖业务产物，其余 /repo 内容与 deps 镜像快照一致）───────────────
FROM onlyedu-dashboard-api-deps
WORKDIR /repo
# 精确 COPY 而非整目录：python/sdk-runtime 与 onlyedu/plugins/site-dashboard 是 pnpm
# workspace 包，deps 快照里它们的 node_modules 内嵌工作区符号链接（@deepseek-ai/* →
# store），整目录 COPY 覆盖会撞 "cannot copy to non-directory"。运行时不消费这两个
# node_modules（SEA exe 自含 node 运行时）。
# SEA 可执行文件与 2.5w 文件 node closure 由 builder 写入 src/deepseek_harness_runtime/runtime/
COPY --from=builder /repo/python/sdk /repo/python/sdk
COPY --from=builder /repo/python/sdk-runtime/src /repo/python/sdk-runtime/src
COPY --from=builder /repo/python/sdk-runtime/pyproject.toml /repo/python/sdk-runtime/pyproject.toml
COPY --from=builder /repo/onlyedu/python/dashboard-api /repo/onlyedu/python/dashboard-api
# cordis patch（site_api_fetch 工具）与技能源；缺省路径 SITE_DASHBOARD_DIR 直接命中
COPY --from=builder /repo/onlyedu/plugins/site-dashboard/src /repo/onlyedu/plugins/site-dashboard/src
COPY --from=builder /repo/onlyedu/plugins/site-dashboard/SKILL.md /repo/onlyedu/plugins/site-dashboard/SKILL.md
COPY --from=builder /repo/onlyedu/plugins/site-dashboard/cordis.yml /repo/onlyedu/plugins/site-dashboard/cordis.yml
COPY --from=builder /repo/onlyedu/plugins/site-dashboard/package.json /repo/onlyedu/plugins/site-dashboard/package.json

ENV PYTHONUNBUFFERED=1 \
    DSH_HOME=/data/dsh-home \
    DASHBOARD_WORKSPACE_DIR=/data/workspace \
    DASHBOARD_HOST=0.0.0.0
VOLUME /data
EXPOSE 8100

# 启动前把技能安装进 harness 自动扫描根 <dsh_home>/skills/（目录名=技能名）
CMD ["sh", "-c", "mkdir -p \"$DSH_HOME/skills/onlyedu-dashboard\" && cp -r /repo/onlyedu/plugins/site-dashboard/. \"$DSH_HOME/skills/onlyedu-dashboard/\" && cd /repo/onlyedu/python/dashboard-api && exec .venv/bin/python server.py"]