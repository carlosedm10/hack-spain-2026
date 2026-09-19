import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

import { FakeEventSource } from "@/graph/fake-event-source";
import { STREAM_URL } from "@/graph/useGraphStream";
import { LivePage } from "@/live/LivePage";
import type { GraphNode } from "@/graph/protocol";

const NATIVE_EVENT_SOURCE = globalThis.EventSource;

let container: HTMLDivElement;
let root: Root;

function mount(node: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root.render(node));
}

function node(
  id: string,
  runId: string | null,
  neighbors: string[] = [],
  overrides: Partial<GraphNode> = {},
): GraphNode {
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
    ...overrides,
  };
}

function emit(event: string, data: unknown) {
  act(() => FakeEventSource.last.emit(event, data));
}

beforeEach(() => {
  FakeEventSource.reset();
  globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  globalThis.EventSource = NATIVE_EVENT_SOURCE;
});

describe("LivePage", () => {
  test("streams the real graph: connects, shows CONNECTING then LIVE with level chip and node labels", () => {
    mount(<LivePage demo={false} />);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last.url).toBe(STREAM_URL);
    expect(container.textContent).toContain("CONNECTING");

    emit("snapshot", {
      revision: 1,
      root: "root",
      nodes: [
        node("root", null, ["run:demo"]),
        node("run:demo", "demo", ["root", "demo:1"]),
        node("demo:1", "demo", ["run:demo"], {
          level: 1,
          threshold: 0.9,
          event: { kind: "file_read", tool: "read_file" },
          created_at: "2026-01-01T00:00:01Z",
        }),
      ],
    });

    expect(
      container.querySelector('.react-flow__node[data-id="demo:1"]'),
    ).not.toBeNull();
    expect(container.textContent).toContain("LIVE");
    expect(container.textContent).toContain("L1");
    expect(container.textContent).toContain("file_read");

    emit("update", {
      revision: 2,
      root: "root",
      upsert_nodes: [
        node("demo:1", "demo", ["run:demo", "demo:2"], {
          level: 1,
          threshold: 0.9,
          event: { kind: "file_read", tool: "read_file" },
          created_at: "2026-01-01T00:00:01Z",
        }),
        node("demo:2", "demo", ["demo:1"], {
          level: 3,
          threshold: 0.8,
          event: { kind: "network_request", tool: "http_request" },
          created_at: "2026-01-01T00:00:02Z",
        }),
      ],
      removed_node_ids: [],
    });

    const added = container.querySelector<HTMLDivElement>(
      '.react-flow__node[data-id="demo:2"]',
    );
    expect(added).not.toBeNull();
    expect(added!.className).toContain("live-node-new");
    expect(container.textContent).toContain("L3");
  });

  test("demo mode renders the mock graph without opening an EventSource", () => {
    mount(<LivePage demo={true} />);
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(
      container.querySelectorAll(".react-flow__node").length,
    ).toBeGreaterThan(0);
    expect(container.textContent).toContain("LIVE");
  });
});
