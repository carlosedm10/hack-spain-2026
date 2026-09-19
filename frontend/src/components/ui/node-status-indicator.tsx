import type { ReactNode } from "react";

export type NodeStatus = "loading" | "success" | "error" | "initial";

export type NodeStatusIndicatorProps = {
  status?: NodeStatus;
  children: ReactNode;
};

const statusClasses: Record<NodeStatus, string> = {
  loading: "ring-2 ring-[#1447e6]",
  success: "ring-2 ring-[#027a48]",
  error: "ring-2 ring-[#d9584b]",
  initial: "",
};

export function NodeStatusIndicator({
  status = "initial",
  children,
}: NodeStatusIndicatorProps) {
  return (
    <div className={`rounded-[9px] ${statusClasses[status]}`}>{children}</div>
  );
}
