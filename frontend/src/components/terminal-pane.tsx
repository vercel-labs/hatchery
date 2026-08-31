"use client";

import "@xterm/xterm/css/xterm.css";

import { CheckIcon, CopyIcon, PlusIcon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { apiBase, wsBase } from "@/lib/api";

export type SubagentTask = {
  id: string;
  sandbox_id: string;
  title: string;
  task_id: string;
  session_id: string;
  fx_session_id?: string;
  status: string;
  created_at: string;
};

export type ManualTerminal = {
  id: string;
  sandbox_id: string;
  title: string;
  session_id?: string;
  status: string;
  created_at: string;
};

export type SandboxWorkspace = {
  id: string;
  sandbox_name: string;
  title: string;
  status: string;
  created_at: string;
  spec: { repos: string[] };
  routes: Array<{ port: number; url: string }>;
  subagents: SubagentTask[];
  terminals: ManualTerminal[];
};

type TerminalTab =
  | (SubagentTask & { kind: "subagent" })
  | (ManualTerminal & { kind: "manual" });

const b64encode = (s: string) =>
  btoa(String.fromCharCode(...new TextEncoder().encode(s)));

function tabs(box: SandboxWorkspace | undefined): TerminalTab[] {
  if (!box) return [];
  return [
    ...box.subagents.map((task) => ({
      ...task,
      title: task.title || "subagent",
      kind: "subagent" as const,
    })),
    ...box.terminals.map((terminal) => ({ ...terminal, kind: "manual" as const })),
  ].sort((left, right) => left.created_at.localeCompare(right.created_at));
}

function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Button
      variant="ghost"
      size="xs"
      className="min-w-0 max-w-full font-mono"
      title={value}
      onClick={() => void copy()}
    >
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="truncate">{value}</span>
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

function TaskTerminal({ chatId, tab }: { chatId: string; tab: TerminalTab }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<
    "waiting" | "connecting" | "live" | "exited" | "error"
  >(tab.kind === "subagent" && tab.status === "pending" ? "waiting" : "connecting");
  const [detail, setDetail] = useState("");

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let offset = 0;
    let exited = false;
    let sessionReady = tab.kind === "manual" || tab.status !== "pending";
    let waitingDetail = "waiting for sandbox queue";

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
        if (!sessionReady) {
          setStatus("waiting");
          setDetail(waitingDetail);
          retry = setTimeout(async () => {
            try {
              const response = await fetch(
                `${apiBase()}/api/chats/${chatId}/subagents/${tab.id}/readiness`,
              );
              if (response.ok) {
                const readiness = await response.json();
                sessionReady = readiness.session_ready;
                waitingDetail =
                  readiness.daemon?.queue_error ||
                  (readiness.daemon?.queue_connected === false
                    ? "sandbox queue is disconnected"
                    : "waiting for sandbox queue");
                setDetail(waitingDetail);
              }
            } catch {
              setDetail("readiness check failed");
            }
            connect();
          }, 1500);
          return;
        }
        setStatus("connecting");
        setDetail("");
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
            setDetail("");
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
        ws.onclose = (event) => {
          if (disposed || exited) return;
          const reason = event.reason || `connection closed (${event.code})`;
          setDetail(reason);
          if (event.code === 4404 || event.code === 4409) {
            setStatus("waiting");
          } else {
            setStatus("error");
          }
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
  }, [chatId, tab.id, tab.kind, tab.status]);

  return (
    <>
      <div className="flex h-8 shrink-0 items-center gap-2 border-b px-3">
        <span
          className={
            status === "live"
              ? "size-2 rounded-full bg-emerald-500"
              : status === "exited"
                ? "size-2 rounded-full bg-muted-foreground"
                : status === "error"
                  ? "size-2 rounded-full bg-destructive"
                  : "size-2 animate-pulse rounded-full bg-amber-500"
          }
        />
        <span className="truncate text-xs text-muted-foreground">
          {tab.title} — {status}{detail ? `: ${detail}` : ""}
        </span>
      </div>
      <div ref={hostRef} className="min-h-0 flex-1 bg-[#0a0a0a] p-2" />
    </>
  );
}

export function TerminalPane({
  chatId,
  sandboxes,
  preferredSandboxId,
  onClose,
  onCreateSandbox,
  onChanged,
}: {
  chatId: string;
  sandboxes: SandboxWorkspace[];
  preferredSandboxId?: string;
  onClose: () => void;
  onCreateSandbox: () => void;
  onChanged: () => void;
}) {
  const latest =
    sandboxes.find((box) => box.id === preferredSandboxId) ??
    sandboxes.findLast((box) => tabs(box).length) ??
    sandboxes.at(-1);
  const [selectedSandboxId, setSelectedSandboxId] = useState(latest?.id ?? "");
  const [selectedTabId, setSelectedTabId] = useState(tabs(latest).at(-1)?.id ?? "");
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState("");
  const activeSandbox =
    sandboxes.find((box) => box.id === selectedSandboxId) ?? latest;
  const activeTabs = tabs(activeSandbox);
  const activeTab =
    activeTabs.find((tab) => tab.id === selectedTabId) ?? activeTabs.at(-1);
  const deploymentUrl = apiBase() ||
    (typeof window === "undefined" ? "<deployment-url>" : window.location.origin);
  const sandboxCommand = activeSandbox
    ? `uv run --project backend python backend/diagnostics.py sandbox shell --url ${deploymentUrl} --chat ${chatId} --sandbox ${activeSandbox.id}`
    : "";
  const taskCommand = activeTab?.kind === "subagent"
    ? `uv run --project backend python backend/diagnostics.py task attach --url ${deploymentUrl} --chat ${chatId} --task ${activeTab.task_id}`
    : "";

  const selectSandbox = (box: SandboxWorkspace) => {
    setSelectedSandboxId(box.id);
    setSelectedTabId(tabs(box).at(-1)?.id ?? "");
  };

  const createTerminal = async () => {
    if (!activeSandbox) return;
    setCreating(true);
    try {
      const response = await fetch(
        `${apiBase()}/api/chats/${chatId}/sandboxes/${activeSandbox.id}/terminals`,
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

  const deleteTab = async (tab: TerminalTab) => {
    const label = tab.kind === "subagent" ? "Stop and delete this subagent?" : "Close this terminal?";
    if (!window.confirm(label)) return;
    setDeletingId(tab.id);
    try {
      const collection = tab.kind === "subagent" ? "subagents" : "terminals";
      const response = await fetch(
        `${apiBase()}/api/chats/${chatId}/${collection}/${tab.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) return;
      const remaining = activeTabs.filter((candidate) => candidate.id !== tab.id);
      setSelectedTabId(remaining.at(-1)?.id ?? "");
      onChanged();
    } finally {
      setDeletingId("");
    }
  };

  const deleteSandbox = async (box: SandboxWorkspace) => {
    if (!window.confirm(`Delete ${box.title || "this sandbox"} and stop everything in it?`)) return;
    setDeletingId(box.id);
    try {
      const response = await fetch(
        `${apiBase()}/api/chats/${chatId}/sandboxes/${box.id}`,
        { method: "DELETE" },
      );
      if (!response.ok) return;
      const remaining = sandboxes.filter((candidate) => candidate.id !== box.id);
      const next = remaining.at(-1);
      setSelectedSandboxId(next?.id ?? "");
      setSelectedTabId(tabs(next).at(-1)?.id ?? "");
      onChanged();
      if (!remaining.length) onClose();
    } finally {
      setDeletingId("");
    }
  };

  return (
    <div className="flex h-2/5 min-w-0 flex-none flex-col border-t @4xl:h-auto @4xl:min-w-[28rem] @4xl:flex-1 @4xl:border-t-0 @4xl:border-l">
      <div className="flex h-10 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {sandboxes.map((box, index) => (
          <div key={box.id} className="flex shrink-0 items-center">
            <Button
              variant={box.id === activeSandbox?.id ? "secondary" : "ghost"}
              size="sm"
              className="max-w-40 rounded-r-none"
              onClick={() => selectSandbox(box)}
            >
              <span className="truncate">{box.title || `sandbox ${index + 1}`}</span>
            </Button>
            <Button
              variant={box.id === activeSandbox?.id ? "secondary" : "ghost"}
              size="icon-sm"
              className="rounded-l-none"
              aria-label={`Delete ${box.title || `sandbox ${index + 1}`}`}
              disabled={deletingId === box.id || box.status === "creating"}
              onClick={() => void deleteSandbox(box)}
            >
              <XIcon />
            </Button>
          </div>
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
          aria-label="Close sandboxes"
          onClick={onClose}
        >
          <XIcon />
        </Button>
      </div>
      {activeSandbox && (
        <div className="flex shrink-0 flex-wrap items-center gap-1 border-b px-2 py-1">
          <CopyValue label="sandbox" value={activeSandbox.id} />
          <CopyValue label="vercel" value={activeSandbox.sandbox_name} />
          <CopyValue label="shell" value={sandboxCommand} />
        </div>
      )}
      <div className="flex h-9 shrink-0 items-center gap-1 overflow-x-auto border-b px-2">
        {activeTabs.map((tab, index) => (
          <div key={tab.id} className="flex shrink-0 items-center">
            <Button
              variant={tab.id === activeTab?.id ? "secondary" : "ghost"}
              size="xs"
              className="max-w-40 rounded-r-none"
              onClick={() => setSelectedTabId(tab.id)}
            >
              <span className="truncate">
                {tab.title || `${tab.kind === "manual" ? "bash" : "subagent"} ${index + 1}`}
              </span>
            </Button>
            <Button
              variant={tab.id === activeTab?.id ? "secondary" : "ghost"}
              size="icon-xs"
              className="rounded-l-none"
              aria-label={`Close ${tab.title || tab.kind}`}
              disabled={deletingId === tab.id}
              onClick={() => void deleteTab(tab)}
            >
              <XIcon />
            </Button>
          </div>
        ))}
        <Button
          variant="ghost"
          size="icon-xs"
          className="shrink-0"
          aria-label="New bash terminal"
          disabled={creating || activeSandbox?.status !== "running"}
          onClick={createTerminal}
        >
          <PlusIcon />
        </Button>
      </div>
      {activeTab?.kind === "subagent" && (
        <div className="flex shrink-0 flex-wrap items-center gap-1 border-b px-2 py-1">
          <CopyValue label="task" value={activeTab.task_id} />
          {activeTab.fx_session_id && (
            <CopyValue label="fx" value={activeTab.fx_session_id} />
          )}
          <CopyValue label="attach" value={taskCommand} />
        </div>
      )}
      {activeTab ? (
        <TaskTerminal key={activeTab.id} chatId={chatId} tab={activeTab} />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center text-sm text-muted-foreground">
          {activeSandbox?.status === "failed" ? "Sandbox failed" : "Sandbox ready"}
        </div>
      )}
    </div>
  );
}
