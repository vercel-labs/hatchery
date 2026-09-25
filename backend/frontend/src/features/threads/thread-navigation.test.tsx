import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ThreadNavigation } from "@/features/threads/thread-navigation";
import type { Thread } from "@/lib/api-types";

function rosterThread(
  threadId: string,
  parentThreadId = "",
  values: Partial<Thread> = {},
): Thread {
  return {
    thread_id: threadId,
    parent_thread_id: parentThreadId,
    depth: parentThreadId ? 1 : 0,
    upstream: parentThreadId ? `threads/mira/${parentThreadId}` : "main",
    children: [],
    status: "idle",
    live: true,
    archived: false,
    task_id: parentThreadId ? `task-${threadId}` : "",
    task_status: parentThreadId ? "working" : "",
    deliverables: [],
    summary: threadId,
    error: "",
    input_tokens: 0,
    output_tokens: 0,
    proposals: [],
    ...values,
  };
}

function navigation(threads: Thread[], selected: string | null = null) {
  return render(
    <ThreadNavigation
      threads={threads}
      selected={selected}
      onSelect={vi.fn()}
    />,
  );
}

function threadRows() {
  return within(screen.getByRole("tree", { name: "Thread list" }))
    .getAllByRole("treeitem")
    .map((item) => item.closest<HTMLElement>("[data-thread-depth]")!);
}

// agentmesh: [[subagents#Console]]
it("renders a collapsed hierarchy and expands children in roster order", async () => {
  navigation([
    rosterThread("Earlier root"),
    rosterThread("First child", "Earlier root"),
    rosterThread("Grandchild", "First child", {
      depth: 2,
      status: "parked",
      error: "Operator action required",
    }),
    rosterThread("Second child", "Earlier root", {
      task_status: "completed",
    }),
    rosterThread("Recent root"),
  ]);

  expect(threadRows().map((row) => row.textContent)).toEqual([
    expect.stringContaining("Recent root"),
    expect.stringContaining("Earlier root"),
  ]);

  await userEvent.click(
    screen.getByRole("button", { name: "Expand subagents for Earlier root" }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Expand subagents for First child" }),
  );
  const rows = threadRows();
  expect(rows.map((row) => row.textContent)).toEqual([
    expect.stringContaining("Recent root"),
    expect.stringContaining("Earlier root"),
    expect.stringContaining("First child"),
    expect.stringContaining("Grandchild"),
    expect.stringContaining("Second child"),
  ]);
  expect(rows.map((row) => row.dataset.threadDepth)).toEqual([
    "0",
    "0",
    "1",
    "2",
    "1",
  ]);
  expect(
    screen.getByRole("img", { name: "Needs attention badge" }),
  ).toBeTruthy();
  expect(screen.getByRole("img", { name: "Completed badge" })).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("supports arrow-key expansion and parent-child focus", async () => {
  navigation([
    rosterThread("Keyboard root"),
    rosterThread("Keyboard child", "Keyboard root"),
  ]);
  const root = screen.getByRole("treeitem", { name: /Keyboard root/ });
  root.focus();

  await userEvent.keyboard("{ArrowRight}");
  const child = await screen.findByRole("treeitem", { name: /Keyboard child/ });
  expect(root.getAttribute("aria-expanded")).toBe("true");

  await userEvent.keyboard("{ArrowRight}");
  expect(document.activeElement).toBe(child);
  await userEvent.keyboard("{ArrowLeft}");
  expect(document.activeElement).toBe(root);
});

// agentmesh: [[subagents#Console]]
it("shows cancelled and parked child status", async () => {
  navigation([
    rosterThread("Status root"),
    rosterThread("Cancelled child", "Status root", {
      task_status: "cancelled",
    }),
    rosterThread("Parked child", "Status root", {
      status: "parked",
      error: "Operator action required",
    }),
  ]);

  await userEvent.click(
    screen.getByRole("button", { name: "Expand subagents for Status root" }),
  );
  expect(screen.getByRole("img", { name: "Cancelled badge" })).toBeTruthy();
  expect(
    screen.getByRole("img", { name: "Needs attention badge" }),
  ).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("labels idle delegated outcomes instead of asking for a reply", async () => {
  navigation([
    rosterThread("Lifecycle root"),
    rosterThread("Finished child", "Lifecycle root", {
      task_status: "completed",
    }),
    rosterThread("Working child", "Lifecycle root"),
  ]);

  await userEvent.click(
    screen.getByRole("button", { name: "Expand subagents for Lifecycle root" }),
  );
  expect(screen.getAllByText("Completed")).toHaveLength(1);
  expect(screen.getByRole("img", { name: "Completed badge" })).toBeTruthy();
  expect(
    screen
      .getByRole("img", { name: "Completed" })
      .classList.contains("bg-zinc-200"),
  ).toBe(true);
  expect(screen.getByText("Waiting for input")).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("keeps a matching thread's ancestor path visible during search", async () => {
  navigation([
    rosterThread("Root plan"),
    rosterThread("Implementation", "Root plan"),
    rosterThread("Needle investigation", "Implementation", { depth: 2 }),
    rosterThread("Unrelated one"),
    rosterThread("Unrelated two"),
    rosterThread("Unrelated three"),
  ]);

  await userEvent.type(screen.getByLabelText("Search threads"), "needle");

  await waitFor(() =>
    expect(threadRows().map((row) => row.dataset.threadDepth)).toEqual([
      "0",
      "1",
      "2",
    ]),
  );
  expect(screen.getByText("Root plan")).toBeTruthy();
  expect(screen.getByText("Implementation")).toBeTruthy();
  expect(screen.getByText("Needle investigation")).toBeTruthy();
  expect(screen.queryByText("Unrelated one")).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("does not retain an unrelated selected thread during search", async () => {
  navigation(
    [
      rosterThread("Selected root"),
      rosterThread("First"),
      rosterThread("Second"),
      rosterThread("Third"),
      rosterThread("Fourth"),
      rosterThread("Fifth"),
    ],
    "Selected root",
  );

  await userEvent.type(screen.getByLabelText("Search threads"), "absent");

  await waitFor(() =>
    expect(screen.getByText("No matching threads.")).toBeTruthy(),
  );
  expect(screen.queryByText("Selected root")).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("retains archived ancestors for active and selected archived descendants", () => {
  const root = rosterThread("Archived root", "", { archived: true });
  const archivedChild = rosterThread("Archived child", "Archived root", {
    archived: true,
  });
  const activeGrandchild = rosterThread("Active grandchild", "Archived child", {
    depth: 2,
  });
  const { rerender } = navigation([root, archivedChild, activeGrandchild]);

  expect(threadRows().map((row) => row.textContent)).toEqual([
    expect.stringContaining("Archived root"),
    expect.stringContaining("Archived child"),
    expect.stringContaining("Active grandchild"),
  ]);

  rerender(
    <ThreadNavigation
      threads={[root, archivedChild]}
      selected="Archived child"
      onSelect={vi.fn()}
    />,
  );
  expect(threadRows().map((row) => row.textContent)).toEqual([
    expect.stringContaining("Archived root"),
    expect.stringContaining("Archived child"),
  ]);
  expect(
    screen.getByText("Archived child").closest("button")?.ariaCurrent,
  ).toBe("true");
});

// agentmesh: [[subagents#Console]]
it("promotes a thread with an unknown parent to a root", () => {
  navigation([rosterThread("Orphaned task", "missing-parent")]);

  expect(threadRows()).toHaveLength(1);
  expect(threadRows()[0].dataset.threadDepth).toBe("0");
  expect(screen.getByText("Orphaned task")).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("renders every thread once when parent links contain a cycle", () => {
  navigation(
    [
      rosterThread("Cycle alpha", "Cycle beta"),
      rosterThread("Cycle beta", "Cycle alpha"),
      rosterThread("Cycle descendant", "Cycle alpha"),
    ],
    "Cycle descendant",
  );

  const rows = threadRows();
  expect(rows).toHaveLength(3);
  expect(screen.getAllByText("Cycle alpha")).toHaveLength(1);
  expect(screen.getAllByText("Cycle beta")).toHaveLength(1);
  expect(screen.getAllByText("Cycle descendant")).toHaveLength(1);
});

// Hatchery: a root thread is a chat, titled like the chat, with its channel and
// attention state on the card.
it("titles root threads by their chat and shows channel and attention badges", () => {
  navigation([
    rosterThread("thread-1", "", {
      title: "Ada triage the flaky deploy",
      trigger: "slack:T1",
      attention: "blocked",
    }),
  ]);
  const card = screen.getByRole("button", { name: /Ada triage the flaky deploy/ });
  expect(within(card).getByLabelText("Blocked")).toBeTruthy();
  expect(card.querySelector("svg path")).toBeTruthy();
});
