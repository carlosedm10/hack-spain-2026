import type { Graph } from "@/graph/protocol";

export const LEVEL_STROKE = [
  "#5fa46b",
  "#d59566",
  "#b06a38",
  "#d9584b",
  "#d12a2a",
  "#b42318",
];

export type ChainEdge = {
  id: string;
  source: string;
  target: string;
  level: number;
  latest: boolean;
};

export function chainEdges(graph: Graph): ChainEdge[] {
  const runs = new Map<string, { seq: number; id: string; level: number }[]>();
  let newestId: string | null = null;
  let newestAt = "";
  for (const node of graph.nodes.values()) {
    if (node.run_id === null) continue;
    const match = /^(.+):(\d+)$/.exec(node.id);
    if (match === null || match[1] !== node.run_id) continue;
    const stamp = node.created_at ?? "";
    if (stamp >= newestAt) {
      newestAt = stamp;
      newestId = node.id;
    }
    const items = runs.get(node.run_id) ?? [];
    items.push({ seq: Number(match[2]), id: node.id, level: node.level });
    runs.set(node.run_id, items);
  }
  const edges: ChainEdge[] = [];
  for (const [run, items] of runs) {
    items.sort((a, b) => a.seq - b.seq);
    let level = graph.nodes.get(`run:${run}`)?.level ?? 0;
    let previous = graph.nodes.has(`run:${run}`) ? `run:${run}` : null;
    let last: ChainEdge | null = null;
    for (const item of items) {
      level = Math.max(level, item.level);
      if (previous !== null) {
        last = {
          id: JSON.stringify([previous, item.id].sort()),
          source: previous,
          target: item.id,
          level,
          latest: false,
        };
        edges.push(last);
      }
      previous = item.id;
    }
    if (last !== null && items.some((item) => item.id === newestId)) {
      last.latest = true;
    }
  }
  return edges;
}
