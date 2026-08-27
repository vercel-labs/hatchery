"use client";

import "@xterm/xterm/css/xterm.css";

import { PlusIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiBase, wsBase } from "@/lib/api";

export type CoderTask = {
  id: string;
  devbox_id: string;
  title: string;
  task_id?: string;
  session_id?: string;
  state: string;
  created_at: string;
};

export type ManualTerminal = {
  id: string;
  devbox_id: string;
  title: string;
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
  terminals: ManualTerminal[];
};

type TerminalTab =
  | (CoderTask & { kind: "subagent" })
  | (ManualTerminal & { kind: "manual" });

const b64encode = (s: string) =>
  btoa(String.fromCharCode(...new TextEncoder().encode(s)));

function tabs(box: DevboxWorkspace | undefined): TerminalTab[] {
  if (!box) return [];
  return [
    ...box.subagents.map((task) => ({ ...task, kind: "subagent" as const })),
    ...box.terminals.map((terminal) => ({ ...terminal, kind: "manual" as const })),
  ].sort((left, right) => left.created_at.localeCompare(right.created_at));
}

function TaskTerminal({ chatId, tab }: { chatId: string; tab: TerminalTab }) {
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
        const path =
          tab.kind === "manual"
            ? `terminals/${tab.id}`
            : `subagents/${tab.id}`;
        ws = new WebSocket(
          `${wsBase()}/api/chats/${chatId}/${path}/tty?offset=${offset}&cols=${term.cols}&rows=${term.rows}`,
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
  }, [chatId, tab.id, tab.kind]);

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
          {tab.title} — {status}
        </span>
      </div>
      <div ref={hostRef} className="min-h-0 flex-1 bg-[#0a0a0a] p-2" />
    </>
  );
}

export function TerminalPane({
  chatId,
  devboxes,
  preferredDevboxId,
  onClose,
  onCreateSandbox,
  onChanged,
}: {
  chatId: string;
  devboxes: DevboxWorkspace[];
  preferredDevboxId?: string;
  onClose: () => void;
  onCreateSandbox: () => void;
  onChanged: () => void;
}) {
  const latest =
    devboxes.find((box) => box.id === preferredDevboxId) ??
    devboxes.findLast((box) => tabs(box).length) ??
    devboxes.at(-1);
  const [selectedDevboxId, setSelectedDevboxId] = useState(latest?.id ?? "");
  const [selectedTabId, setSelectedTabId] = useState(tabs(latest).at(-1)?.id ?? "");
  const [creating, setCreating] = useState(false);
  const activeDevbox =
    devboxes.find((box) => box.id === selectedDevboxId) ?? latest;
  const activeTabs = tabs(activeDevbox);
  const activeTab =
    activeTabs.find((tab) => tab.id === selectedTabId) ?? activeTabs.at(-1);

  const selectDevbox = (box: DevboxWorkspace) => {
    setSelectedDevboxId(box.id);
    setSelectedTabId(tabs(box).at(-1)?.id ?? "");
  };

  const createTerminal = async () => {
    if (!activeDevbox) return;
    setCreating(true);
    try {
      const response = await fetch(
        `${apiBase()}/api/chats/${chatId}/devboxes/${activeDevbox.id}/terminals`,
        { method: "POST" },
      );
      if (!response.ok) return;
      const terminal: ManualTerminal = await response.json();
      setSelectedTabId(terminal.id);
      onChanged();
    } finally {
      setCreating(false);
    }
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
          <PlusIcon />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto size-7 shrink-0"
          aria-label="Close devboxes"
          onClick={onClose}
        >
          <XIcon />
        </Button>
      </div>
      <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {activeTabs.map((tab, index) => (
          <Button
            key={tab.id}
            variant={tab.id === activeTab?.id ? "secondary" : "ghost"}
            size="xs"
            className="max-w-40 shrink-0"
            onClick={() => setSelectedTabId(tab.id)}
          >
            <span className="truncate">
              {tab.title || `${tab.kind === "manual" ? "bash" : "subagent"} ${index + 1}`}
            </span>
          </Button>
        ))}
        <Button
          variant="ghost"
          size="icon-xs"
          className="shrink-0"
          aria-label="New bash terminal"
          disabled={creating || activeDevbox?.state !== "ready"}
          onClick={createTerminal}
        >
          <PlusIcon />
        </Button>
      </div>
      {activeTab ? (
        <TaskTerminal key={activeTab.id} chatId={chatId} tab={activeTab} />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
          {activeDevbox?.error || "Devbox ready"}
        </div>
      )}
    </div>
  );
}
