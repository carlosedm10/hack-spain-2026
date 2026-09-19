import type { PendingAction } from "@/dashboard/demo";
import type { Graph, GraphNode } from "@/graph/protocol";

export const NODE_WIDTH = 216;
export const NODE_HEIGHT = 104;
const COLUMN_GAP = 260;
const ROW_GAP = 142;

export type ActivityItem = {
  id: string;
  label: string;
  tool: string;
  runId: string | null;
  position: { x: number; y: number };
} & (
  | { kind: "structure" | "pending" }
  | { kind: "classified"; level: number; confidence: number }
);

export type ActivityLink = {
  id: string;
  source: string;
  target: string;
  pending: boolean;
};

export function eventText(
  node: GraphNode,
  keys: string[],
  fallback: string,
) {
  for (const key of keys) {
    const value = node.event?.[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return fallback;
}

export function layoutGraph(graph: Graph, pending: PendingAction | null) {
  // BFS depth from the root so shared action nodes and cycles get one stable position.
  const depth = new Map<string, number>();
  const rootId = graph.root;
  if (rootId && graph.nodes.has(rootId)) {
    const queue: string[] = [rootId];
    depth.set(rootId, 0);
    while (queue.length) {
      const current = queue.shift()!;
      const currentDepth = depth.get(current)!;
      const node = graph.nodes.get(current);
      if (!node) continue;
      for (const neighbor of node.neighbors) {
        if (depth.has(neighbor)) continue;
        depth.set(neighbor, currentDepth + 1);
        queue.push(neighbor);
      }
    }
  }

  const rowByDepth = new Map<number, number>();
  const nextPosition = (nodeId: string) => {
    const d = depth.get(nodeId) ?? (rowByDepth.size + 1);
    let row = rowByDepth.get(d);
    if (row === undefined) {
      row = 0;
    }
    rowByDepth.set(d, row + 1);
    return { x: d * COLUMN_GAP, y: row * ROW_GAP };
  };

  const nodes: ActivityItem[] = Array.from(graph.nodes.values(), (node) => {
    const root = node.id === graph.root;
    const base = {
      id: node.id,
      label: eventText(
        node,
        ["label", "kind", "event"],
        root ? "Activity entry point" : (node.run_id ?? node.id),
      ),
      tool: eventText(node, ["tool", "target", "event"], ""),
      runId: node.run_id,
      position: nextPosition(node.id),
    };
    return node.event === null
      ? { ...base, kind: "structure" }
      : {
          ...base,
          kind: "classified",
          level: node.level,
          confidence: node.threshold,
        };
  });

  if (pending && !graph.nodes.has(pending.id)) {
    const d = graph.nodes.has(pending.parentId)
      ? (depth.get(pending.parentId) ?? 0) + 1
      : rowByDepth.size + 1;
    let row = rowByDepth.get(d) ?? 0;
    rowByDepth.set(d, row + 1);
    nodes.push({
      id: pending.id,
      label: pending.label,
      tool: pending.tool,
      runId: pending.run_id,
      position: { x: d * COLUMN_GAP, y: row * ROW_GAP },
      kind: "pending",
    });
  }

  const positions = new Map(nodes.map((node) => [node.id, node.position]));
  const links = new Map<string, ActivityLink>();
  for (const node of graph.nodes.values()) {
    for (const neighbor of node.neighbors) {
      if (neighbor === node.id || !graph.nodes.has(neighbor)) continue;
      const pair = [node.id, neighbor].sort();
      const id = JSON.stringify(pair);
      const a = positions.get(pair[0])!;
      const b = positions.get(pair[1])!;
      const forward = a.x < b.x || (a.x === b.x && a.y <= b.y);
      links.set(id, {
        id,
        source: pair[forward ? 0 : 1],
        target: pair[forward ? 1 : 0],
        pending: false,
      });
    }
  }
  if (
    pending &&
    !graph.nodes.has(pending.id) &&
    graph.nodes.has(pending.parentId)
  ) {
    const id = JSON.stringify([pending.parentId, pending.id].sort());
    links.set(id, {
      id,
      source: pending.parentId,
      target: pending.id,
      pending: true,
    });
  }
  return {
    nodes,
    links: Array.from(links.values()).sort((a, b) => a.id.localeCompare(b.id)),
  };
}
