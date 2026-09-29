import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type ContextTab = {
  key: string;
  label: ReactNode;
  title?: string;
  // DOM ids tie a tab to the panel it shows, when it has one.
  id?: string;
  controls?: string;
  selected: boolean;
  // Marks the thread a view tab currently shows, without selecting it.
  current?: boolean;
  separated?: boolean;
  onSelect: () => void;
};

// The main pane's top bar: the tabs of the active context (an agent or a chat).
// Tabs past the visible ones go into a "more" menu.
export function ContextTabs({
  label,
  leading,
  tabs,
  more = [],
}: {
  label: string;
  leading?: ReactNode;
  tabs: ContextTab[];
  more?: ContextTab[];
}) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-2 border-b px-3">
      {leading}
      <div
        role="tablist"
        aria-label={label}
        className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none]"
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          const items = Array.from(
            event.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]'),
          );
          const index = items.indexOf(event.target as HTMLElement);
          if (index < 0) return;
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? items.length - 1
                : (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) %
                  items.length;
          items[next].focus();
        }}
      >
        {tabs.map((tab) => (
          <Button
            key={tab.key}
            id={tab.id}
            role="tab"
            aria-selected={tab.selected}
            aria-controls={tab.controls}
            data-current={tab.current || undefined}
            tabIndex={tab.selected ? 0 : -1}
            variant={tab.selected ? "secondary" : "ghost"}
            size="sm"
            title={tab.title}
            className={cn(
              "max-w-48 shrink-0",
              !tab.selected && !tab.current && "text-muted-foreground",
              tab.separated && "ms-3",
            )}
            onClick={tab.onSelect}
          >
            <span className="truncate">{tab.label}</span>
          </Button>
        ))}
        {more.length ? (
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  size="sm"
                  variant="ghost"
                  className="shrink-0 text-muted-foreground"
                  aria-label={`${more.length} more`}
                />
              }
            >
              +{more.length}
              <ChevronDown />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {more.map((tab) => (
                <DropdownMenuItem key={tab.key} title={tab.title} onClick={tab.onSelect}>
                  <span className="truncate">{tab.label}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : null}
      </div>
    </header>
  );
}
