import { lazy, Suspense, useState } from "react";
import { api } from "@/lib/api";
import type { AgentThreads } from "@/lib/api-types";
import { RepositoryPanelSkeleton } from "./repository-skeletons";
import { RepositoryVersionSwitcher } from "./repository-version-switcher";
const RepositoryPanel = lazy(() => import("./repository-panel"));

// Repository browses committed main for every agent; with `agentId` it is that
// agent's Workspace, limited to its directory and its workspace proposals.
export function RepositoryView({
  rosters,
  refresh,
  agentId,
  title,
}: {
  rosters: AgentThreads[];
  refresh: () => Promise<unknown>;
  agentId?: string;
  title?: string;
}) {
  const [selection, setSelection] = useState("");
  const proposals = rosters.flatMap((roster) =>
    roster.threads.flatMap((thread) =>
      thread.proposals
        .filter(
          (proposal) =>
            thread.chat_id &&
            (!agentId ||
              (roster.agent_id === agentId &&
                proposal.section === "workspace")),
        )
        .map((proposal) => ({
          ...proposal,
          owner: roster.agent_id,
          threadId: thread.thread_id,
          chatId: thread.chat_id!,
          localReview: Boolean(roster.local_review),
        })),
    ),
  );
  const proposed = proposals.find((proposal) => proposal.branch === selection);
  async function approve(sha: string) {
    if (!proposed) return;
    await api(`/api/chats/${encodeURIComponent(proposed.chatId)}/thread/approve`, {
      branch: proposed.branch,
      expected_sha: sha,
    });
    await refresh();
  }
  return (
    <>
      <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b px-5">
        <h2 className="truncate text-sm font-medium">
          {title ?? (agentId ? `${agentId} / Workspace` : "Repository")}
        </h2>
        {proposals.length ? (
          <RepositoryVersionSwitcher
            proposals={proposals}
            selected={proposed}
            onSelect={setSelection}
          />
        ) : null}
      </header>
      <Suspense
        fallback={
          <RepositoryPanelSkeleton showModeToggle={Boolean(proposed?.chatId)} />
        }
      >
        <RepositoryPanel
          key={proposed?.branch ?? "main"}
          prefix={agentId ? `agents/${agentId}/` : ""}
          chatId={proposed?.chatId}
          proposal={proposed?.branch}
          onApprove={proposed?.localReview ? approve : undefined}
          reviewUrl={proposed?.url}
        />
      </Suspense>
    </>
  );
}
