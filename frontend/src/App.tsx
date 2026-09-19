import { useMemo, useState } from "react";
import { ArrowUpRight, X } from "lucide-react";
import { MotionConfig } from "framer-motion";

import { Button } from "@/components/ui/button";
import { InteractiveLogsTable } from "@/components/ui/interactive-logs-table";
import { ActivityPanel } from "@/dashboard/ActivityPanel";
import { GraphPanel } from "@/dashboard/GraphPanel";
import {
  LEVEL_LABELS,
  useDemo,
  type DemoLog,
  type PendingAction,
  type SafeAction,
} from "@/dashboard/demo";
import { actionsFromIncident, logsFromGraph } from "@/dashboard/feeds";
import { eventText } from "@/dashboard/graph-layout";
import { useIncidentFeed } from "@/dashboard/useIncidentFeed";
import type { Graph } from "@/graph/protocol";
import {
  useGraphStream,
  type GraphStreamStatus,
} from "@/graph/useGraphStream";

const EMPTY_GRAPH: Graph = { revision: 0, root: null, nodes: new Map() };

type DashboardProps = {
  graph: Graph;
  pending: PendingAction | null;
  actions: SafeAction[];
  logs: DemoLog[];
  status: GraphStreamStatus | null;
  onRestart?: () => void;
};

function Dashboard({
  graph,
  pending,
  actions,
  logs,
  status,
  onRestart,
}: DashboardProps) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const selected = selectedNodeId ? graph.nodes.get(selectedNodeId) : null;
  const pendingSelected =
    selectedNodeId === pending?.id ? pending : null;
  const inspectorTitle =
    pendingSelected?.label ??
    String(selected?.event?.label ?? selected?.id ?? "");

  return (
    <MotionConfig reducedMotion="user">
      <main
        id="dashboard"
        aria-label={
          status === null
            ? "Agent safety dashboard · simulated data"
            : "Agent safety dashboard"
        }
        className="mx-auto min-h-dvh max-w-[1920px] bg-[#fdfcf4] p-4 text-zinc-900"
      >
        <header
          aria-label="AngryRobot"
          className="mb-2 flex h-14 items-center justify-center"
        >
          <h1>
            <img
              src="/angry-robot-wordmark.svg"
              alt="AngryRobot"
              width={110}
              height={48}
              className="h-12 w-auto"
            />
          </h1>
        </header>
        <div className="dashboard-grid grid gap-2 lg:grid-cols-2">
          <section
            aria-label="Action graph and analysis"
            className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border shadow-xs"
          >
            <div className="min-h-0 flex-1">
              <GraphPanel
                graph={graph}
                pending={pending}
                selectedNodeId={selectedNodeId}
                onSelectNode={setSelectedNodeId}
                onRestart={onRestart}
                status={status}
              />
            </div>
            {(selected || pendingSelected) && (
              <section
                aria-label="Selected action analysis"
                className="max-h-56 shrink-0 overflow-y-auto border-t bg-[#fcfcfc] p-4"
              >
                <div className="mb-2 flex items-start justify-between gap-2">
                  <h3 className="text-sm font-semibold">{inspectorTitle}</h3>
                  <Button
                    aria-label="Close analysis"
                    size="icon-sm"
                    variant="ghost"
                    onClick={() => setSelectedNodeId(null)}
                  >
                    <X />
                  </Button>
                </div>
                <p className="text-xs leading-relaxed text-zinc-600">
                  {pendingSelected
                    ? "Awaiting Jev. No safety verdict or protective action has been assigned to this event."
                    : selected?.event
                      ? eventText(
                          selected,
                          ["content", "summary", "label", "kind", "event"],
                          "Classified action.",
                        )
                      : "Structural graph node. This groups the session or run; it is not a safety verdict."}
                </p>
                {selected?.event && (
                  <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
                    <span
                      className="rounded-md border px-2 py-1 font-medium"
                      style={
                        selected.level >= 3
                          ? {
                              borderColor: "#e0a49c",
                              background: "#f6e6e4",
                              color: "#b42318",
                            }
                          : selected.level > 0
                            ? {
                                borderColor: "#d5c9a3",
                                background: "#ebe6d2",
                                color: "#7a4a24",
                              }
                            : {
                                borderColor: "#a5c4ae",
                                background: "#dde8d8",
                                color: "#027a48",
                              }
                      }
                    >
                      L{selected.level} · {LEVEL_LABELS[selected.level]}
                    </span>
                    <span className="rounded-md bg-zinc-100 px-2 py-1">
                      {Math.round(selected.threshold * 100)}% confidence
                    </span>
                    <span className="font-mono text-zinc-500">
                      {selected.intent}
                    </span>
                  </div>
                )}
                {selected?.action_id && (
                  <p className="mt-2 flex items-center gap-1 text-[11px] text-zinc-500">
                    <ArrowUpRight className="size-3" />
                    Response trace:{" "}
                    <span className="font-mono">{selected.action_id}</span>
                  </p>
                )}
              </section>
            )}
          </section>
          <div className="grid min-h-0 min-w-0 grid-rows-[minmax(250px,0.95fr)_minmax(280px,1fr)] gap-2">
            <ActivityPanel
              actions={actions}
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
            />
            <section
              aria-label="Container log viewer"
              className="min-h-0 min-w-0 overflow-hidden rounded-xl border shadow-xs"
            >
              <InteractiveLogsTable logs={logs} />
            </section>
          </div>
        </div>
      </main>
    </MotionConfig>
  );
}

function DemoDashboard() {
  const demo = useDemo();
  return (
    <Dashboard
      key={demo.run}
      graph={demo.graph}
      pending={demo.pending}
      actions={demo.actions}
      logs={demo.logs}
      status={null}
      onRestart={demo.restart}
    />
  );
}

function StreamDashboard() {
  const { graph, status } = useGraphStream();
  const incident = useIncidentFeed();
  const view = graph ?? EMPTY_GRAPH;
  const actions = useMemo(
    () => (incident === null ? [] : actionsFromIncident(incident)),
    [incident],
  );
  const logs = useMemo(() => logsFromGraph(view), [view]);
  return (
    <Dashboard
      graph={view}
      pending={null}
      actions={actions}
      logs={logs}
      status={status}
    />
  );
}

export default function App({ demo = false }: { demo?: boolean }) {
  return demo ? <DemoDashboard /> : <StreamDashboard />;
}
