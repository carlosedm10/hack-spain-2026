import { describe, expect, test } from "bun:test";
import type { PendingAction } from "@/dashboard/demo";
import { layoutGraph } from "@/dashboard/graph-layout";
import { fromSnapshot, type GraphNode } from "@/graph/protocol";

function node(
  id: string,
  runId: string | null,
  neighbors: string[] = [],
  level = 0,
): GraphNode {
  return {
    id,
    run_id: runId,
    run_ids: runId ? [runId] : [],
    visit_count: 1,
    neighbors,
    level,
    threshold: 0.99,
    intent: null,
    event:
      id === "root" || id.startsWith("run:")
        ? null
        : {
            label: id,
            tool: "shell",
            summary: "An action",
            event: "tool_call",
          },
    action_id: null,
    created_at: null,
  };
}

const pending: PendingAction = {
  id: "atlas:2",
  run_id: "atlas",
  parentId: "atlas:1",
  label: "Read credentials",
  tool: "shell",
  created_at: "2026-01-01T00:00:00Z",
};
const snapshot = (nodes: GraphNode[]) => {
  const graph = fromSnapshot({ revision: 1, root: nodes.length ? "root" : null, nodes });
  // Wire undirected edges so the test fixtures match the backend's mutual neighbor lists.
  for (const node of graph.nodes.values()) {
    for (const neighborId of node.neighbors) {
      const neighbor = graph.nodes.get(neighborId);
      if (neighbor && !neighbor.neighbors.includes(node.id)) {
        neighbor.neighbors.push(node.id);
      }
    }
  }
  return graph;
};
const initial = () =>
  snapshot([
    node("root", null, ["run:atlas", "run:scout"]),
    node("run:atlas", "atlas", ["root", "atlas:1"]),
    node("atlas:1", "atlas", ["run:atlas"]),
    node("run:scout", "scout", ["root", "scout:1"]),
    node("scout:1", "scout", ["run:scout"]),
  ]);

describe("activity graph layout", () => {
  test("deduplicates mutual links and handles cycles, missing neighbors and self-links", () => {
    const graph = snapshot([
      node("root", null, ["run:atlas", "atlas:1", "missing", "root"]),
      node("run:atlas", "atlas", ["root", "atlas:1", "atlas:1"]),
      node("atlas:1", "atlas", ["root", "run:atlas"]),
    ]);
    const result = layoutGraph(graph, null);
    expect(result.nodes).toHaveLength(3);
    expect(result.links).toHaveLength(3);
    expect(new Set(result.links.map((link) => link.id)).size).toBe(3);
    expect(
      result.links.every(
        (link) => graph.nodes.has(link.source) && graph.nodes.has(link.target),
      ),
    ).toBe(true);
    expect(layoutGraph(graph, null)).toEqual(result);
  });

  test("places nodes by graph depth and keeps existing positions stable as actions and runs arrive", () => {
    const graph = initial();
    const before = layoutGraph(graph, null);
    const atlas = before.nodes.find((item) => item.id === "atlas:1")!;
    const scout = before.nodes.find((item) => item.id === "scout:1")!;
    expect(atlas.position.x).toBe(scout.position.x);
    expect(atlas.position.y).not.toBe(scout.position.y);
    graph.nodes.set(
      "atlas:2",
      node("atlas:2", "atlas", ["atlas:1", "scout:1"]),
    );
    graph.nodes.set("run:third", node("run:third", "third", ["root"]));
    const after = layoutGraph(graph, null);
    for (const item of before.nodes) {
      expect(after.nodes.find((next) => next.id === item.id)?.position).toEqual(
        item.position,
      );
    }
    const atlas2 = after.nodes.find((item) => item.id === "atlas:2")!;
    expect(atlas2.position.x).toBeGreaterThan(atlas.position.x);
  });

  test("pending and structural nodes carry no verdict; confidence does not determine severity", () => {
    const graph = initial();
    const before = Array.from(graph.nodes.entries());
    const result = layoutGraph(graph, pending);
    const provisional = result.nodes.find((item) => item.id === pending.id)!;
    expect(provisional.kind).toBe("pending");
    expect(provisional).not.toHaveProperty("level");
    expect(provisional).not.toHaveProperty("confidence");
    expect(result.nodes.find((item) => item.id === "root")).not.toHaveProperty(
      "level",
    );
    expect(result.nodes.find((item) => item.id === "run:atlas")?.kind).toBe(
      "structure",
    );
    expect(result.nodes.find((item) => item.id === "atlas:1")).toMatchObject({
      kind: "classified",
      level: 0,
      confidence: 0.99,
    });
    expect(result.links.filter((link) => link.pending)).toHaveLength(1);
    expect(Array.from(graph.nodes.entries())).toEqual(before);

    graph.nodes.set(pending.id, {
      ...node(pending.id, "atlas", [pending.parentId], 3),
      threshold: 0.35,
    });
    graph.nodes.get(pending.parentId)!.neighbors.push(pending.id);
    const classified = layoutGraph(graph, pending);
    expect(
      classified.nodes.filter((item) => item.id === pending.id),
    ).toHaveLength(1);
    expect(
      classified.nodes.find((item) => item.id === pending.id),
    ).toMatchObject({
      kind: "classified",
      level: 3,
      confidence: 0.35,
      position: provisional.position,
    });
    expect(classified.links.some((link) => link.pending)).toBe(false);
  });

  test("labels real monitor events by kind and tool when mock label is absent", () => {
    const graph = snapshot([
      node("root", null, ["run:demo"]),
      node("run:demo", "demo", ["root", "demo:1"]),
      {
        ...node("demo:1", "demo", ["run:demo"], 1),
        event: {
          kind: "network_request",
          tool: "http_request",
          target: "https://x",
        },
      },
    ]);
    const item = layoutGraph(graph, null).nodes.find(
      (entry) => entry.id === "demo:1",
    )!;
    expect(item.label).toBe("network_request");
    expect(item.tool).toBe("http_request");
  });

  test("empty graphs and pending actions without a materialized parent have no dangling links", () => {
    const graph = snapshot([]);
    expect(layoutGraph(graph, null)).toEqual({ nodes: [], links: [] });
    const result = layoutGraph(graph, pending);
    expect(result.nodes).toHaveLength(1);
    expect(result.nodes[0].kind).toBe("pending");
    expect(result.links).toEqual([]);
    expect(graph.nodes.size).toBe(0);
  });
});
