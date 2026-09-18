import { ChevronRightIcon, MessageSquareIcon } from "lucide-react";

import {
  Avatar,
  AvatarBadge,
  AvatarFallback,
  AvatarGroup,
  AvatarGroupCount,
  AvatarImage,
} from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

import { buildHierarchy, type HierarchyNode } from "./hierarchy";

export type ThreadBotStatus = "working" | "waiting" | "done" | "error" | "offline";

export type ThreadNavigationBot = {
  id: string;
  name: string;
  avatarUrl?: string;
  status: ThreadBotStatus;
};

export type ThreadActivity = {
  id: string;
  label: string;
  tone?: "neutral" | "attention" | "success" | "error";
};

export type ThreadNavigationItem = {
  id: string;
  parentId?: string | null;
  title: string;
  subtitle?: string;
  bots?: readonly ThreadNavigationBot[];
  activities?: readonly ThreadActivity[];
};

export type ThreadNavigationProps = {
  threads: readonly ThreadNavigationItem[];
  activeThreadId?: string | null;
  onThreadSelect: (thread: ThreadNavigationItem) => void;
  defaultExpanded?: boolean;
  className?: string;
  emptyTitle?: string;
  emptyDescription?: string;
};

const botStatusClass: Record<ThreadBotStatus, string> = {
  working: "bg-status-green-700",
  waiting: "bg-status-amber-700",
  done: "bg-muted-foreground",
  error: "bg-status-red-700",
  offline: "bg-muted-foreground",
};

const activityVariant: Record<
  NonNullable<ThreadActivity["tone"]>,
  "secondary" | "outline" | "destructive"
> = {
  neutral: "outline",
  attention: "secondary",
  success: "secondary",
  error: "destructive",
};

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "B";
}

function BotStack({ bots }: { bots: readonly ThreadNavigationBot[] }) {
  if (bots.length === 0) return null;
  const visible = bots.slice(0, 3);

  return (
    <AvatarGroup aria-label={`${bots.length} bot${bots.length === 1 ? "" : "s"}`}>
      {visible.map((bot) => (
        <Avatar
          key={bot.id}
          size="sm"
          title={`${bot.name}: ${bot.status}`}
          className={cn(bot.status === "working" && "motion-safe:animate-agent-hop")}
        >
          {bot.avatarUrl ? <AvatarImage src={bot.avatarUrl} alt="" /> : null}
          <AvatarFallback>{initials(bot.name)}</AvatarFallback>
          <AvatarBadge
            aria-label={bot.status}
            className={cn(
              botStatusClass[bot.status],
              bot.status === "working" && "motion-safe:animate-agent-pulse",
            )}
          />
        </Avatar>
      ))}
      {bots.length > visible.length ? (
        <AvatarGroupCount aria-label={`${bots.length - visible.length} more bots`}>
          +{bots.length - visible.length}
        </AvatarGroupCount>
      ) : null}
    </AvatarGroup>
  );
}

function ThreadNode({
  node,
  depth,
  activeThreadId,
  onThreadSelect,
  defaultExpanded,
}: {
  node: HierarchyNode<ThreadNavigationItem>;
  depth: number;
  activeThreadId?: string | null;
  onThreadSelect: (thread: ThreadNavigationItem) => void;
  defaultExpanded: boolean;
}) {
  const { item, children } = node;
  const active = item.id === activeThreadId;
  const inset = { paddingInlineStart: `${depth * 0.875 + 0.25}rem` };
  const row = (
    <div
      className={cn(
        "flex min-w-0 items-start gap-1",
        depth > 0 && "border-s border-sidebar-border",
      )}
      style={inset}
      data-orphaned={node.orphaned || undefined}
      data-cyclic={node.cyclic || undefined}
    >
      {children.length ? (
        <CollapsibleTrigger
          aria-label={`Toggle replies to ${item.title}`}
          className="group mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
        >
          <ChevronRightIcon className="size-4 transition-transform duration-200 group-data-panel-open:rotate-90 motion-reduce:transition-none" />
        </CollapsibleTrigger>
      ) : (
        <span className="size-7 shrink-0" aria-hidden="true" />
      )}
      <Button
        variant={active ? "secondary" : "ghost"}
        className="h-auto min-w-0 flex-1 justify-start px-2 py-1.5"
        onClick={() => onThreadSelect(item)}
        aria-current={active ? "page" : undefined}
      >
        <span className="flex min-w-0 flex-1 flex-col items-start gap-1">
          <span className="flex w-full min-w-0 items-center gap-2">
            <span className="truncate">{item.title}</span>
            <BotStack bots={item.bots ?? []} />
          </span>
          {item.subtitle ? (
            <span className="w-full truncate text-left text-xs text-muted-foreground">
              {item.subtitle}
            </span>
          ) : null}
          {item.activities?.length ? (
            <span className="flex max-w-full flex-wrap gap-1">
              {item.activities.map((activity) => (
                <Badge
                  key={activity.id}
                  variant={activityVariant[activity.tone ?? "neutral"]}
                  data-tone={activity.tone ?? "neutral"}
                >
                  {activity.label}
                </Badge>
              ))}
            </span>
          ) : null}
        </span>
      </Button>
    </div>
  );

  if (!children.length) return row;

  return (
    <Collapsible defaultOpen={defaultExpanded}>
      {row}
      <CollapsibleContent className="h-[var(--collapsible-panel-height)] overflow-hidden transition-[height,opacity] duration-200 ease-out motion-reduce:transition-none data-ending-style:h-0 data-ending-style:opacity-0 data-starting-style:h-0 data-starting-style:opacity-0">
        {children.map((child) => (
          <ThreadNode
            key={child.item.id}
            node={child}
            depth={depth + 1}
            activeThreadId={activeThreadId}
            onThreadSelect={onThreadSelect}
            defaultExpanded={defaultExpanded}
          />
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function ThreadNavigation({
  threads,
  activeThreadId,
  onThreadSelect,
  defaultExpanded = true,
  className,
  emptyTitle = "No threads",
  emptyDescription = "Threads will appear here when activity starts.",
}: ThreadNavigationProps) {
  const hierarchy = buildHierarchy(threads);

  return (
    <ScrollArea className={cn("h-full min-h-0", className)}>
      {hierarchy.length ? (
        <nav className="flex min-w-0 flex-col gap-0.5 p-1" aria-label="Threads">
          {hierarchy.map((node) => (
            <ThreadNode
              key={node.item.id}
              node={node}
              depth={0}
              activeThreadId={activeThreadId}
              onThreadSelect={onThreadSelect}
              defaultExpanded={defaultExpanded}
            />
          ))}
        </nav>
      ) : (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <MessageSquareIcon />
            </EmptyMedia>
            <EmptyTitle>{emptyTitle}</EmptyTitle>
            <EmptyDescription>{emptyDescription}</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </ScrollArea>
  );
}
