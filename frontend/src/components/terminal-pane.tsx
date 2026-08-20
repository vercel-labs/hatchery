"use client";

import "@xterm/xterm/css/xterm.css";

import { XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { wsBase } from "@/lib/api";

// The devbox pty over the backend's ws proxy, rendered with xterm.js.
// devboxd's /__tty protocol (cli/devboxd/pkg/protocol): JSON frames
// {type, body} — handshake{sessionId,offset}, tty-output{data:b64},
// tty-input{data:b64}, resize{cols,rows}, exit{code}. Output bytes are
// counted (decoded length) as the resume offset; a close without a
// preceding exit frame is a transport drop, so we reconnect at the offset.
// A 4404 close means the coder session doesn't exist yet — retry until the
// task lands.

const b64encode = (s: string) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));

export function TerminalPane({
  chatId,
  onClose,
}: {
  chatId: string;
  onClose: () => void;
}) {
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

      // xterm measures the character cell by setting this string on a canvas
      // context, where var() is invalid — resolve Geist Mono to its concrete
      // families and make sure it's loaded before the first measurement.
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
        if (!dims || (dims.cols === term.cols && dims.rows === term.rows))
          return;
        fit.fit();
        send("resize", { cols: term.cols, rows: term.rows });
      });
      observer.observe(host);

      const connect = () => {
        if (disposed || exited) return;
        ws = new WebSocket(
          `${wsBase()}/api/chats/${chatId}/tty?offset=${offset}&cols=${term.cols}&rows=${term.rows}`,
        );
        ws.onmessage = (event) => {
          const frame = JSON.parse(event.data);
          if (frame.type === "handshake") {
            offset = frame.body.offset; // server clamps to its replay buffer
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
        ws.onclose = (event) => {
          if (disposed || exited) return;
          if (event.code === 4409) {
            exited = true;
            setStatus("exited");
            term.write(`\r\n[${event.reason || "coder exited"}]\r\n`);
            return;
          }
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
  }, [chatId]);

  return (
    <div className="flex h-2/5 min-w-0 flex-none flex-col border-t @5xl:h-auto @5xl:flex-1 @5xl:border-t-0 @5xl:border-l">
      <div className="flex h-10 shrink-0 items-center gap-2 border-b px-3">
        <span
          className={`size-2 rounded-full ${
            status === "live"
              ? "bg-emerald-500"
              : status === "exited"
                ? "bg-muted-foreground"
                : "animate-pulse bg-amber-500"
          }`}
        />
        <span className="text-xs font-medium text-muted-foreground">
          coder terminal — {status}
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto size-7"
          aria-label="Close terminal"
          onClick={onClose}
        >
          <XIcon className="size-4" />
        </Button>
      </div>
      <div ref={hostRef} className="min-h-0 flex-1 bg-[#0a0a0a] p-2" />
    </div>
  );
}
