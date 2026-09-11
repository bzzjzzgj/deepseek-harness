/**
 * OnlyEdu 站点数据插件：会话启动时把配置的站点目录注入为 catalog 上下文，
 * 并注册 site_api_fetch 工具让模型按 (site, endpoint, params) 拉取带参数的接口数据。
 * 首个接入场景：根据用户口述的学员信息（姓名/手机号/学号）查询学员订单。
 * @module @deepseek-ai/dsh-onlyedu-site-dashboard
 */

import type { Context } from "@deepseek-ai/cordis";
import z from "@deepseek-ai/schemastery";
import { defineTool } from "@deepseek-ai/dsh-tools";
import { createUserMessage } from "@deepseek-ai/dsh-llm";

/** Cordis 插件名（loader 诊断与消息 source 溯源共用）。 */
export const name = "onlyedu-site-dashboard";

/** 工具注册需要 tools 服务就绪；session-start 是核心事件，无需额外注入。 */
export const inject = ["tools"];

/** 单次 site_api_fetch 返回给模型的最大字符数，防止响应撑爆上下文。 */
export const MAX_FETCH_RESPONSE_CHARS = 60_000;

/** 单次拉取的超时（毫秒）。 */
export const FETCH_TIMEOUT_MS = 15_000;

/** 参数允许的类型。 */
export type ParamValue = string | number | boolean;

/**
 * 一个接口参数：声明在配置里，模型只能填 name 对应值，位置/类型/枚举都由声明约束。
 * `value` 为固定值（如 tenantId）：不进工具参数、不进 catalog，模型管不到。
 */
export interface EndpointParam {
  /** 参数名（工具 params 的键 / 路径模板 {name} / query 或 body 字段名）。 */
  name: string;
  /** 参数位置：路径模板 / URL 查询串 / JSON 请求体。 */
  in: "path" | "query" | "body";
  /** 值类型，用于校验模型传参。缺省 string。 */
  type?: "string" | "number" | "boolean";
  /** 是否必填；必填缺失时返回错误文本让模型自纠。 */
  required?: boolean;
  /** 给模型的填值说明（catalog 里展示）。 */
  description?: string;
  /** 允许取值（模型只能填这些），缺省不限制。 */
  enum?: ParamValue[];
  /** 固定值：声明在配置，始终使用，不暴露给模型。 */
  value?: ParamValue;
  /** 模型不传时的默认值。 */
  default?: ParamValue;
}

/** 一个接口：路径 + 方法 + 可选参数声明。 */
export interface Endpoint {
  id: string;
  path: string;
  method?: "GET" | "POST";
  parameters?: EndpointParam[];
  /**
   * AgentData 分发查询的固定 dataType：设置后该接口的 POST body 会包成
   * `{ DataType: <此值>, Parameters: {...} }` 信封（OnlyEdu AgentDataController 契约），
   * dataType 为服务器分发键，固定不可被模型改动（与 value 参数同款信任边界）。
   */
  dataType?: string;
}

/** 站点配置：声明式、可信边界。headers 含敏感 token 时用 ${VAR} 占位，运行时从 env 替换。 */
export interface SiteConfig {
  /** 站点唯一 id（catalog 与工具参数都用它）。 */
  id: string;
  /** 站点显示名（catalog 里给人看）。 */
  name: string;
  /** 接口路径的基址，如 https://api.example.com/api/v1。 */
  baseUrl: string;
  /** 可选请求头；值支持 ${ENV_VAR} 占位符（从进程环境取值）。 */
  headers?: Record<string, string>;
  /** 该站点暴露给模型的接口清单。 */
  endpoints: Endpoint[];
}

/** 插件配置。 */
export interface Config {
  /** 站点清单（可多个）。 */
  sites: SiteConfig[];
}

const paramSchema = z.object({
  name: z.string(),
  in: z.union(["path", "query", "body"]),
  type: z.union(["string", "number", "boolean"]),
  required: z.boolean(),
  description: z.string(),
  enum: z.array(z.union([z.string(), z.number(), z.boolean()])),
  value: z.union([z.string(), z.number(), z.boolean()]),
  default: z.union([z.string(), z.number(), z.boolean()]),
});

/** Schemastery 校验配置。 */
export const Config: z<Config> = z.object({
  sites: z.array(
    z.object({
      id: z.string(),
      name: z.string(),
      baseUrl: z.string(),
      headers: z.dict(z.string()),
      endpoints: z.array(
        z.object({
          id: z.string(),
          path: z.string(),
          method: z.union(["GET", "POST"]),
          dataType: z.string(),
          parameters: z.array(paramSchema),
        }),
      ),
    }),
  ),
});

/** 解析 headers 里的 ${ENV_VAR} 占位符；缺失时省略该头并记 warn。 */
function resolveHeaders(
  raw: Record<string, string> | undefined,
  log: Context["logger"],
): Record<string, string> | undefined {
  /* v8 ignore next -- schemastery 把缺省 headers 归一为 {}，运行期 raw 恒非 undefined；守卫仅保护手构 SiteConfig。 */
  if (raw === undefined) return undefined;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(raw)) {
    const match = /^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$/.exec(v);
    const envName = match?.[1];
    if (envName !== undefined) {
      const env = process.env[envName];
      if (env === undefined) {
        log.warn(
          `${name}: header ${k} 引用缺失的环境变量 ${envName} —— 省略该头`,
        );
        continue;
      }
      out[k] = env;
    } else {
      out[k] = v;
    }
  }
  return out;
}

/** 校验一个参数值是否符合 type / enum 声明，返回错误文本或 undefined。 */
function validateParam(
  param: EndpointParam,
  value: ParamValue,
): string | undefined {
  // 如果这个参数被定义为数字类型，但实际传进来的值却不是数字，则校验失败
  if (param.type === "number" && typeof value !== "number")
    return `参数 ${param.name} 需要 number`;
  if (param.type === "boolean" && typeof value !== "boolean")
    return `参数 ${param.name} 需要 boolean`;
  if (param.type === "string" && typeof value !== "string")
    return `参数 ${param.name} 需要 string`;
  // schemastery 会把配置里缺失的 enum 数组字段默认成 []；空枚举视为"无限制"。
  if (
    param.enum !== undefined &&
    param.enum.length > 0 &&
    !param.enum.includes(value)
  ) {
    return `参数 ${param.name} 只能是 ${param.enum.join("|")}`;
  }
  return undefined;
}

/** 取一个参数的最终值：固定 value > 模型传参 > default；都没有且必填则报错。 */
function resolveParamValue(
  param: EndpointParam,
  provided: Record<string, ParamValue>,
): { value?: ParamValue; error?: string } {
  if (param.value !== undefined) return { value: param.value };
  const given = provided[param.name];
  if (given !== undefined) {
    const invalid = validateParam(param, given);
    return invalid === undefined ? { value: given } : { error: invalid };
  }
  if (param.default !== undefined) return { value: param.default };
  if (param.required === true) return { error: `缺少必填参数 ${param.name}` };
  return {};
}

/** 构造请求 URL 与 body：path 模板替换 + query 拼接 + body JSON。返回错误文本或 {url, body?}。 */
function buildRequest(
  endpoint: Endpoint,
  provided: Record<string, ParamValue>,
): { url: string; body?: string; error?: string } {
  let url = endpoint.path;
  const query = new URLSearchParams();
  const bodyParts: Record<string, ParamValue> = {};
  /* v8 ignore next -- schemastery 把缺省 parameters 归一为 []，运行期恒非 nullish；保编译类型。 */
  const endpointParameters = endpoint.parameters ?? [];
  for (const param of endpointParameters) {
    const { value, error } = resolveParamValue(param, provided);
    if (error !== undefined) return { url, error };
    if (value === undefined) continue;
    if (param.in === "path") {
      url = url.replace(`{${param.name}}`, encodeURIComponent(String(value)));
    } else if (param.in === "query") {
      query.set(param.name, String(value));
    } else {
      bodyParts[param.name] = value;
    }
  }
  if (query.size > 0) url = `${url}?${query.toString()}`;
  let body: string | undefined;
  if (endpoint.dataType !== undefined) {
    // AgentData 分发查询：把所有 body 参数包进 Parameters，外套 { DataType, Parameters } 信封（空参也发 {}）。
    body = JSON.stringify({
      DataType: endpoint.dataType,
      Parameters: { ...bodyParts },
    });
  } else {
    body =
      Object.keys(bodyParts).length > 0 ? JSON.stringify(bodyParts) : undefined;
  }
  return body === undefined ? { url } : { url, body };
}

/** 渲染站点目录（catalog 注入文本）：列出站点、接口及每个接口的参数说明。 */
export function renderSiteCatalog(sites: readonly SiteConfig[]): string {
  const lines = sites.map((site) => {
    const endpoints = site.endpoints
      .map((e) => {
        const params = (e.parameters ?? [])
          .filter((p) => p.value === undefined) // 固定值不暴露给模型
          .map(
            (p) =>
              `      - ${p.name}（${p.in}${p.type !== undefined ? `,${p.type}` : ""}${p.required === true ? ",必填" : ",可选"}）${p.description ?? ""}${p.enum !== undefined && p.enum.length > 0 ? `，只能 ${p.enum.join("|")}` : ""}`,
          )
          .join("\n");
        return `    - ${e.id}: ${e.method ?? "GET"} ${e.path}${e.dataType !== undefined ? `（dataType=${e.dataType} 分发查询）` : ""}${params.length > 0 ? `\n      参数：\n${params}` : ""}`;
      })
      .join("\n");
    return `  - ${site.id}（${site.name}）: ${site.baseUrl}\n${endpoints}`;
  });
  return `可用站点与接口：\n${lines.join("\n")}`;
}

/** 按配置拉一个接口，返回给模型的文本（截断 + 出错时返回结构化错误而非抛错）。 */
async function fetchEndpointText(
  site: SiteConfig,
  endpoint: Endpoint,
  provided: Record<string, ParamValue>,
  log: Context["logger"],
): Promise<string> {
  const built = buildRequest(endpoint, provided);
  if (built.error !== undefined)
    return `site_api_fetch: ${site.id}/${endpoint.id} — ${built.error}`;
  // 显式拼接：不能用 new URL(path, baseUrl)——path 的绝对路径语义会丢弃 baseUrl 的 path 段
  // （baseUrl 常带子前缀，如 /api 或 /TscOpen 网关），那样会 404。逐段去多余斜杠后合成为前缀。
  const base = site.baseUrl.replace(/\/+$/, "");
  const url = `${base}/${built.url.replace(/^\/+/, "")}`;
  const headers = resolveHeaders(site.headers, log);
  try {
    const res = await fetch(url, {
      method: endpoint.method ?? "GET",
      /* v8 ignore next -- schemastery 归一化保证 headers 恒为对象，此空分支仅防御类型上可能 undefined 的手构配置。 */
      ...(headers !== undefined ? { headers } : {}),
      ...(built.body !== undefined
        ? {
            body: built.body,
            headers: { ...headers, "content-type": "application/json" },
          }
        : {}),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    if (!res.ok)
      return `site_api_fetch: ${site.id}/${endpoint.id} → HTTP ${res.status} ${res.statusText}`;
    const text = await res.text();
    return text.length > MAX_FETCH_RESPONSE_CHARS
      ? text.slice(0, MAX_FETCH_RESPONSE_CHARS) + "\n…（已截断）"
      : text;
  } catch (error: unknown) {
    return `site_api_fetch: ${site.id}/${endpoint.id} 拉取失败：${String(error)}`;
  }
}

/** 把工具 args.params（JSON 值）收窄为参数表；非对象时返回 undefined（execute 会忽略）。 */
function parseParams(raw: unknown): Record<string, ParamValue> {
  /* v8 ignore next -- args.params 已通过工具 schema 的 object 校验（defineTool 先 validate 再 execute/presentCall），
     #      非对象 / null / 数组不可能到达这里。 */
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return {};
  const out: Record<string, ParamValue> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    )
      out[key] = value;
  }
  return out;
}

/** 站点清单按 id 索引，工具参数解析用。 */
function findSite(
  sites: readonly SiteConfig[],
  id: string,
): SiteConfig | undefined {
  return sites.find((s) => s.id === id);
}
function findEndpoint(site: SiteConfig, id: string): Endpoint | undefined {
  return site.endpoints.find((e) => e.id === id);
}

/**
 * 注册工具并注入站点目录。
 * 工具注册对 ctx 生命周期有效；站点目录在每次 session-start 注入一次。
 */
export function apply(ctx: Context, config: Config): void {
  const sites = config.sites;

  ctx.on("agent/session-start", ({ agent }) => {
    const text = renderSiteCatalog(sites);
    agent.inject(
      createUserMessage({
        content: [{ type: "text", text }],
        source: { kind: "plugin", plugin: name, form: "catalog" },
      }),
    );
  });

  const siteFetchTool = defineTool({
    name: "site_api_fetch",
    description:
      "按 (site, endpoint, params) 拉取配置站点的一个接口。站点与接口 id、参数名必须用注入的站点目录里的；参数值从用户口述的学员信息等上下文中提取。",
    parameters: {
      site: {
        type: "string",
        required: true,
        description: "站点 id（见注入的站点目录）。",
      },
      endpoint: {
        type: "string",
        required: true,
        description: "接口 id（见注入的站点目录）。",
      },
      params: {
        type: "object",
        additionalProperties: true,
        description: "接口参数，键为目录里声明的参数名。",
      },
    },
    output: {
      schema: {
        type: "object",
        additionalProperties: false,
        properties: {
          content: { type: "string", required: true },
        },
      },
      render: (_args, value) => [{ type: "text", text: value.content }],
    },
    async execute(args, _exec) {
      const site = findSite(sites, args.site);
      if (site === undefined)
        return { content: `site_api_fetch: 未知站点 "${args.site}"` };
      const endpoint = findEndpoint(site, args.endpoint);
      if (endpoint === undefined)
        return {
          content: `site_api_fetch: 站点 ${site.id} 无接口 "${args.endpoint}"`,
        };
      return {
        content: await fetchEndpointText(
          site,
          endpoint,
          parseParams(args.params),
          ctx.logger,
        ),
      };
    },
    presentCall(args) {
      const params = parseParams(args.params);
      const site = args.site;
      const endpoint = args.endpoint;
      const detail =
        Object.keys(params).length > 0
          ? `${site}/${endpoint} ${JSON.stringify(params)}`
          : `${site}/${endpoint}`;
      return {
        card: "generic",
        title: `Fetch ${detail}`,
        kind: "read",
        rawInput: detail,
      };
    },
  });
  ctx.tools.register(siteFetchTool);
}
