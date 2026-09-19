import { describe, expect, test } from "bun:test";

import {
  applyUpdate,
  fromSnapshot,
  parseSnapshot,
  parseUpdate,
  type GraphNode,
} from "@/graph/protocol";

function node(id: string, neighbors: string[] = [], runId: string | null = null): GraphNode {
  return {
    id,
    neighbors,
    threshold: 0,
    run_id: runId,
    run_ids: runId ? [runId] : [],
    visit_count: 1,
    level: 0,
    intent: null,
    event: null,
    action_id: null,
    created_at: null,
  };
}

describe("parsing", () => {
  test("reads a snapshot", () => {
    const snapshot = parseSnapshot(
      JSON.stringify({ revision: 3, root: "root", nodes: [node("root")] }),
    );
    expect(snapshot?.revision).toBe(3);
    expect(snapshot?.nodes).toHaveLength(1);
  });

  test("reads an empty snapshot", () => {
    const snapshot = parseSnapshot(
      JSON.stringify({ revision: 0, root: null, nodes: [] }),
    );
    expect(snapshot).toEqual({ revision: 0, root: null, nodes: [] });
  });

  test("rejects malformed payloads", () => {
    expect(parseSnapshot("not json")).toBeNull();
    expect(
      parseSnapshot(JSON.stringify({ revision: "3", root: null, nodes: [] })),
    ).toBeNull();
    expect(
      parseSnapshot(JSON.stringify({ revision: 1, root: null })),
    ).toBeNull();
    expect(
      parseUpdate(
        JSON.stringify({ revision: 1, root: null, upsert_nodes: [] }),
      ),
    ).toBeNull();
    expect(
      parseUpdate(
        JSON.stringify({
          revision: 1,
          root: null,
          upsert_nodes: [{ id: "a" }],
          removed_node_ids: [],
        }),
      ),
    ).toBeNull();
  });
});

describe("applying updates", () => {
  const base = fromSnapshot({
    revision: 1,
    root: "root",
    nodes: [node("root", ["run:r1"]), node("run:r1", ["root"])],
  });

  test("adds nodes and rewires neighbors", () => {
    const next = applyUpdate(base, {
      revision: 2,
      root: "root",
      upsert_nodes: [
        node("run:r1", ["root", "r1:1"]),
        node("r1:1", ["run:r1"]),
      ],
      removed_node_ids: [],
    });

    expect(next?.revision).toBe(2);
    expect(next?.nodes.size).toBe(3);
    expect(next?.nodes.get("run:r1")?.neighbors).toEqual(["root", "r1:1"]);
  });

  test("replaces a node wholesale", () => {
    const next = applyUpdate(base, {
      revision: 2,
      root: "root",
      upsert_nodes: [
        {
          ...node("run:r1", ["root"]),
          level: 4,
          intent: "exfil",
          threshold: 0.9,
        },
      ],
      removed_node_ids: [],
    });

    expect(next?.nodes.get("run:r1")).toEqual({
      ...node("run:r1", ["root"]),
      level: 4,
      intent: "exfil",
      threshold: 0.9,
    });
  });

  test("removes nodes and clears the root", () => {
    const next = applyUpdate(base, {
      revision: 2,
      root: null,
      upsert_nodes: [],
      removed_node_ids: ["root", "run:r1"],
    });

    expect(next?.root).toBeNull();
    expect(next?.nodes.size).toBe(0);
  });

  test("leaves the previous graph untouched", () => {
    applyUpdate(base, {
      revision: 2,
      root: "root",
      upsert_nodes: [node("r1:1", ["run:r1"])],
      removed_node_ids: ["run:r1"],
    });

    expect(base.nodes.size).toBe(2);
    expect(base.revision).toBe(1);
  });

  test("refuses a revision gap", () => {
    for (const revision of [1, 3]) {
      expect(
        applyUpdate(base, {
          revision,
          root: "root",
          upsert_nodes: [],
          removed_node_ids: [],
        }),
      ).toBeNull();
    }
  });
});
