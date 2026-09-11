---
name: onlyedu-dashboard
description: 生成自包含 HTML 数据看板报表。数据通过 onlyedu-site-dashboard 插件提供的 site_api_fetch 工具拉取。
disable-model-invocation: false
user-invocable: true
---

# OnlyEdu 数据看板生成规范

数据由 **onlyedu-site-dashboard** 插件支撑：它在会话启动时注入「可用站点与接口」目录，并注册 `site_api_fetch` 工具。本技能描述如何用该工具取数并产出看板。

## 数据获取（site_api_fetch）

1. **先读目录再调用**：站点 id、接口 id、参数名必须与注入的站点目录逐字一致；不得臆造接口或参数。参数值从用户口述的学员信息等上下文提取。
2. **按需多次调用**：不同 dataType 各调一次；没有批量接口，不要一次传多个学员的值。
3. **字段含义不明时先调 `agent-types`**（`POST /AgentData/Types`）：它返回全部 dataType 的说明与参数表，以此为准组织展示，不要猜测字段语义。
4. **参数约束**：遵守目录里的可选/必填与值约束（如 `studentName` 模糊查询至少 2 个字符；`student_classes` 的 `studentId` 必填）。缺必填或违反约束会拿到错误文本，按提示修正后重试。

## 响应契约（插件包装的 OnlyEdu AgentData）

- 信封固定为 `{ "status": 200, "msg": "成功", "response": ... }`：
  - `status === 200`：成功，业务数据在 `response`；
  - `status !== 200`：失败，`response` 为 `null`，原因在 `msg`——向用户说明原因，不要渲染空看板。
- 列表型查询的 `response` 形如 `{ dataType, total, data: [...], queryTime }`；`total` 有上限（如学员姓名模糊查询上限 20 条），看板需标注「共 N 条」与 `queryTime`。
- 响应文本超过 60,000 字符会被插件截断（尾部有「…（已截断）」标记）：此时结果可能不完整，改用更精确的条件（studentId / studentCode / mobilePhone）缩小范围重查，不要基于截断数据下结论。
- 手机号等敏感字段是脱敏的（如 `137****3041`），展示时保持原样，不要尝试还原。

## 看板产出

- **单文件自包含 HTML**：内联 `<style>` 与轻量图表（手写 SVG 或原生 Canvas）；禁止 CDN 等外部依赖，离线可打开。
- **布局**：顶部标题栏（站点名 + 数据时间 `queryTime` + 生成时间）→ 指标卡（KPI 摘要）→ 主体图表区 → 数据明细表。
- **可用性**：移动端单列可用；语义化配色；标签清晰；空数据/查询失败给出占位提示与原因。
- **写文件**：用 `write` 工具输出到会话工作区 `output/dashboard.html`，回复中给出相对路径。
- 多学员对比场景：先逐一查 `student_basic` 定位 `studentId`，再对每个 `studentId` 查 `student_classes`，在明细表中按学员分组展示。
