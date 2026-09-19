import { afterEach, beforeEach, expect, test } from "bun:test";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import {
  InteractiveLogsTable,
  type Log,
} from "@/components/ui/interactive-logs-table";
import { ActivityPanel } from "@/dashboard/ActivityPanel";
import type { SafeAction } from "@/dashboard/demo";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const action: SafeAction = {
  id: "action-1",
  nodeId: "node-1",
  title: "Notify on-call",
  source: "HappyRobot",
  status: "failed",
  level: 4,
  startedAt: "2026-06-01T12:00:00Z",
  details: [
    {
      label: "Transcript",
      meta: "Call could not connect; notification failed.",
    },
  ],
};

const logs: Log[] = [
  {
    id: "1",
    timestamp: "2026-06-01T14:00:00+02:00",
    level: "warning",
    service: "sandbox",
    message: "Unexpected outbound request",
    duration: "20ms",
    status: "blocked",
    tags: ["node-1"],
  },
  {
    id: "2",
    timestamp: "2026-06-01T12:01:00Z",
    level: "info",
    service: "monitor",
    message: "Capture started",
    duration: "1ms",
    status: "ok",
    tags: ["node-2"],
  },
];

function button(text: string) {
  const result = Array.from(container.querySelectorAll("button")).find(
    (entry) => entry.textContent?.includes(text),
  );
  if (!result) throw new Error(`Missing button: ${text}`);
  return result;
}

test("action rows select graph nodes on opening and reflect controlled status and transcript updates", () => {
  const selected: string[] = [];
  const render = (value: SafeAction) =>
    act(() =>
      root.render(
        <ActivityPanel
          actions={[value]}
          selectedNodeId="node-1"
          onSelectNode={(id) => selected.push(id)}
        />,
      ),
    );
  render(action);
  const row = button("Notify on-call");
  expect(row.getAttribute("aria-current")).toBe("true");
  expect(row.textContent).toContain("Failed");
  expect(row.getAttribute("aria-expanded")).toBe("false");
  row.focus();
  expect(document.activeElement).toBe(row);
  act(() => row.click());
  expect(selected).toEqual(["node-1"]);
  expect(row.getAttribute("aria-expanded")).toBe("true");
  const details = document.getElementById(row.getAttribute("aria-controls")!);
  expect(details?.getAttribute("aria-hidden")).toBe("false");
  expect(details?.textContent).toContain(action.details[0].meta);
  render({
    ...action,
    status: "done",
    details: [{ label: "Transcript", meta: "On-call acknowledged." }],
  });
  expect(row.textContent).toContain("Completed");
  expect(details?.textContent).toContain("On-call acknowledged.");
  act(() => row.click());
  expect(selected).toEqual(["node-1"]);
  render({ ...action, status: "running" });
  expect(row.textContent).toContain("Running");
});

test("logs show all entries with UTC timestamps and expandable details without search or filters", () => {
  act(() => root.render(<InteractiveLogsTable logs={logs} />));
  expect(container.textContent).toContain("Container logs");
  expect(button("Unexpected outbound request").textContent).toContain(
    "12:00:00",
  );
  act(() => button("Unexpected outbound request").click());
  expect(
    button("Unexpected outbound request").getAttribute("aria-expanded"),
  ).toBe("true");
  expect(
    container.querySelector('[aria-label="Log tags"]')?.textContent,
  ).toContain("node-1");
  expect(container.querySelector("input")).toBeNull();
  expect(
    container.querySelector('[aria-label="Filter container logs"]'),
  ).toBeNull();
  expect(container.querySelector("header")?.textContent?.trim()).toBe(
    "Container logs",
  );
  expect(container.textContent).toContain("Capture started");
  expect(button("Unexpected outbound request").className).toContain(
    "bg-[#f7f4e8]",
  );
  act(() => root.render(<InteractiveLogsTable logs={[]} />));
  expect(container.textContent).toContain("Waiting for container logs.");
});

test("empty activity waits for Jev rather than showing sample tasks", () => {
  act(() =>
    root.render(
      <ActivityPanel
        actions={[]}
        selectedNodeId={null}
        onSelectNode={() => {}}
      />,
    ),
  );
  expect(container.textContent).toContain("Waiting for Jev");
  expect(container.querySelectorAll("button").length).toBe(0);
});
