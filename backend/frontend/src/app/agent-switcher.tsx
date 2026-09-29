import { Bot, Check, ChevronsUpDown, Plus, Trash2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Agent } from "@/lib/api";
import { resolveAgentColor } from "@/lib/agent-colors";

function threadCount(count: number) {
  return `${count} ${count === 1 ? "thread" : "threads"}`;
}

// Ported from the agentmesh console; agents keep Hatchery's names and colors.
// Besides switching, it adds and deletes agents.
export function AgentSwitcher({
  agents,
  agent,
  threadCounts,
  onSelect,
  onAdd = () => {},
  onDelete = () => {},
}: {
  agents: Agent[];
  agent: Agent;
  threadCounts: Record<string, number>;
  onSelect: (agentId: string) => void;
  onAdd?: () => void;
  onDelete?: (agent: Agent) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="group flex h-12 w-full items-center gap-3 rounded-xl px-2 text-left outline-none transition-colors hover:bg-sidebar-accent focus-visible:ring-3 focus-visible:ring-ring/50 data-popup-open:bg-sidebar-accent"
        aria-label={`Current agent: ${agent.name}. Switch agent`}
      >
        <span className="grid size-8 shrink-0 place-items-center rounded-lg border bg-background">
          <Bot
            className="size-4"
            style={{ color: resolveAgentColor(agent.color) }}
            aria-hidden
          />
        </span>
        <span className="min-w-0 flex-1">
          <strong className="block truncate text-sm font-medium">
            {agent.name}
          </strong>
          <span className="block text-xs text-muted-foreground">
            {threadCount(threadCounts[agent.id] ?? 0)}
          </span>
        </span>
        <ChevronsUpDown
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent sideOffset={6}>
        <DropdownMenuGroup>
          <DropdownMenuLabel>Switch agent</DropdownMenuLabel>
          {agents.map((item) => (
            <div key={item.id} className="flex items-center gap-0.5">
              <DropdownMenuItem
                className="min-w-0 flex-1 gap-3 px-2 py-2"
                onClick={() => onSelect(item.id)}
              >
                <span className="grid size-7 shrink-0 place-items-center rounded-md bg-muted">
                  <Bot
                    className="size-3.5"
                    style={{ color: resolveAgentColor(item.color) }}
                    aria-hidden
                  />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{item.name}</span>
                  <span className="block text-xs text-muted-foreground">
                    {threadCount(threadCounts[item.id] ?? 0)}
                  </span>
                </span>
                {item.id === agent.id ? (
                  <Check className="size-4 shrink-0" aria-label="Selected" />
                ) : null}
              </DropdownMenuItem>
              {/* The last agent stays: chats always need somewhere to land. */}
              <DropdownMenuItem
                variant="destructive"
                className="shrink-0 self-stretch px-2 opacity-60 focus:opacity-100"
                aria-label={`Delete ${item.name}`}
                title={agents.length > 1 ? `Delete ${item.name}` : "The last agent cannot be deleted"}
                disabled={agents.length <= 1}
                onClick={() => onDelete(item)}
              >
                <Trash2 className="size-3.5" />
              </DropdownMenuItem>
            </div>
          ))}
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={onAdd}>
          <Plus />
          Add agent
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
