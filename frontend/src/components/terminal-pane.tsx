"use client";

import "@xterm/xterm/css/xterm.css";

import { PlusIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { wsBase } from "@/lib/api";

export type CoderTask = {
  id: string;
  devbox_id: string;
  title: string;
  task_id?: string;
  session_id?: string;
  state: string;
  created_at: string;
};

export type DevboxWorkspace = {
  id: string;
  title: string;
  repos: string[];
  state: string;
  error?: string;
  created_at: string;
  subagents: CoderTask[];
};

const b64encode = (s: string) =>
  btoa(String.fromCharCode(...new TextEncoder().encode(s)));

function TaskTerminal({ chatId, task }: { chatId: string; task: CoderTask }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"connecting" | "live" | "exited">(
    "connecting",
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let offset = 0;
    let exited = false;

    const boot = async () => {
      const [{ Terminal }, { FitAddon }] = await Promise.all([
        import("@xterm/xterm"),
        import("@xterm/addon-fit"),
      ]);
      if (disposed) return;
      const fontFamily =
        `${getComputedStyle(host).getPropertyValue("--font-geist-mono").trim() || "ui-monospace"}, monospace`;
      await document.fonts.load(`12px ${fontFamily}`).catch(() => {});
      if (disposed) return;

      const term = new Terminal({
        fontSize: 12,
        fontFamily,
        theme: { background: "#0a0a0a" },
        scrollback: 10000,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(host);
      fit.fit();

      const send = (type: string, body: unknown) => {
        if (ws?.readyState === WebSocket.OPEN)
          ws.send(JSON.stringify({ type, body }));
      };
      term.onData((data) => send("tty-input", { data: b64encode(data) }));
      const observer = new ResizeObserver(() => {
        const dims = fit.proposeDimensions();
        if (!dims || (dims.cols === term.cols && dims.rows === term.rows)) return;
        fit.fit();
        send("resize", { cols: term.cols, rows: term.rows });
      });
      observer.observe(host);

      const connect = () => {
        if (disposed || exited) return;
        ws = new WebSocket(
          `${wsBase()}/api/chats/${chatId}/subagents/${task.id}/tty?offset=${offset}&cols=${term.cols}&rows=${term.rows}`,
        );
        ws.onmessage = (event) => {
          const frame = JSON.parse(event.data);
          if (frame.type === "handshake") {
            offset = frame.body.offset;
            setStatus("live");
          } else if (frame.type === "tty-output") {
            const bytes = Uint8Array.from(atob(frame.body.data), (c) =>
              c.charCodeAt(0),
            );
            offset += bytes.length;
            term.write(bytes);
          } else if (frame.type === "exit") {
            exited = true;
            setStatus("exited");
            term.write(`\r\n[session exited: ${frame.body.code}]\r\n`);
          }
        };
        ws.onclose = () => {
          if (disposed || exited) return;
          setStatus("connecting");
          retry = setTimeout(connect, 1500);
        };
      };
      connect();

      return () => {
        observer.disconnect();
        term.dispose();
      };
    };

    const cleanup = boot();
    return () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      ws?.close();
      void cleanup.then((dispose) => dispose?.());
    };
  }, [chatId, task.id]);

  return (
    <>
      <div className="flex h-8 shrink-0 items-center gap-2 border-b px-3">
        <span
          className={
            status === "live"
              ? "size-2 rounded-full bg-emerald-500"
              : status === "exited"
                ? "size-2 rounded-full bg-muted-foreground"
                : "size-2 animate-pulse rounded-full bg-amber-500"
          }
        />
        <span className="truncate text-xs text-muted-foreground">
          {task.title} — {status}
        </span>
      </div>
      <div ref={hostRef} className="min-h-0 flex-1 bg-[#0a0a0a] p-2" />
    </>
  );
}

export function TerminalPane({
  chatId,
  devboxes,
  onClose,
  onCreateSandbox,
}: {
  chatId: string;
  devboxes: DevboxWorkspace[];
  onClose: () => void;
  onCreateSandbox: () => void;
}) {
  const latest = devboxes.findLast((box) => box.subagents.length) ?? devboxes.at(-1);
  const [selectedDevboxId, setSelectedDevboxId] = useState(latest?.id ?? "");
  const [selectedTaskId, setSelectedTaskId] = useState(
    latest?.subagents.at(-1)?.id ?? "",
  );
  const activeDevbox =
    devboxes.find((box) => box.id === selectedDevboxId) ?? latest;
  const activeTask =
    activeDevbox?.subagents.find((task) => task.id === selectedTaskId) ??
    activeDevbox?.subagents.at(-1);

  const selectDevbox = (box: DevboxWorkspace) => {
    setSelectedDevboxId(box.id);
    setSelectedTaskId(box.subagents.at(-1)?.id ?? "");
  };

  return (
    <div className="flex h-2/5 min-w-0 flex-none flex-col border-t @4xl:h-auto @4xl:min-w-[28rem] @4xl:flex-1 @4xl:border-t-0 @4xl:border-l">
      <div className="flex h-10 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {devboxes.map((box, index) => (
          <Button
            key={box.id}
            variant={box.id === activeDevbox?.id ? "secondary" : "ghost"}
            size="sm"
            className="max-w-40 shrink-0"
            onClick={() => selectDevbox(box)}
          >
            <span className="truncate">{box.title || `devbox ${index + 1}`}</span>
          </Button>
        ))}
        <Button
          variant="ghost"
          size="icon"
          className="size-7 shrink-0"
          aria-label="Create sandbox"
          onClick={onCreateSandbox}
        >
          <PlusIcon className="size-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto size-7 shrink-0"
          aria-label="Close devboxes"
          onClick={onClose}
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {activeDevbox?.subagents.map((task, index) => (
          <Button
            key={task.id}
            variant={task.id === activeTask?.id ? "secondary" : "ghost"}
            size="xs"
            className="max-w-40 shrink-0"
            onClick={() => setSelectedTaskId(task.id)}
          >
            <span className="truncate">{task.title || `subagent ${index + 1}`}</span>
          </Button>
        ))}
        {!activeDevbox?.subagents.length && (
          <span className="px-2 text-xs text-muted-foreground">
            No subagents in this devbox
          </span>
        )}
      </div>
      {activeTask ? (
        <TaskTerminal key={activeTask.id} chatId={chatId} task={activeTask} />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
          {activeDevbox?.error || "Devbox ready"}
        </div>
      )}
    </div>
  );
}
