import { useEffect, useState } from "react";
import {
  Controls,
  ControlButton,
  Handle,
  Position,
  useReactFlow,
  useStore,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import {
  Check,
  GitBranch,
  LoaderCircle,
  Maximize2,
  Minus,
  Plus,
  Terminal,
  Workflow,
} from "lucide-react";

import { AnimatedSvgEdge } from "@/components/ui/animated-svg-edge";
import { BaseNode } from "@/components/ui/base-node";
import { NodeStatusIndicator } from "@/components/ui/node-status-indicator";
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  type ActivityItem,
} from "@/dashboard/graph-layout";

export type ActivityNode = Node<
  {
    item: ActivityItem;
    reducedMotion: boolean;
    onSelect: (id: string) => void;
  },
  "activity"
>;

const levels = [
  { label: "Benign", color: "#027a48", background: "#dde8d8" },
  { label: "Mild", color: "#b06a38", background: "#ebe6d2" },
  { label: "Moderate", color: "#b06a38", background: "#ebe6d2" },
  { label: "Severe", color: "#d9584b", background: "#f6e6e4" },
  { label: "Critical", color: "#d12a2a", background: "#f6e6e4" },
  { label: "Catastrophic", color: "#b42318", background: "#f0d3cf" },
];

function ActivityCard({ data, selected }: NodeProps<ActivityNode>) {
  const { item, reducedMotion, onSelect } = data;
  const verdict = item.kind === "classified" ? levels[item.level] : undefined;
  const awaiting = item.kind === "pending";
  const structural = item.kind === "structure";
  const color = awaiting ? "#1447e6" : (verdict?.color ?? "#736f6a");
  const status = awaiting
    ? "Awaiting Jev"
    : structural
      ? item.runId
        ? "Agent run"
        : "Graph root"
      : `L${item.kind === "classified" ? item.level : ""} · ${verdict?.label ?? "Unrecognized level"}`;
  const Icon = awaiting
    ? LoaderCircle
    : structural
      ? item.runId
        ? GitBranch
        : Workflow
      : item.kind === "classified" && item.level === 0
        ? Check
        : Terminal;

  return (
    <NodeStatusIndicator status="initial">
      <BaseNode
        role="button"
        aria-label={`${item.label}, ${status}${item.tool ? `, ${item.tool}` : ""}`}
        aria-pressed={selected}
        className="activity-node nodrag nopan focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-blue-600"
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            event.stopPropagation();
            onSelect(item.id);
          }
        }}
        style={{
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          borderRadius: 12,
          padding: "12px 14px",
          background: verdict?.background ?? "#fcfcfc",
          border: `1px solid ${selected ? color : awaiting ? "#c3ccf0" : verdict ? `${color}55` : "#dad5cc"}`,
          boxShadow: selected
            ? `0 0 0 2px ${color}25, 0 5px 16px #172b4d10`
            : "0 2px 6px #172b4d06",
          cursor: "pointer",
          color: "#1a1614",
        }}
      >
        <Handle
          type="target"
          position={Position.Top}
          isConnectable={false}
          style={{ opacity: 0 }}
        />
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            color,
            fontSize: 10,
            fontWeight: 650,
            marginBottom: 9,
          }}
        >
          <Icon
            size={13}
            aria-hidden="true"
            className={awaiting && !reducedMotion ? "animate-spin" : undefined}
          />
          <span>{status}</span>
          {item.kind === "classified" && Number.isFinite(item.confidence) && (
            <span
              title="Jev confidence, independent of severity level"
              style={{ marginLeft: "auto", color: "#736f6a", fontWeight: 500 }}
            >
              {Math.round(item.confidence * 100)}%
            </span>
          )}
        </div>
        <div
          title={item.label}
          style={{
            fontSize: 13,
            fontWeight: 650,
            lineHeight: "18px",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {item.label}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            marginTop: 8,
            color: "#736f6a",
            fontSize: 10,
          }}
        >
          {!structural && <Terminal size={11} aria-hidden="true" />}
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {item.tool || (structural ? "Unclassified structure" : item.runId)}
          </span>

        </div>
        <Handle
          type="source"
          position={Position.Bottom}
          isConnectable={false}
          style={{ opacity: 0 }}
        />
      </BaseNode>
    </NodeStatusIndicator>
  );
}

export const nodeTypes = { activity: ActivityCard };
export const edgeTypes = { activity: AnimatedSvgEdge };

export function GraphControls() {
  const { zoomIn, zoomOut, fitBounds, getNodesBounds, getNodes } =
    useReactFlow();
  return (
    <Controls
      position="bottom-right"
      orientation="horizontal"
      showZoom={false}
      showFitView={false}
      showInteractive={false}
    >
      <ControlButton
        title="Zoom in"
        aria-label="Zoom in"
        onClick={() => void zoomIn()}
        style={{ width: 38, height: 38 }}
      >
        <Plus aria-hidden="true" />
      </ControlButton>
      <ControlButton
        title="Zoom out"
        aria-label="Zoom out"
        onClick={() => void zoomOut()}
        style={{ width: 38, height: 38 }}
      >
        <Minus aria-hidden="true" />
      </ControlButton>
      <ControlButton
        title="Fit graph to view"
        aria-label="Fit graph to view"
        onClick={() =>
          void fitBounds(getNodesBounds(getNodes()), { padding: 0.18 })
        }
        style={{ width: 38, height: 38 }}
      >
        <Maximize2 aria-hidden="true" />
      </ControlButton>
    </Controls>
  );
}

export function FollowViewport({
  nodeKey,
  reducedMotion,
}: {
  nodeKey: string;
  reducedMotion: boolean;
}) {
  const width = useStore((state) => state.width);
  const height = useStore((state) => state.height);
  const { fitBounds, getNodesBounds, getNodes } = useReactFlow();
  useEffect(() => {
    if (!width || !height) return;
    const frame = requestAnimationFrame(() => {
      void fitBounds(getNodesBounds(getNodes()), {
        padding: 0.18,
        duration: reducedMotion ? 0 : 250,
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [
    nodeKey,
    reducedMotion,
    fitBounds,
    getNodesBounds,
    getNodes,
    width,
    height,
  ]);
  return null;
}

export function useReducedMotion() {
  const [reducedMotion, setReducedMotion] = useState(
    () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return reducedMotion;
}
