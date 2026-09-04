"use client";

import { getToolName } from "ai";
import { BanIcon, CheckIcon, ShieldAlertIcon, XIcon } from "lucide-react";
import { type ReactNode, useLayoutEffect, useRef, useState } from "react";

import {
  toolPayload,
  type ToolPayloadValue,
} from "@/components/parts/tool-payload";
import { Spinner } from "@/components/ui/spinner";
import type { ChatToolPart } from "@/lib/messages";

function status(part: ChatToolPart): { icon: ReactNode; label: string } {
  switch (part.state) {
    case "input-streaming":
      return { icon: <Spinner />, label: "preparing…" };
    case "input-available":
      return { icon: <Spinner />, label: "running…" };
    case "approval-requested":
      return {
        icon: <ShieldAlertIcon className="size-4" />,
        label: "needs approval",
      };
    case "approval-responded":
      return part.approval?.approved
        ? { icon: <Spinner />, label: "approved, running…" }
        : { icon: <BanIcon className="size-4" />, label: "rejected" };
    case "output-available":
      return part.preliminary
        ? { icon: <Spinner />, label: "running…" }
        : { icon: <CheckIcon className="size-4" />, label: "done" };
    case "output-error":
      return {
        icon: <XIcon className="size-4 text-destructive" />,
        label: "failed",
      };
    case "output-denied":
      return { icon: <BanIcon className="size-4" />, label: "denied" };
  }
}

const INLINE_KEY_LENGTH = 5;

function ScalarEntry({ name, value }: { name: string; value: ToolPayloadValue }) {
  const container = useRef<HTMLSpanElement>(null);
  const measurement = useRef<HTMLSpanElement>(null);
  const [block, setBlock] = useState(false);

  useLayoutEffect(() => {
    const element = container.current;
    const content = measurement.current;
    if (name.length <= INLINE_KEY_LENGTH || !element || !content) return;

    const update = () => setBlock(content.scrollWidth > element.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [name]);

  return (
    <span className="relative block min-w-0" ref={container}>
      {name.length > INLINE_KEY_LENGTH && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute invisible whitespace-nowrap"
          ref={measurement}
        >
          {name}: {value.type === "scalar" ? value.value : ""}
        </span>
      )}
      {block ? (
        <>
          <span className="block">{name}:</span>
          <span className="block min-w-0 pl-[2ch]">
            <Payload value={value} />
          </span>
        </>
      ) : (
        <span className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-[1ch]">
          <span>{name}:</span>
          <Payload value={value} />
        </span>
      )}
    </span>
  );
}

function Payload({ value }: { value: ToolPayloadValue }) {
  if (value.type === "scalar") {
    return <span className="min-w-0 break-words whitespace-pre-wrap">{value.value}</span>;
  }

  if (value.type === "array") {
    return (
      <span className="grid min-w-0 gap-y-0.5">
        {value.values.map((item, index) => (
          <span className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-2" key={index}>
            <span>-</span>
            <Payload value={item} />
          </span>
        ))}
      </span>
    );
  }

  return (
    <span className="grid min-w-0 gap-y-0.5">
      {value.entries.map(([key, item]) =>
        item.type === "scalar" ? (
          <ScalarEntry key={key} name={key} value={item} />
        ) : (
          <span className="min-w-0" key={key}>
            <span className="block">{key}:</span>
            <span className="block min-w-0 pl-[2ch]">
              <Payload value={item} />
            </span>
          </span>
        ),
      )}
    </span>
  );
}

// Generic tool renderer (seal's fallback ToolPart, approvals stripped —
// hatchery has no gated tools yet): status row, input, streamed output.
export function ToolPart({ part }: { part: ChatToolPart }) {
  const name = getToolName(part);
  const { icon, label } = status(part);
  const input = part.input == null ? null : toolPayload(part.input);

  return (
    <>
      <div className="flex items-center gap-2 px-1.5 text-sm text-muted-foreground">
        {icon}
        <span className="font-medium text-foreground">{name}</span>
        <span>{label}</span>
      </div>
      {input != null && (
        <div className="max-h-24 overflow-auto px-1.5 font-mono text-xs text-muted-foreground">
          <Payload value={input} />
        </div>
      )}
      {part.state === "output-available" && part.output != null && (
        <pre className="max-h-64 overflow-auto rounded-lg bg-muted p-2 font-mono text-xs break-all whitespace-pre-wrap">
          {typeof part.output === "string"
            ? part.output
            : JSON.stringify(part.output, null, 2)}
        </pre>
      )}
      {part.state === "output-error" && (
        <div className="max-h-64 overflow-auto px-1.5 font-mono text-xs text-destructive">
          <Payload value={toolPayload(part.errorText)!} />
        </div>
      )}
    </>
  );
}
