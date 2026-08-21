"use client";

import "@xterm/xterm/css/xterm.css";

import { XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { wsBase } from "@/lib/api";

export type CoderTask = {
  id: string;
  title: string;
  task_id?: string;
  session_id?: string;
  state: string;
  created_at: string;
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
          `${wsBase()}/api/chats/${chatId}/tasks/${task.id}/tty?offset=${offset}&cols=${term.cols}&rows=${term.rows}`,
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
  tasks,
  onClose,
}: {
  chatId: string;
  tasks: CoderTask[];
  onClose: () => void;
}) {
  const [selectedId, setSelectedId] = useState(tasks.at(-1)?.id ?? "");
  const active =
    tasks.find((task) => task.id === selectedId) ?? tasks.at(-1);

  return (
    <div className="flex h-2/5 min-w-0 flex-none flex-col border-t @5xl:h-auto @5xl:flex-1 @5xl:border-t-0 @5xl:border-l">
      <div className="flex h-10 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {tasks.map((task, index) => (
          <Button
            key={task.id}
            variant={task.id === active?.id ? "secondary" : "ghost"}
            size="sm"
            className="max-w-40 shrink-0"
            onClick={() => setSelectedId(task.id)}
          >
            <span className="truncate">{task.title || `coder ${index + 1}`}</span>
          </Button>
        ))}
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto size-7 shrink-0"
          aria-label="Close terminals"
          onClick={onClose}
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      {active && <TaskTerminal key={active.id} chatId={chatId} task={active} />}
    </div>
  );
}
