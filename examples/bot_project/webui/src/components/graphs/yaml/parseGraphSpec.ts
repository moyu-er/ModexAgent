/**
 * parseGraphSpec.ts — YAML → 结构化拓扑(graph PRD §9.2)。
 *
 * 输入是 GraphSpec YAML(后端 `src/modex_graph/spec.py` 的 GraphSpec/NodeSpec/
 * EdgeSpec 字段名),输出前端渲染用的 `ParsedGraphTopology`。
 *
 * 只做结构解析,不做拓扑校验(环、可达性、max_iterations 由后端负责)。
 * 结构错误(YAML 语法、缺 name、未知字段、未知 node_type 等)抛出带
 * 行号/路径信息的 `GraphSpecParseError`。
 *
 * `__start__`/`__end__` 是虚拟端点(只出现在 edges 里),解析时按 PRD
 * 附录 B 合成对应的 ParsedNode:`__start__` 置顶、`__end__` 置尾,且仅在
 * 被某条边引用时合成。
 */
import { isMap, isScalar, isSeq, LineCounter, parseDocument } from "yaml";
import type { Node as YamlNode, Pair, YAMLMap } from "yaml";

/** 引擎级虚拟端点(对应 modex_graph GraphNode 哨兵,不是真实节点)。 */
export const GRAPH_NODE_START = "__start__";
export const GRAPH_NODE_END = "__end__";

/** spec 中允许声明的功能节点类型(后端 NodeFactory 注册键)。 */
export const FUNCTIONAL_NODE_TYPES = [
  "agent",
  "function",
  "delay",
  "human_input",
  "graph",
] as const;

export type ParsedNodeType =
  | (typeof FUNCTIONAL_NODE_TYPES)[number]
  | typeof GRAPH_NODE_START
  | typeof GRAPH_NODE_END;

export interface ParsedNodeConfig {
  agent?: string;
  pool?: string;
}

export interface ParsedNode {
  name: string;
  nodeType: ParsedNodeType;
  config: ParsedNodeConfig;
  trigger?: string;
}

export interface ParsedEdge {
  source: string;
  target: string;
}

export interface ParsedGraphTopology {
  name: string;
  scheduler: "linear" | "parallel";
  defaultTrigger: "on_receive" | "on_all_preds";
  nodes: ParsedNode[];
  edges: ParsedEdge[];
  /** 图入口 — 固定为 "__start__"。 */
  entryNode: string;
}

/** 结构解析错误 — 携带出错的 YAML 行号(1-based)与 spec 内路径。 */
export class GraphSpecParseError extends Error {
  /** spec 内路径,如 "nodes[1].node_type";根级错误为空串。 */
  readonly path: string;
  readonly line: number | null;
  readonly column: number | null;

  constructor(
    message: string,
    path: string,
    line: number | null = null,
    column: number | null = null,
  ) {
    const loc = line === null ? "" : ` (line ${line}, column ${column ?? 1})`;
    super(path ? `${path}: ${message}${loc}` : `${message}${loc}`);
    this.name = "GraphSpecParseError";
    this.path = path;
    this.line = line;
    this.column = column;
  }
}

// GraphSpec 的合法字段(后端 GraphSpec,extra="forbid")。version/state_class/
// metadata/max_iterations 对拓扑渲染无意义,允许存在但不读取。
const TOP_LEVEL_KEYS = [
  "name",
  "version",
  "state_class",
  "scheduler",
  "max_iterations",
  "default_trigger",
  "metadata",
  "nodes",
  "edges",
] as const;

const NODE_KEYS = ["name", "node_type", "config", "trigger"] as const;
const EDGE_KEYS = ["source", "target"] as const;

const SCHEDULERS = ["linear", "parallel"] as const;
const TRIGGERS = ["on_receive", "on_all_preds"] as const;

type Ctx = { lineCounter: LineCounter };

function fail(ctx: Ctx, message: string, path: string, node?: YamlNode | null): never {
  const range = node?.range;
  if (range) {
    const pos = ctx.lineCounter.linePos(range[0]);
    throw new GraphSpecParseError(message, path, pos.line, pos.col);
  }
  throw new GraphSpecParseError(message, path);
}

function scalarString(node: unknown): string | null {
  return isScalar(node) && typeof node.value === "string" ? node.value : null;
}

function pairKey(pair: Pair): string | null {
  return scalarString(pair.key);
}

function getPair(map: YAMLMap, key: string): Pair | undefined {
  return map.items.find((p) => pairKey(p) === key);
}

function checkKeys(
  ctx: Ctx,
  map: YAMLMap,
  allowed: readonly string[],
  path: string,
): void {
  for (const pair of map.items) {
    const key = pairKey(pair);
    if (key === null) {
      fail(ctx, "mapping key must be a plain string", path, pair.key as YamlNode);
    }
    if (!allowed.includes(key)) {
      fail(ctx, `unknown field '${key}'`, path, pair.key as YamlNode);
    }
  }
}

function requiredString(
  ctx: Ctx,
  map: YAMLMap,
  key: string,
  path: string,
): string {
  const pair = getPair(map, key);
  const value = pair ? scalarString(pair.value) : null;
  if (value === null || value === "") {
    fail(ctx, `missing required field '${key}' (non-empty string)`, path, pair?.value as YamlNode | undefined ?? map);
  }
  return value;
}

function parseNode(ctx: Ctx, raw: unknown, index: number): ParsedNode {
  const path = `nodes[${index}]`;
  if (!isMap(raw)) {
    fail(ctx, "node entry must be a mapping", path, raw as YamlNode);
  }
  checkKeys(ctx, raw, NODE_KEYS, path);

  const name = requiredString(ctx, raw, "name", `${path}.name`);
  if (name === GRAPH_NODE_START || name === GRAPH_NODE_END) {
    fail(
      ctx,
      `'${name}' is a reserved virtual endpoint — it must not be declared in nodes`,
      `${path}.name`,
      getPair(raw, "name")?.value as YamlNode | undefined,
    );
  }

  const typePair = getPair(raw, "node_type");
  const nodeType = typePair ? scalarString(typePair.value) : null;
  if (nodeType === null) {
    fail(ctx, "missing required field 'node_type'", `${path}.node_type`, typePair?.value as YamlNode | undefined ?? raw);
  }
  if (!(FUNCTIONAL_NODE_TYPES as readonly string[]).includes(nodeType)) {
    fail(
      ctx,
      `unknown node_type '${nodeType}' (expected one of: ${FUNCTIONAL_NODE_TYPES.join(", ")})`,
      `${path}.node_type`,
      typePair?.value as YamlNode | undefined,
    );
  }

  const config: ParsedNodeConfig = {};
  const configPair = getPair(raw, "config");
  if (configPair !== undefined && configPair.value != null) {
    if (!isMap(configPair.value)) {
      fail(ctx, "'config' must be a mapping", `${path}.config`, configPair.value as YamlNode);
    }
    // config 是自由结构(后端 dict[str, Any]),只提取渲染用的 agent/pool。
    for (const key of ["agent", "pool"] as const) {
      const p = getPair(configPair.value, key);
      if (p === undefined || p.value == null) continue;
      const v = scalarString(p.value);
      if (v === null) {
        fail(ctx, `'config.${key}' must be a string`, `${path}.config.${key}`, p.value as YamlNode);
      }
      config[key] = v;
    }
  }

  const node: ParsedNode = { name, nodeType: nodeType as ParsedNodeType, config };
  const triggerPair = getPair(raw, "trigger");
  if (triggerPair !== undefined && triggerPair.value != null) {
    const trigger = scalarString(triggerPair.value);
    if (trigger === null) {
      fail(ctx, "'trigger' must be a string", `${path}.trigger`, triggerPair.value as YamlNode);
    }
    node.trigger = trigger;
  }
  return node;
}

function parseEdge(ctx: Ctx, raw: unknown, index: number): ParsedEdge {
  const path = `edges[${index}]`;
  if (!isMap(raw)) {
    fail(ctx, "edge entry must be a mapping", path, raw as YamlNode);
  }
  checkKeys(ctx, raw, EDGE_KEYS, path);
  return {
    source: requiredString(ctx, raw, "source", `${path}.source`),
    target: requiredString(ctx, raw, "target", `${path}.target`),
  };
}

/** 解析 GraphSpec YAML 文本为结构化拓扑。失败抛 `GraphSpecParseError`。 */
export function parseGraphSpecYaml(source: string): ParsedGraphTopology {
  const lineCounter = new LineCounter();
  const doc = parseDocument(source, { lineCounter });
  const ctx: Ctx = { lineCounter };

  const syntaxError = doc.errors[0];
  if (syntaxError) {
    const pos = syntaxError.linePos?.[0];
    throw new GraphSpecParseError(
      `invalid YAML: ${syntaxError.message.split("\n")[0] ?? syntaxError.message}`,
      "",
      pos?.line ?? null,
      pos?.col ?? null,
    );
  }

  const root = doc.contents;
  if (root == null) {
    throw new GraphSpecParseError("graph spec is empty", "");
  }
  if (!isMap(root)) {
    fail(ctx, "graph spec root must be a mapping", "", root);
  }
  checkKeys(ctx, root, TOP_LEVEL_KEYS, "");

  const name = requiredString(ctx, root, "name", "name");

  const schedulerPair = getPair(root, "scheduler");
  let scheduler: ParsedGraphTopology["scheduler"] = "linear";
  if (schedulerPair !== undefined && schedulerPair.value != null) {
    const v = scalarString(schedulerPair.value);
    if (v === null || !(SCHEDULERS as readonly string[]).includes(v)) {
      fail(
        ctx,
        `'scheduler' must be one of: ${SCHEDULERS.join(", ")}`,
        "scheduler",
        schedulerPair.value as YamlNode,
      );
    }
    scheduler = v as ParsedGraphTopology["scheduler"];
  }

  const triggerPair = getPair(root, "default_trigger");
  let defaultTrigger: ParsedGraphTopology["defaultTrigger"] = "on_all_preds";
  if (triggerPair !== undefined && triggerPair.value != null) {
    const v = scalarString(triggerPair.value);
    if (v === null || !(TRIGGERS as readonly string[]).includes(v)) {
      fail(
        ctx,
        `'default_trigger' must be one of: ${TRIGGERS.join(", ")}`,
        "default_trigger",
        triggerPair.value as YamlNode,
      );
    }
    defaultTrigger = v as ParsedGraphTopology["defaultTrigger"];
  }

  const nodesPair = getPair(root, "nodes");
  const declaredNodes: ParsedNode[] = [];
  if (nodesPair !== undefined && nodesPair.value != null) {
    if (!isSeq(nodesPair.value)) {
      fail(ctx, "'nodes' must be a sequence", "nodes", nodesPair.value as YamlNode);
    }
    nodesPair.value.items.forEach((item, i) => {
      declaredNodes.push(parseNode(ctx, item, i));
    });
  }

  const edgesPair = getPair(root, "edges");
  const edges: ParsedEdge[] = [];
  if (edgesPair !== undefined && edgesPair.value != null) {
    if (!isSeq(edgesPair.value)) {
      fail(ctx, "'edges' must be a sequence", "edges", edgesPair.value as YamlNode);
    }
    edgesPair.value.items.forEach((item, i) => {
      edges.push(parseEdge(ctx, item, i));
    });
  }

  // 合成虚拟端点节点(PRD 附录 B):__start__ 置顶、__end__ 置尾,
  // 仅在被 edges 引用时合成。
  const referenced = new Set<string>();
  for (const edge of edges) {
    referenced.add(edge.source);
    referenced.add(edge.target);
  }
  const nodes: ParsedNode[] = [];
  if (referenced.has(GRAPH_NODE_START)) {
    nodes.push({ name: GRAPH_NODE_START, nodeType: GRAPH_NODE_START, config: {} });
  }
  nodes.push(...declaredNodes);
  if (referenced.has(GRAPH_NODE_END)) {
    nodes.push({ name: GRAPH_NODE_END, nodeType: GRAPH_NODE_END, config: {} });
  }

  return { name, scheduler, defaultTrigger, nodes, edges, entryNode: GRAPH_NODE_START };
}
