import { useEffect, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  ReactFlow,
  type Edge,
} from "@xyflow/react";
import { Workflow } from "lucide-react";
import "@xyflow/react/dist/style.css";

import {
  edgeTypes,
  FollowViewport,
  GraphControls,
  nodeTypes,
  useReducedMotion,
  type ActivityNode,
} from "@/components/activity-graph";
import { layoutGraph, NODE_HEIGHT, NODE_WIDTH } from "@/dashboard/graph-layout";
import type { Graph } from "@/graph/protocol";
import type { GraphStreamStatus } from "@/graph/useGraphStream";
import { LEVEL_STROKE } from "@/live/path";

const noop = () => {};

const STATUS_PILL: Record<GraphStreamStatus, { dot: string; text: string }> = {
  live: { dot: "bg-emerald-400 animate-pulse", text: "LIVE" },
  connecting: { dot: "bg-sky-400", text: "CONNECTING" },
  reconnecting: { dot: "bg-amber-400", text: "RECONNECTING" },
};

export type LiveGraphProps = {
  graph: Graph | null;
  status: GraphStreamStatus;
};

export function LiveGraph({ graph, status }: LiveGraphProps) {
  const reducedMotion = useReducedMotion();
  const layout = useMemo(
    () =>
      graph === null ? { nodes: [], links: [] } : layoutGraph(graph, null),
    [graph],
  );

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

  const nodes = useMemo<ActivityNode[]>(
    () =>
      layout.nodes.map((item) => ({
        id: item.id,
        type: "activity",
        position: item.position,
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        className: fresh.has(item.id) ? "live-node-new" : undefined,
        data: { item, reducedMotion, onSelect: noop },
      })),
    [layout.nodes, fresh, reducedMotion],
  );

  useEffect(() => {
    for (const id of fresh) seen.current.add(id);
  }, [fresh]);

  const edges = useMemo<Edge[]>(() => {
    return layout.links.map((link) => ({
      id: link.id,
      source: link.source,
      target: link.target,
      type: reducedMotion ? "default" : "activity",
      data: { duration: 3, path: "bezier" },
      style: { stroke: "#4a5568", strokeWidth: 1.5 },
      selectable: false,
      focusable: false,
    }));
  }, [layout.links, reducedMotion]);

  const nodeKey = JSON.stringify(layout.nodes.map((node) => node.id));
  const maxLevel = layout.nodes.reduce(
    (max, item) =>
      item.kind === "classified" ? Math.max(max, item.level) : max,
    0,
  );
  const pill = STATUS_PILL[status];

  return (
    <div
      className="relative h-dvh w-dvw overflow-hidden"
      style={{ background: "#070b14" }}
    >
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
        deleteKeyCode={null}
        minZoom={0.2}
        maxZoom={1.8}
        style={{ background: "#070b14", color: "#8ab4f8" }}
        aria-label="Live agent activity graph. Drag the canvas to pan."
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={22}
          size={1}
          color="#1c2536"
        />
        <GraphControls />
        <FollowViewport nodeKey={nodeKey} reducedMotion={reducedMotion} />
      </ReactFlow>
      <div
        aria-label="Stream status"
        className="pointer-events-none absolute left-4 top-4 flex items-center gap-2 font-mono text-[11px] tracking-widest"
      >
        <span className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1 text-zinc-200 backdrop-blur">
          <span
            aria-hidden="true"
            className={`inline-block h-1.5 w-1.5 rounded-full ${pill.dot}`}
          />
          {pill.text}
        </span>
        <span
          className={`rounded-full border border-white/10 px-3 py-1 text-zinc-950 backdrop-blur ${maxLevel >= 3 ? "animate-pulse" : ""}`}
          style={{
            background:
              LEVEL_STROKE[Math.min(maxLevel, LEVEL_STROKE.length - 1)],
          }}
        >
          L{maxLevel}
        </span>
      </div>
      {nodes.length === 0 && (
        <div
          role="status"
          className="pointer-events-none absolute inset-0 grid place-content-center text-center text-zinc-500"
        >
          <Workflow
            size={26}
            aria-hidden="true"
            style={{ margin: "0 auto 12px", color: "#33415c" }}
          />
          <strong className="font-medium text-zinc-400">
            Waiting for agent activity
          </strong>
          <span className="mt-1.5 text-xs">
            Classified actions will appear here.
          </span>
        </div>
      )}
    </div>
  );
}
