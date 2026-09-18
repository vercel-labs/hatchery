import { BotIcon, ChevronsUpDownIcon } from "lucide-react";

import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type AgentSwitcherStatus = "online" | "busy" | "offline" | "error";

export type AgentSwitcherAgent = {
  id: string;
  name: string;
  description?: string;
  avatarUrl?: string;
  status?: AgentSwitcherStatus;
};

export type AgentSwitcherProps = {
  agents: AgentSwitcherAgent[];
  value: string | null;
  onValueChange: (agentId: string) => void;
  label?: string;
  disabled?: boolean;
  className?: string;
};

const statusClass: Record<AgentSwitcherStatus, string> = {
  online: "bg-status-green-700",
  busy: "bg-status-amber-700",
  offline: "bg-muted-foreground",
  error: "bg-status-red-700",
};

function initials(name: string) {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "A";
}

function AgentAvatar({ agent }: { agent: AgentSwitcherAgent }) {
  return (
    <span className="relative shrink-0">
      <Avatar size="sm">
        {agent.avatarUrl ? <AvatarImage src={agent.avatarUrl} alt="" /> : null}
        <AvatarFallback>{initials(agent.name)}</AvatarFallback>
      </Avatar>
      {agent.status ? (
        <span
          aria-label={agent.status}
          className={cn(
            "absolute -right-0.5 -bottom-0.5 size-2 rounded-full ring-2 ring-background",
            statusClass[agent.status],
          )}
        />
      ) : null}
    </span>
  );
}

export function AgentSwitcher({
  agents,
  value,
  onValueChange,
  label = "Agent",
  disabled = false,
  className,
}: AgentSwitcherProps) {
  const selected = agents.find((agent) => agent.id === value) ?? null;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        disabled={disabled || agents.length === 0}
        render={
          <Button
            variant="outline"
            className={cn("w-full min-w-0 justify-between", className)}
            aria-label={selected ? `${label}: ${selected.name}` : `Select ${label.toLowerCase()}`}
          />
        }
      >
        <span className="flex min-w-0 items-center gap-2">
          {selected ? <AgentAvatar agent={selected} /> : <BotIcon data-icon="inline-start" />}
          <span className="truncate">{selected?.name ?? `Select ${label.toLowerCase()}`}</span>
        </span>
        <ChevronsUpDownIcon data-icon="inline-end" />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuGroup>
          <DropdownMenuLabel>{label}</DropdownMenuLabel>
          <DropdownMenuRadioGroup value={value ?? undefined} onValueChange={onValueChange}>
            {agents.map((agent) => (
              <DropdownMenuRadioItem key={agent.id} value={agent.id}>
                <AgentAvatar agent={agent} />
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate">{agent.name}</span>
                  {agent.description ? (
                    <span className="truncate text-xs text-muted-foreground">
                      {agent.description}
                    </span>
                  ) : null}
                </span>
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
