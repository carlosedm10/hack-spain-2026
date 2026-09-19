import { useEffect, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  type Edge,
} from "@xyflow/react";
import { Play, RotateCcw, Workflow } from "lucide-react";
import "@xyflow/react/dist/style.css";

import {
  edgeTypes,
  FollowViewport,
  GraphControls,
  nodeTypes,
  useReducedMotion,
  type ActivityNode,
} from "@/components/activity-graph";
import { Button } from "@/components/ui/button";
import type { PendingAction } from "@/dashboard/demo";
import { layoutGraph, NODE_HEIGHT, NODE_WIDTH } from "@/dashboard/graph-layout";
import type { Graph } from "@/graph/protocol";
import type { GraphStreamStatus } from "@/graph/useGraphStream";
import { cn } from "@/lib/utils";

type GraphPanelProps = {
  graph: Graph;
  pending: PendingAction | null;
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
  onRestart?: () => void;
  onTrigger?: () => void;
  status?: GraphStreamStatus | null;
};

const STATUS_PILL: Record<GraphStreamStatus, { dot: string; text: string }> = {
  live: { dot: "bg-[#027a48] animate-pulse", text: "LIVE" },
  connecting: { dot: "bg-[#1447e6]", text: "CONNECTING" },
  reconnecting: { dot: "bg-[#b06a38]", text: "RECONNECTING" },
};

export function GraphPanel({
  graph,
  pending,
  selectedNodeId,
  onSelectNode,
  onRestart,
  onTrigger,
  status = null,
}: GraphPanelProps) {
  const reducedMotion = useReducedMotion();

  const layout = useMemo(() => layoutGraph(graph, pending), [graph, pending]);

  const seen = useRef(new Set<string>());
  const fresh = useMemo(
    () =>
      new Set(
        layout.nodes
          .filter((item) => !seen.current.has(item.id))
          .map((item) => item.id),
      ),
    [layout.nodes],
  );
  useEffect(() => {
    for (const id of fresh) seen.current.add(id);
  }, [fresh]);

  const nodes = useMemo<ActivityNode[]>(
    () =>
      layout.nodes.map((item) => ({
        id: item.id,
        type: "activity",
        position: item.position,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        selected: item.id === selectedNodeId,
        className: fresh.has(item.id) ? "live-node-new" : undefined,
        data: { item, reducedMotion, onSelect: onSelectNode },
      })),
    [layout.nodes, fresh, selectedNodeId, reducedMotion, onSelectNode],
  );
  const edges = useMemo<Edge[]>(() => {
    return layout.links.map((link) => ({
      id: link.id,
      source: link.source,
      target: link.target,
      type: link.pending && !reducedMotion ? "activity" : "default",
      data: { duration: 3, path: "bezier" },
      style: {
        stroke: link.pending ? "#a8bbef" : "#c8c3bc",
        strokeWidth: 1.5,
        strokeDasharray: link.pending ? "4 4" : undefined,
      },
      selectable: false,
      focusable: false,
    }));
  }, [layout.links, reducedMotion]);
  const nodeKey = JSON.stringify(layout.nodes.map((node) => node.id));
  const pill = status === null ? null : STATUS_PILL[status];

  return (
    <section
      className="graph-panel"
      aria-label="Agent activity graph"
      style={{
        height: "100%",
        minHeight: 0,
        display: "flex",
        flexDirection: "column",
        background: "#fcfcfc",
      }}
    >
      <header className="flex h-10 shrink-0 items-center justify-between gap-3 border-b px-4">
        <h2 className="text-sm font-semibold text-zinc-900">Agent activity</h2>
        <div className="flex items-center gap-2">
          {pill && (
            <span
              aria-label="Stream status"
              className="flex items-center gap-1.5 rounded-full border border-zinc-200 px-2.5 py-0.5 font-mono text-[10px] tracking-widest text-zinc-600"
            >
              <span
                aria-hidden="true"
                className={cn(
                  "inline-block h-1.5 w-1.5 rounded-full",
                  pill.dot,
                )}
              />
              {pill.text}
            </span>
          )}
          {onTrigger && (
            <Button
              variant="default"
              size="sm"
              aria-label="Trigger a live demo run"
              onClick={onTrigger}
            >
              <Play aria-hidden="true" />
              Trigger run
            </Button>
          )}
          {onRestart && (
            <Button
              variant="outline"
              size="sm"
              aria-label="Restart test run"
              onClick={onRestart}
            >
              <RotateCcw aria-hidden="true" />
              Restart test run
            </Button>
          )}
        </div>
      </header>
      <div style={{ flex: 1, minHeight: 220, position: "relative" }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          nodesDraggable={false}
          nodesConnectable={false}
          nodesFocusable={false}
          edgesFocusable={false}
          elementsSelectable={false}
          onNodeClick={(_, node) => onSelectNode(node.id)}
          deleteKeyCode={null}
          minZoom={0.25}
          maxZoom={1.8}
          style={{ background: "#fcfcfc", color: "#4e81d1" }}
          aria-label="Read-only agent activity. Select a node for details; drag the canvas to pan."
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={20}
            size={1}
            color="#e9edf2"
          />
          <GraphControls />
          <FollowViewport nodeKey={nodeKey} reducedMotion={reducedMotion} />
        </ReactFlow>
        {nodes.length === 0 && (
          <div
            role="status"
            style={{
              position: "absolute",
              inset: 0,
              display: "grid",
              placeContent: "center",
              textAlign: "center",
              pointerEvents: "none",
              color: "#7b8492",
              fontSize: 12,
            }}
          >
            <Workflow
              size={26}
              aria-hidden="true"
              style={{ margin: "0 auto 12px", color: "#a5afbd" }}
            />
            <strong style={{ color: "#4a5565", fontWeight: 550 }}>
              Waiting for agent activity
            </strong>
            <span style={{ marginTop: 6 }}>
              Classified actions will appear here.
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
