import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown } from "lucide-react";
import { useId, useState } from "react";

import { Badge } from "@/components/ui/badge";

type LogLevel = "info" | "warning" | "error";

export interface Log {
  id: string;
  timestamp: string;
  level: LogLevel;
  service: string;
  message: string;
  duration: string;
  status: string;
  tags: string[];
}

const levelStyles: Record<LogLevel, string> = {
  info: "bg-[#ece8e3] text-[#4a443c]",
  warning: "bg-[#ebe6d2] text-[#7a4a24]",
  error: "bg-[#f6e6e4] text-[#b42318]",
};

const rowStyles: Record<LogLevel, string> = {
  info: "bg-[#fcfcfc] hover:bg-[#f2f2f2]",
  warning: "bg-[#f7f4e8] hover:bg-[#ebe6d2]",
  error: "bg-[#f9ecea] hover:bg-[#f6e6e4]",
};

function LogRow({
  log,
  expanded,
  onToggle,
}: {
  log: Log;
  expanded: boolean;
  onToggle: () => void;
}) {
  const detailsId = useId();
  const formattedTime = new Date(log.timestamp).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone: "UTC",
  });

  return (
    <>
      <motion.button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        aria-controls={detailsId}
        className={`grid w-full min-w-0 grid-cols-[4rem_3.5rem_minmax(0,1fr)_1rem] items-center gap-2 px-4 py-1.5 text-left transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500 ${rowStyles[log.level]}`}
      >
        <time
          dateTime={log.timestamp}
          title={`${log.timestamp} (UTC)`}
          className="font-mono text-[10px] tabular-nums text-zinc-500"
        >
          {formattedTime}
        </time>
        <Badge
          variant="secondary"
          className={`justify-center px-1 text-[10px] capitalize ${levelStyles[log.level]}`}
        >
          {log.level}
        </Badge>
        <span className="min-w-0">
          <span className="block truncate text-xs text-zinc-800">
            {log.message}
          </span>
          <span className="mt-0.5 block truncate text-[10px] text-zinc-500">
            {log.service}
          </span>
        </span>
        <ChevronDown
          aria-hidden="true"
          className={`size-3.5 text-zinc-400 transition-transform ${expanded ? "rotate-180" : ""}`}
        />
      </motion.button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            id={detailsId}
            key="details"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden border-t border-zinc-100 bg-[#f2f2f2]"
          >
            <div className="space-y-3 p-4 text-xs">
              <p className="whitespace-pre-wrap break-words font-mono text-zinc-800 [overflow-wrap:anywhere]">
                {log.message}
              </p>
              <dl className="grid grid-cols-2 gap-3 text-zinc-600">
                <div className="min-w-0">
                  <dt className="text-zinc-400">Source</dt>
                  <dd className="break-words">{log.service}</dd>
                </div>
                <div className="min-w-0">
                  <dt className="text-zinc-400">Status / duration</dt>
                  <dd className="break-words">
                    {log.status} / {log.duration}
                  </dd>
                </div>
                <div className="col-span-2 min-w-0">
                  <dt className="text-zinc-400">Timestamp (UTC)</dt>
                  <dd className="break-words font-mono">{log.timestamp}</dd>
                </div>
              </dl>
              <div className="flex flex-wrap gap-1.5" aria-label="Log tags">
                {log.tags.map((tag, index) => (
                  <Badge
                    key={`${tag}-${index}`}
                    variant="outline"
                    className="max-w-full whitespace-normal break-all text-[10px]"
                  >
                    {tag}
                  </Badge>
                ))}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

export function InteractiveLogsTable({ logs }: { logs: Log[] }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  return (
    <section
      aria-label="Container logs"
      className="flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden bg-[#fcfcfc]"
    >
      <header className="flex h-10 shrink-0 items-center border-b border-zinc-200 px-4">
        <h2 className="text-sm font-semibold text-zinc-900">Container logs</h2>
      </header>
      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <div className="divide-y divide-zinc-100">
          {logs.map((log) => (
            <div key={log.id}>
              <LogRow
                log={log}
                expanded={expandedId === log.id}
                onToggle={() =>
                  setExpandedId((current) =>
                    current === log.id ? null : log.id,
                  )
                }
              />
            </div>
          ))}
        </div>
        {logs.length === 0 && (
          <p
            role="status"
            className="px-4 py-12 text-center text-xs text-zinc-500"
          >
            Waiting for container logs.
          </p>
        )}
      </div>
    </section>
  );
}
