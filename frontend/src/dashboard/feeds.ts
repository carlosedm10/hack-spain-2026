import type { Log } from "@/components/ui/interactive-logs-table";
import type { SafeAction } from "@/dashboard/demo";
import { eventText } from "@/dashboard/graph-layout";
import type { Graph, GraphNode } from "@/graph/protocol";
import { ACTION_STATUS_LABEL, CALL_LABEL } from "@/ladder/copy";
import type { ActionTransition, IncidentState } from "@/ladder/types";
import { actionModeLabel } from "@/ladder/wallboard";

function classified(graph: Graph): GraphNode[] {
  return Array.from(graph.nodes.values())
    .filter((node) => node.event !== null && node.run_id !== null)
    .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""));
}

export function actionsFromIncident(incident: IncidentState): SafeAction[] {
  const latest = new Map<string, ActionTransition>();
  for (const action of incident.actions) latest.set(action.action_id, action);
  return [...latest.values()].map((action) => ({
    id: action.action_id,
    nodeId: `run:${incident.incident_id}`,
    title: action.name,
    source: action.ladder_level === null ? "HappyRobot" : "Host playbook",
    status:
      action.status === "queued" || action.status === "running"
        ? "running"
        : action.status === "failed" || action.status === "canceled"
          ? "failed"
          : "done",
    level: action.level,
    startedAt: action.timestamp,
    details: [
      { label: "Status", meta: ACTION_STATUS_LABEL[action.status] },
      { label: "Mode", meta: actionModeLabel(action.mode) },
      ...(action.ladder_level === null
        ? []
        : [{ label: "Ladder level", meta: `L${action.ladder_level}` }]),
      ...(action.call_status === null
        ? []
        : [{ label: "Call", meta: CALL_LABEL[action.call_status] }]),
      ...(action.detail === null ? [] : [{ label: "Detail", meta: action.detail }]),
      ...(action.error_code === null
        ? []
        : [{ label: "Error", meta: action.error_code }]),
    ],
  }));
}

export function logsFromGraph(graph: Graph): Log[] {
  return classified(graph).map((node) => ({
    id: `log-${node.id}`,
    timestamp: node.created_at ?? "",
    level: node.level >= 3 ? "error" : node.level >= 1 ? "warning" : "info",
    service: `run:${node.run_id}`,
    message: eventText(node, ["content", "label", "kind", "event"], node.id),
    duration: "—",
    status: `L${node.level}`,
    tags: [node.run_id ?? "", node.id, node.intent ?? ""].filter(Boolean),
  }));
}
