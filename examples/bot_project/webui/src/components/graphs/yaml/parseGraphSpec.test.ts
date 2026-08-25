/**
 * parseGraphSpec 单元测试(graph PRD §9.2 / tickets G01)。
 *
 * 前两个 fixture 是真实 spec 文件的内联拷贝:
 * - examples/bot_project/config/graphs/simple.yml
 * - examples/bot_project/tests/integration/fixtures/graphs/review_workflow.yml
 *   (PRD 附录 B 对应的 review_workflow;config/graphs/ 下目前只有 simple.yml)
 */
import { describe, expect, it } from "vitest";

import {
  GraphSpecParseError,
  parseGraphSpecYaml,
} from "./parseGraphSpec";

// examples/bot_project/config/graphs/simple.yml — 原样内联
const SIMPLE_YML = `name: simple
version: "1.0"
state_class: default
nodes: []
edges:
  - source: __start__
    target: __end__
`;

// examples/bot_project/tests/integration/fixtures/graphs/review_workflow.yml — 原样内联
const REVIEW_WORKFLOW_YML = `name: review_workflow
version: "1.0"
state_class: default
scheduler: parallel
max_iterations: 50
default_trigger: on_all_preds
metadata:
  description: >-
    Designer → Implementer → Reviewer workflow with conditional routing.
    Reviewer loops back to Implementer for revisions or delivers to END
    for approval. Uses parallel scheduling with ON_ALL_PREDS trigger.
nodes:
  - name: designer
    node_type: agent
    config:
      agent: designer
      pool: review
  - name: implementer
    node_type: agent
    config:
      agent: implementer
      pool: review
  - name: reviewer
    node_type: agent
    config:
      agent: reviewer
      pool: review
edges:
  - source: __start__
    target: designer
  - source: designer
    target: implementer
  - source: implementer
    target: reviewer
  - source: reviewer
    target: implementer
  - source: reviewer
    target: __end__
`;

describe("parseGraphSpecYaml", () => {
  it("parses simple.yml — 空 nodes + start→end 直连,补全虚拟端点", () => {
    const topo = parseGraphSpecYaml(SIMPLE_YML);

    expect(topo.name).toBe("simple");
    expect(topo.scheduler).toBe("linear"); // GraphSpec 默认值
    expect(topo.defaultTrigger).toBe("on_all_preds"); // GraphSpec 默认值
    expect(topo.entryNode).toBe("__start__");
    expect(topo.nodes).toEqual([
      { name: "__start__", nodeType: "__start__", config: {} },
      { name: "__end__", nodeType: "__end__", config: {} },
    ]);
    expect(topo.edges).toEqual([{ source: "__start__", target: "__end__" }]);
  });

  it("parses review_workflow.yml — 产出 PRD 附录 B 的结构(含回环边)", () => {
    const topo = parseGraphSpecYaml(REVIEW_WORKFLOW_YML);

    expect(topo.name).toBe("review_workflow");
    expect(topo.scheduler).toBe("parallel");
    expect(topo.defaultTrigger).toBe("on_all_preds");
    expect(topo.entryNode).toBe("__start__");

    // 节点顺序:__start__ 置顶、声明节点居中、__end__ 置尾
    expect(topo.nodes.map((n) => n.name)).toEqual([
      "__start__",
      "designer",
      "implementer",
      "reviewer",
      "__end__",
    ]);
    expect(topo.nodes.map((n) => n.nodeType)).toEqual([
      "__start__",
      "agent",
      "agent",
      "agent",
      "__end__",
    ]);
    expect(topo.nodes[1]).toEqual({
      name: "designer",
      nodeType: "agent",
      config: { agent: "designer", pool: "review" },
    });
    // 虚拟端点无 config/trigger
    expect(topo.nodes[0]).toEqual({ name: "__start__", nodeType: "__start__", config: {} });
    expect(topo.nodes[4]).toEqual({ name: "__end__", nodeType: "__end__", config: {} });

    expect(topo.edges).toEqual([
      { source: "__start__", target: "designer" },
      { source: "designer", target: "implementer" },
      { source: "implementer", target: "reviewer" },
      { source: "reviewer", target: "implementer" }, // 回环
      { source: "reviewer", target: "__end__" },
    ]);
  });

  it("未被引用的虚拟端点不合成", () => {
    const topo = parseGraphSpecYaml(
      `name: t\nnodes:\n  - name: a\n    node_type: function\nedges:\n  - source: __start__\n    target: a\n`,
    );
    expect(topo.nodes.map((n) => n.name)).toEqual(["__start__", "a"]);
  });

  it("非法 YAML 抛出带行号的 GraphSpecParseError", () => {
    let caught: unknown;
    try {
      parseGraphSpecYaml("name: [unclosed\n");
    } catch (err) {
      caught = err;
    }
    expect(caught).toBeInstanceOf(GraphSpecParseError);
    const err = caught as GraphSpecParseError;
    expect(err.line).not.toBeNull();
    expect(err.line).toBeGreaterThanOrEqual(1);
    expect(err.message).toContain("invalid YAML");
  });

  it("缺 name 抛出结构化错误(path=name)", () => {
    expect(() => parseGraphSpecYaml("nodes: []\n")).toThrowError(GraphSpecParseError);
    try {
      parseGraphSpecYaml("nodes: []\n");
    } catch (err) {
      expect((err as GraphSpecParseError).path).toBe("name");
    }
  });

  it("未知顶层字段抛错并带行号", () => {
    try {
      parseGraphSpecYaml("name: t\nbogus: 1\n");
      expect.unreachable();
    } catch (err) {
      const e = err as GraphSpecParseError;
      expect(e).toBeInstanceOf(GraphSpecParseError);
      expect(e.message).toContain("unknown field 'bogus'");
      expect(e.line).toBe(2);
    }
  });

  it("未知 node_type 抛错,path 指向对应节点字段", () => {
    const src = `name: t
nodes:
  - name: a
    node_type: foo
`;
    try {
      parseGraphSpecYaml(src);
      expect.unreachable();
    } catch (err) {
      const e = err as GraphSpecParseError;
      expect(e).toBeInstanceOf(GraphSpecParseError);
      expect(e.message).toContain("unknown node_type 'foo'");
      expect(e.path).toBe("nodes[0].node_type");
      expect(e.line).toBe(4);
    }
  });

  it("节点声明保留字端点名抛错", () => {
    const src = `name: t
nodes:
  - name: __start__
    node_type: agent
`;
    expect(() => parseGraphSpecYaml(src)).toThrowError(/reserved virtual endpoint/);
  });

  it("节点未知字段抛错", () => {
    const src = `name: t
nodes:
  - name: a
    node_type: agent
    bogus: 1
`;
    expect(() => parseGraphSpecYaml(src)).toThrowError(/unknown field 'bogus'/);
  });

  it("nodes[].trigger 为显式 null 时解析成功且节点无 trigger 字段(后端 _yaml() 对 trigger=None 输出 trigger: null)", () => {
    // bot/webui/routes/graph_routes.py `_yaml()` 的原样输出内联拷贝 ——
    // yaml.dump(GraphSpec(..., nodes=[NodeSpec(trigger=None)], ...).model_dump(mode="json"))。
    const BACKEND_TRIGGER_NULL_YML = `name: trigger_null
nodes:
- name: a
  node_type: agent
  config:
    agent: designer
    pool: review
  trigger: null
edges:
- source: __start__
  target: a
- source: a
  target: __end__
state_class: default
scheduler: linear
version: '1.0'
metadata: {}
max_iterations: 25
default_trigger: on_all_preds
`;

    const topo = parseGraphSpecYaml(BACKEND_TRIGGER_NULL_YML);

    // toEqual 精确匹配整个节点对象 —— 多出一个 trigger 字段即失败。
    const node = topo.nodes.find((n) => n.name === "a");
    expect(node).toEqual({
      name: "a",
      nodeType: "agent",
      config: { agent: "designer", pool: "review" },
    });
  });

  it("后端 _yaml() 往返输出含 state_schema: null 时解析成功(scope-converge GraphSpec 新增字段)", () => {
    // bot/webui/routes/graph_routes.py `_yaml()` 的原样输出内联拷贝 ——
    // yaml.dump(GraphSpec(..., state_schema=None, ...).model_dump(mode="json"))。
    // state_schema 对渲染无意义,允许存在但不读取(与 version/state_class 同款)。
    const BACKEND_STATE_SCHEMA_NULL_YML = `name: state_schema_null
nodes:
- name: a
  node_type: agent
  config:
    agent: designer
    pool: review
  trigger: null
edges:
- source: __start__
  target: a
- source: a
  target: __end__
state_class: default
state_schema: null
scheduler: linear
version: '1.0'
metadata: {}
max_iterations: 25
default_trigger: on_all_preds
`;

    const topo = parseGraphSpecYaml(BACKEND_STATE_SCHEMA_NULL_YML);

    expect(topo.name).toBe("state_schema_null");
    expect(topo.nodes.map((n) => n.name)).toEqual(["__start__", "a", "__end__"]);
  });

  it("声明式 state_schema(非 null)也允许存在且不被读取", () => {
    const src = `name: t
state_schema:
  research_notes:
    type: string
    initial: ""
  tool_results:
    type: list
    item_type: string
nodes:
  - name: a
    node_type: agent
edges:
  - source: __start__
    target: a
`;
    const topo = parseGraphSpecYaml(src);
    expect(topo.nodes.map((n) => n.name)).toEqual(["__start__", "a"]);
  });

  it.each([
    { label: "boolean", triggerYaml: "trigger: true" },
    { label: "number", triggerYaml: "trigger: 1" },
    { label: "mapping", triggerYaml: "trigger: {mode: on_receive}" },
  ])("非字符串 trigger($label)仍按非字符串抛错", ({ triggerYaml }) => {
    const src = `name: t
nodes:
  - name: a
    node_type: agent
    ${triggerYaml}
`;
    try {
      parseGraphSpecYaml(src);
      expect.unreachable();
    } catch (err) {
      const e = err as GraphSpecParseError;
      expect(e).toBeInstanceOf(GraphSpecParseError);
      expect(e.message).toContain("'trigger' must be a string");
      expect(e.path).toBe("nodes[0].trigger");
    }
  });

  it("edge 缺 target 抛错(path=edges[i].target)", () => {
    const src = `name: t
edges:
  - source: __start__
`;
    try {
      parseGraphSpecYaml(src);
      expect.unreachable();
    } catch (err) {
      const e = err as GraphSpecParseError;
      expect(e).toBeInstanceOf(GraphSpecParseError);
      expect(e.path).toBe("edges[0].target");
    }
  });

  it("非法 scheduler 值抛错", () => {
    expect(() => parseGraphSpecYaml("name: t\nscheduler: roundrobin\n")).toThrowError(
      /'scheduler' must be one of/,
    );
  });

  it("根节点不是 mapping 抛错", () => {
    expect(() => parseGraphSpecYaml("- just\n- a\n- list\n")).toThrowError(
      /root must be a mapping/,
    );
  });
});
