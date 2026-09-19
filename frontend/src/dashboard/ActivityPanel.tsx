import TaskRows from "@/components/ui/task-rows";
import type { SafeAction } from "@/dashboard/demo";

export function ActivityPanel({
  actions,
  selectedNodeId,
  onSelectNode,
}: {
  actions: SafeAction[];
  selectedNodeId: string | null;
  onSelectNode: (id: string) => void;
}) {
  return (
    <section
      aria-label="Protective actions"
      className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-xl border bg-[#fcfcfc] shadow-xs"
    >
      <header className="flex h-10 shrink-0 items-center border-b border-zinc-200 px-4">
        <h2 className="text-sm font-semibold text-zinc-900">
          Protective actions
        </h2>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {actions.length > 0 ? (
          <TaskRows
            variant="List"
            rows={actions.map((action) => ({
              key: action.id,
              label: action.title,
              amount: `${action.source} · L${action.level} · ${new Date(action.startedAt).toLocaleTimeString("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit" })} UTC`,
              status: action.status,
              selected: action.nodeId === selectedNodeId,
              details: [
                { label: "Graph node", meta: action.nodeId },
                ...action.details,
              ],
            }))}
            onToggleRow={(id, open) => {
              const action = actions.find((entry) => entry.id === id);
              if (open && action) onSelectNode(action.nodeId);
            }}
          />
        ) : (
          <div role="status" className="px-4 py-10 text-center">
            <p className="text-xs font-medium text-zinc-600">Waiting for Jev</p>
          </div>
        )}
      </div>
    </section>
  );
}
