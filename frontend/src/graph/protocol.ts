export type GraphNode = {
  id: string;
  neighbors: string[];
  threshold: number;
  run_id: string | null;
  run_ids: string[];
  visit_count: number;
  level: number;
  intent: string | null;
  event: Record<string, unknown> | null;
  action_id: string | null;
  created_at: string | null;
};

type GraphSnapshot = {
  revision: number;
  root: string | null;
  nodes: GraphNode[];
};

type GraphUpdate = {
  revision: number;
  root: string | null;
  upsert_nodes: GraphNode[];
  removed_node_ids: string[];
};

export type Graph = {
  revision: number;
  root: string | null;
  nodes: Map<string, GraphNode>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNode(value: unknown): value is GraphNode {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    Array.isArray(value.neighbors) &&
    value.neighbors.every((id) => typeof id === "string") &&
    (value.run_ids === undefined || Array.isArray(value.run_ids)) &&
    (value.visit_count === undefined || typeof value.visit_count === "number")
  );
}

function isNodeList(value: unknown): value is GraphNode[] {
  return Array.isArray(value) && value.every(isNode);
}

function isRoot(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function parse(raw: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(raw);
    return isRecord(value) ? value : null;
  } catch {
    return null;
  }
}

export function parseSnapshot(raw: string): GraphSnapshot | null {
  const value = parse(raw);
  if (
    value === null ||
    typeof value.revision !== "number" ||
    !isRoot(value.root) ||
    !isNodeList(value.nodes)
  ) {
    return null;
  }
  return { revision: value.revision, root: value.root, nodes: value.nodes };
}

export function parseUpdate(raw: string): GraphUpdate | null {
  const value = parse(raw);
  if (
    value === null ||
    typeof value.revision !== "number" ||
    !isRoot(value.root) ||
    !isNodeList(value.upsert_nodes) ||
    !Array.isArray(value.removed_node_ids) ||
    !value.removed_node_ids.every((id) => typeof id === "string")
  ) {
    return null;
  }
  return {
    revision: value.revision,
    root: value.root,
    upsert_nodes: value.upsert_nodes,
    removed_node_ids: value.removed_node_ids as string[],
  };
}

function normalizeNode(node: GraphNode): GraphNode {
  return {
    ...node,
    run_ids: node.run_ids ?? (node.run_id ? [node.run_id] : []),
    visit_count: node.visit_count ?? 1,
  };
}

export function fromSnapshot(snapshot: GraphSnapshot): Graph {
  return {
    revision: snapshot.revision,
    root: snapshot.root,
    nodes: new Map(snapshot.nodes.map((node) => [node.id, normalizeNode(node)])),
  };
}

export function applyUpdate(graph: Graph, update: GraphUpdate): Graph | null {
  if (update.revision !== graph.revision + 1) {
    return null;
  }
  const nodes = new Map(graph.nodes);
  for (const id of update.removed_node_ids) {
    nodes.delete(id);
  }
  for (const node of update.upsert_nodes) {
    nodes.set(node.id, normalizeNode(node));
  }
  return { revision: update.revision, root: update.root, nodes };
}
