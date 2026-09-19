import { describe, expect, test } from "bun:test";

import { chainEdges, LEVEL_STROKE } from "@/live/path";
import { fromSnapshot, type GraphNode } from "@/graph/protocol";

function node(
  id: string,
  runId: string | null,
  level = 0,
  created_at: string | null = null,
): GraphNode {
  return {
    id,
    neighbors: [],
    threshold: 0,
    run_id: runId,
    level,
    intent: null,
    event: null,
    action_id: null,
    created_at,
  };
}

const graph = (nodes: GraphNode[]) =>
  fromSnapshot({ revision: 1, root: "root", nodes });

describe("chainEdges", () => {
  test("chains run start and sequence nodes across runs, ordered by seq not insertion", () => {
    const result = chainEdges(
      graph([
        node("root", null),
        node("run:atlas", "atlas"),
        node("atlas:2", "atlas", 3, "2026-01-01T00:00:02Z"),
        node("atlas:1", "atlas", 1, "2026-01-01T00:00:01Z"),
        node("run:scout", "scout"),
        node("scout:1", "scout", 0, "2026-01-01T00:00:03Z"),
      ]),
    );
    expect(result.map((edge) => [edge.source, edge.target])).toEqual([
      ["run:atlas", "atlas:1"],
      ["atlas:1", "atlas:2"],
      ["run:scout", "scout:1"],
    ]);
  });

  test("level escalates to the max seen so far within the run", () => {
    const result = chainEdges(
      graph([
        node("run:atlas", "atlas"),
        node("atlas:1", "atlas", 2),
        node("atlas:2", "atlas", 1),
        node("atlas:3", "atlas", 4),
      ]),
    );
    expect(result.map((edge) => edge.level)).toEqual([2, 2, 4]);
    expect(result.map((edge) => LEVEL_STROKE[edge.level])).toEqual([
      "#b06a38",
      "#b06a38",
      "#d12a2a",
    ]);
  });

  test("latest flags only the final edge of the run with the globally newest node", () => {
    const result = chainEdges(
      graph([
        node("run:atlas", "atlas"),
        node("atlas:1", "atlas", 0, "2026-01-01T00:00:01Z"),
        node("atlas:2", "atlas", 0, "2026-01-01T00:00:05Z"),
        node("run:scout", "scout"),
        node("scout:1", "scout", 0, "2026-01-01T00:00:03Z"),
        node("scout:2", "scout", 0, "2026-01-01T00:00:04Z"),
      ]),
    );
    expect(result.filter((edge) => edge.latest)).toEqual([
      {
        id: JSON.stringify(["atlas:1", "atlas:2"].sort()),
        source: "atlas:1",
        target: "atlas:2",
        level: 0,
        latest: true,
      },
    ]);
  });

  test("falls back to insertion order for newest when created_at is missing", () => {
    const result = chainEdges(
      graph([
        node("run:scout", "scout"),
        node("scout:1", "scout"),
        node("run:atlas", "atlas"),
        node("atlas:1", "atlas"),
      ]),
    );
    expect(
      result.filter((edge) => edge.latest).map((edge) => edge.target),
    ).toEqual(["atlas:1"]);
  });

  test("chains without a run start node and ignores non-sequence ids", () => {
    const result = chainEdges(
      graph([
        node("atlas:1", "atlas", 1),
        node("atlas:2", "atlas", 2),
        node("atlas:notes", "atlas", 5),
      ]),
    );
    expect(result.map((edge) => [edge.source, edge.target])).toEqual([
      ["atlas:1", "atlas:2"],
    ]);
  });
});
