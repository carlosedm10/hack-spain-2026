import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";

import App from "@/App";
import { DEMO_INTERVAL, useDemo } from "@/dashboard/demo";
import { FakeEventSource } from "@/graph/fake-event-source";
import {
  RECONNECT_DELAY_MS,
  STREAM_URL,
  useGraphStream,
  type GraphStream,
} from "@/graph/useGraphStream";

const NATIVE_EVENT_SOURCE = globalThis.EventSource;

let stream: GraphStream;
let container: HTMLDivElement;
let root: Root;

function Probe() {
  stream = useGraphStream();
  return null;
}

function mount(node: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  act(() => root.render(node));
}

function node(id: string, neighbors: string[] = [], runId: string | null = null) {
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

function emit(event: string, data: unknown) {
  act(() => FakeEventSource.last.emit(event, data));
}

async function waitForReconnect() {
  await act(async () => {
    await new Promise((resolve) =>
      setTimeout(resolve, RECONNECT_DELAY_MS + 50),
    );
  });
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

describe("useGraphStream", () => {
  test("applies a snapshot and then updates", () => {
    mount(<Probe />);
    expect(FakeEventSource.last.url).toBe(STREAM_URL);

    emit("snapshot", { revision: 1, root: "root", nodes: [node("root")] });
    expect(stream.status).toBe("live");
    expect(stream.graph?.nodes.size).toBe(1);

    emit("update", {
      revision: 2,
      root: "root",
      upsert_nodes: [node("root", ["run:r1"]), node("run:r1", ["root"])],
      removed_node_ids: [],
    });
    expect(stream.graph?.revision).toBe(2);
    expect(stream.graph?.nodes.get("root")?.neighbors).toEqual(["run:r1"]);

    emit("update", {
      revision: 3,
      root: null,
      upsert_nodes: [],
      removed_node_ids: ["root", "run:r1"],
    });
    expect(stream.graph?.nodes.size).toBe(0);
    expect(stream.error).toBeNull();
  });

  test("reconnects to a fresh snapshot after a revision gap", async () => {
    mount(<Probe />);
    emit("snapshot", { revision: 1, root: "root", nodes: [node("root")] });
    emit("update", {
      revision: 5,
      root: "root",
      upsert_nodes: [],
      removed_node_ids: [],
    });

    expect(stream.status).toBe("reconnecting");
    expect(stream.error).toBe("revision gap");
    expect(FakeEventSource.instances[0].closed).toBe(true);

    await waitForReconnect();
    expect(FakeEventSource.instances).toHaveLength(2);

    emit("snapshot", {
      revision: 9,
      root: "root",
      nodes: [node("root"), node("other")],
    });
    expect(stream.status).toBe("live");
    expect(stream.graph?.revision).toBe(9);
    expect(stream.graph?.nodes.size).toBe(2);
  });

  test("reconnects on an invalid message", async () => {
    mount(<Probe />);
    emit("snapshot", "{not json");
    expect(stream.status).toBe("reconnecting");
    expect(stream.error).toBe("invalid snapshot");

    await waitForReconnect();
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  test("reconnects when the server drops the stream", async () => {
    mount(<Probe />);
    emit("snapshot", { revision: 1, root: null, nodes: [] });
    act(() => FakeEventSource.last.fail());

    expect(stream.status).toBe("reconnecting");
    expect(stream.error).toBe("stream closed");
    await waitForReconnect();
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  test("closes the connection on unmount", () => {
    mount(<Probe />);
    const source = FakeEventSource.last;
    act(() => root.unmount());
    expect(source.closed).toBe(true);
  });
});

describe("App", () => {
  test("streams the shared graph and reuses upserted nodes", () => {
    mount(<App />);
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.last.url).toBe(STREAM_URL);
    expect(container.textContent).toContain("CONNECTING");
    expect(
      container.querySelector('[aria-label="Restart test run"]'),
    ).toBeNull();

    emit("snapshot", {
      revision: 1,
      root: "root",
      nodes: [
        node("root", ["run:r1"]),
        node("run:r1", ["root", "r1:1"], "r1"),
        {
          ...node("r1:1", ["run:r1"], "r1"),
          level: 2,
          threshold: 0.9,
          event: { label: "Read file" },
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
    });
    expect(container.textContent).toContain("LIVE");
    expect(
      container.querySelector('.react-flow__node[data-id="r1:1"]'),
    ).not.toBeNull();
    expect(
      container.querySelector('[aria-label="Container logs"]')?.textContent,
    ).toContain("Read file");

    emit("update", {
      revision: 2,
      root: "root",
      upsert_nodes: [
        {
          ...node("r1:1", ["run:r1"], "r1"),
          level: 3,
          threshold: 0.9,
          event: { label: "Read file" },
          created_at: "2026-01-01T00:00:01Z",
        },
      ],
      removed_node_ids: [],
    });
    expect(
      container.querySelectorAll('.react-flow__node[data-id="r1:1"]'),
    ).toHaveLength(1);
    expect(container.textContent).toContain("L3");
  });

  test("renders protective actions from the incident feed", async () => {
    const incident = {
      incident_id: "r1",
      accepted_level: 4,
      rows: { 1: "idle", 2: "idle", 3: "ok", 4: "running", 5: "idle" },
      actions: [
        {
          kind: "action_transition",
          incident_id: "r1",
          level: 4,
          action_id: "r1:contain_all_runs",
          name: "contain_all_runs",
          ladder_level: 3,
          mode: "simulated",
          status: "ok",
          timestamp: "2026-01-01T00:00:01Z",
          detail: null,
          error_code: null,
          call_status: null,
        },
        {
          kind: "action_transition",
          incident_id: "r1",
          level: 4,
          action_id: "r1:page_oncall:l4",
          name: "page_oncall",
          ladder_level: null,
          mode: "real",
          status: "running",
          timestamp: "2026-01-01T00:00:02Z",
          detail: null,
          error_code: null,
          call_status: "ringing",
        },
      ],
      pager_status: "running",
      call_status: "ringing",
      updated_at: "2026-01-01T00:00:02Z",
    };
    const nativeFetch = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(incident))) as unknown as typeof fetch;
    try {
      mount(<App />);
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      const panel = container.querySelector(
        '[aria-label="Protective actions"]',
      );
      expect(panel?.textContent).toContain("contain_all_runs");
      expect(panel?.textContent).toContain("page_oncall");
      expect(panel?.textContent).toContain("HappyRobot");
    } finally {
      globalThis.fetch = nativeFetch;
    }
  });

  test("renders a mock dashboard without opening the backend stream", () => {
    mount(<App demo />);
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(container.textContent).toContain("Agent activity");
    expect(container.textContent).toContain("Protective actions");
    expect(container.textContent).toContain("Container logs");
    expect(
      container.querySelector('header img[src="/angry-robot.svg"]'),
    ).toBeNull();
    expect(
      container.querySelector('header[aria-label="AngryRobot"]')?.className,
    ).toContain("justify-center");
    expect(
      container
        .querySelector('header h1 img[alt="AngryRobot"]')
        ?.getAttribute("src"),
    ).toBe("/angry-robot-wordmark.svg");
    expect(container.textContent).not.toContain("Eyes on every action");
    expect(container.textContent).not.toContain("Follow live");
    expect(container.textContent).not.toContain(
      "Links show activity associations",
    );
    expect(container.querySelector('[aria-label="Graph legend"]')).toBeNull();
    expect(
      container.querySelector('[aria-label="Session summary"]'),
    ).toBeNull();
    expect(container.querySelector('input[type="search"]')).toBeNull();
    expect(
      container.querySelector('[aria-label="Filter container logs"]'),
    ).toBeNull();
    expect(container.querySelector('[aria-label="Replay demo"]')).toBeNull();
    const headers = Array.from(
      container.querySelectorAll("header h2"),
      (header) => header.textContent?.trim(),
    );
    expect(headers).toEqual([
      "Agent activity",
      "Protective actions",
      "Container logs",
    ]);
    const node = container.querySelector<HTMLDivElement>(
      '.react-flow__node[data-id="atlas:3"]',
    )!;
    expect(node.style.pointerEvents).toBe("all");
    act(() => node.click());
    expect(
      container.querySelector('[aria-label="Selected action analysis"]')
        ?.textContent,
    ).toContain("Repeated boundary probing");
    act(() =>
      container
        .querySelector<HTMLButtonElement>('[aria-label="Close analysis"]')!
        .click(),
    );
    expect(
      container.querySelector('[aria-label="Selected action analysis"]'),
    ).toBeNull();
  });

  test("restarts the test run and clears old activity and selection", () => {
    mount(<App demo />);
    act(() =>
      container
        .querySelector<HTMLDivElement>('.react-flow__node[data-id="atlas:3"]')!
        .click(),
    );
    expect(
      container.querySelector('[aria-label="Selected action analysis"]'),
    ).not.toBeNull();
    const restart = container.querySelector<HTMLButtonElement>(
      '[aria-label="Restart test run"]',
    );
    expect(restart).not.toBeNull();
    act(() => restart!.click());
    expect(container.querySelectorAll(".react-flow__node")).toHaveLength(3);
    expect(
      container.querySelector('[aria-label="Selected action analysis"]'),
    ).toBeNull();
    expect(
      container.querySelector('[aria-label="Protective actions"]')?.textContent,
    ).toContain("Waiting for Jev");
    expect(
      container.querySelector('[aria-label="Container logs"]')?.textContent,
    ).not.toContain("Boundary probing detected");
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  test("automatically adds pending activity without playback controls", async () => {
    let frame: ReturnType<typeof useDemo> | undefined;
    function DemoProbe() {
      frame = useDemo();
      return null;
    }
    mount(<DemoProbe />);
    expect(frame?.graph.nodes.size).toBe(7);
    expect(frame?.pending).toBeNull();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, DEMO_INTERVAL + 100));
    });
    expect(frame?.graph.nodes.size).toBe(7);
    expect(frame?.pending?.id).toBe("scout:2");
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});
