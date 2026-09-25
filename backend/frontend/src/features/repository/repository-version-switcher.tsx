import { Check, ChevronDown, GitBranch, GitPullRequest } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Proposal } from "@/lib/api-types";

type RepositoryProposal = Proposal & {
  owner: string;
  threadId: string;
};

export function RepositoryVersionSwitcher({
  proposals,
  selected,
  onSelect,
}: {
  proposals: RepositoryProposal[];
  selected?: RepositoryProposal;
  onSelect: (branch: string) => void;
}) {
  const label = selected ? `${selected.owner} / ${selected.section}` : "main";
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="group flex h-9 min-w-28 max-w-[65%] items-center gap-2 rounded-lg bg-muted/60 px-3 text-xs outline-none transition-colors hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 data-popup-open:bg-muted"
        aria-label={`Repository version: ${label}. Switch version`}
      >
        {selected ? (
          <GitPullRequest
            className="size-3.5 shrink-0 text-muted-foreground"
            aria-hidden
          />
        ) : (
          <GitBranch
            className="size-3.5 shrink-0 text-muted-foreground"
            aria-hidden
          />
        )}
        <span className="min-w-0 flex-1 truncate text-left font-medium">
          {label}
        </span>
        <ChevronDown
          className="size-3.5 shrink-0 text-muted-foreground transition-transform group-data-popup-open:rotate-180"
          aria-hidden
        />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={6} className="min-w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Repository version</DropdownMenuLabel>
          <DropdownMenuItem
            className="gap-3 px-2 py-2"
            onClick={() => onSelect("")}
          >
            <GitBranch
              className="size-4 shrink-0 text-muted-foreground"
              aria-hidden
            />
            <span className="min-w-0 flex-1">
              <span className="block font-medium">main</span>
              <span className="block text-xs text-muted-foreground">
                Primary branch
              </span>
            </span>
            {!selected ? (
              <Check className="size-4 shrink-0" aria-label="Selected" />
            ) : null}
          </DropdownMenuItem>
          {proposals.map((proposal) => (
            <DropdownMenuItem
              key={proposal.branch}
              className="gap-3 px-2 py-2"
              onClick={() => onSelect(proposal.branch)}
            >
              <GitPullRequest
                className="size-4 shrink-0 text-muted-foreground"
                aria-hidden
              />
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">
                  {proposal.owner} / {proposal.section}
                </span>
                <span className="block truncate text-xs text-muted-foreground">
                  {proposal.merged ? "Merged" : "Review"} ·{" "}
                  {proposal.branch.slice(-8)}
                </span>
              </span>
              {proposal.branch === selected?.branch ? (
                <Check className="size-4 shrink-0" aria-label="Selected" />
              ) : null}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
