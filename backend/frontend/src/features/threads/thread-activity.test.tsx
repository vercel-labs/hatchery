import { render, screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import { ThreadNavigation } from "@/features/threads/thread-navigation";
import { ThreadStatePanel } from "@/features/threads/thread-state-panel";
import type { ThreadActivity, ThreadDetail } from "@/lib/api-types";

const activity: ThreadActivity = {
  status: "idle",
  phase: "leased",
  sandbox_active: true,
  mailbox_depth: 4,
  pending_prompts: 2,
  queued_tools: 3,
  running_tool: null,
  consolidating: [],
  awaiting_admission: false,
  budget_held: false,
  quiet_until: 0,
  schedules: [],
  updated_at: 1_789_000_000,
};
const thread: ThreadDetail = {
  thread_id: "background-thread",
  owner: "mira",
  summary: "Investigate build latency",
  status: "idle",
  live: true,
  archived: false,
  task_id: "background-thread",
  task_status: "working",
  deliverables: [],
  error: "",
  input_tokens: 140,
  output_tokens: 37,
  proposals: [],
  turns: 5,
  compactions: 1,
  revision: 23,
  branch: "threads/mira/build",
  base_sha: "a".repeat(40),
  checkpoint_sha: "b".repeat(40),
  messages: [],
  activity,
};

// agentmesh: [[gateway#Thread activity tests]]
it("keeps accepted prompts in the work queue when the runtime inbox is empty", () => {
  const current = {
    ...thread,
    activity: {
      ...activity,
      status: "active",
      phase: "idle",
      mailbox_depth: 0,
      pending_prompts: 3,
      queued_tools: 0,
      running_tool: { tool_name: "bash", tool_args: '{"command":"sleep 15"}' },
    },
  };
  const { rerender } = render(<ThreadStatePanel thread={current} />);
  const queue = () => screen.getByRole("region", { name: "Work queue" });
  expect(within(queue()).getByText("3")).toBeTruthy();
  expect(within(queue()).getByText("3 prompts")).toBeTruthy();
  const details = screen.getByText("Details").closest("details")!;
  expect(details.open).toBe(false);
  screen.getByText("Details").click();
  expect(details.open).toBe(true);
  expect(
    within(screen.getByText("Runtime inbox").parentElement!).getByText("0"),
  ).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{
        ...current,
        activity: { ...current.activity, queued_tools: 2, mailbox_depth: 7 },
      }}
    />,
  );
  expect(within(queue()).getByText("5")).toBeTruthy();
  expect(within(queue()).getByText("3 prompts · 2 tools")).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{ ...current, live: false, status: "cancelled" }}
    />,
  );
  expect(screen.queryByRole("region", { name: "Work queue" })).toBeNull();
  rerender(
    <ThreadNavigation
      threads={[current]}
      selected={current.thread_id}
      onSelect={() => {}}
    />,
  );
  expect(screen.getByLabelText("3 prompts queued")).toBeTruthy();
  expect(screen.getByLabelText("1 tool running")).toBeTruthy();
});

// agentmesh: [[gateway#Thread activity tests]]
it("shows queued tools separately from the running command", () => {
  const current = {
    ...thread,
    activity: {
      ...activity,
      status: "active",
      phase: "idle",
      mailbox_depth: 0,
      pending_prompts: 0,
      queued_tools: 1,
      running_tool: { tool_name: "bash", tool_args: '{"command":"sleep 15"}' },
    },
  };
  const { rerender } = render(<ThreadStatePanel thread={current} />);
  expect(screen.getByText("sleep 15")).toBeTruthy();
  expect(
    within(screen.getByRole("region", { name: "Work queue" })).getByText(
      "1 tool",
    ),
  ).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{
        ...current,
        activity: { ...current.activity, queued_tools: 0 },
      }}
    />,
  );
  expect(screen.queryByRole("region", { name: "Work queue" })).toBeNull();
  expect(screen.getByText("sleep 15")).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{
        ...current,
        activity: {
          ...current.activity,
          queued_tools: 0,
          running_tool: null,
          status: "idle",
        },
      }}
    />,
  );
  expect(screen.queryByRole("region", { name: "Current command" })).toBeNull();
  expect(screen.getByText("Ready for your reply")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Scheduled work" })).toBeNull();
  expect(screen.getByText("Details").closest("details")?.open).toBe(false);
});

// agentmesh: [[gateway#Thread activity tests]]
it("animates the bot and shows queue counts while an idle report is doing background work", () => {
  const { rerender } = render(
    <ThreadNavigation
      threads={[thread]}
      selected={thread.thread_id}
      onSelect={() => {}}
    />,
  );
  const indicator = screen.getByRole("img", { name: "Working in background" });
  expect(
    indicator.firstElementChild?.classList.contains(
      "motion-safe:animate-bot-hop",
    ),
  ).toBe(true);
  expect(
    indicator
      .querySelector("g")
      ?.classList.contains("motion-safe:animate-bot-blink"),
  ).toBe(true);
  const card = indicator.closest("button")!;
  const badges = card.querySelector<HTMLElement>("[data-thread-badges]")!;
  expect(within(badges).getByLabelText("4 runtime messages")).toBeTruthy();
  expect(within(badges).getByLabelText("2 prompts queued")).toBeTruthy();
  expect(within(badges).getByLabelText("3 tools queued")).toBeTruthy();
  expect(badges.parentElement?.querySelector("strong")?.textContent).toBe(
    "Investigate build latency",
  );
  expect(indicator.closest("button")?.classList.contains("h-16")).toBe(true);

  rerender(
    <ThreadNavigation
      threads={[
        {
          ...thread,
          activity: {
            ...activity,
            phase: "idle",
            mailbox_depth: 0,
            pending_prompts: 0,
            queued_tools: 0,
          },
        },
      ]}
      selected={thread.thread_id}
      onSelect={() => {}}
    />,
  );
  expect(
    screen
      .getByRole("img", { name: "Ready for your reply" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(false);
  expect(
    screen
      .getByRole("img", { name: "Ready for your reply" })
      .querySelector("g")
      ?.classList.contains("motion-safe:animate-bot-blink"),
  ).toBe(true);
  expect(screen.queryByLabelText(/runtime messages/)).toBeNull();

  const sleepingThread = {
    ...thread,
    activity: {
      ...activity,
      phase: "idle",
      sandbox_active: false,
      mailbox_depth: 0,
      pending_prompts: 0,
      queued_tools: 0,
      schedules: Array.from({ length: 5 }, (_, index) => ({
        key: `reminder-${index}`,
        due_at: 1_789_000_100 + index,
      })),
    },
  };
  rerender(
    <ThreadNavigation
      threads={[sleepingThread]}
      selected={thread.thread_id}
      onSelect={() => {}}
    />,
  );
  const sleeping = screen.getByRole("img", {
    name: "Ready for your reply",
  });
  expect(sleeping.querySelector('[data-eye-state="sleeping"]')).toBeTruthy();
  expect(
    sleeping
      .querySelector("g")
      ?.classList.contains("motion-safe:animate-bot-blink"),
  ).toBe(false);
  const sleepingCard = sleeping.closest("button");
  expect(sleepingCard?.dataset.sandboxActive).toBe("false");
  expect(sleepingCard?.classList.contains("opacity-85")).toBe(true);
  expect(sleeping.classList.contains("saturate-50")).toBe(true);
  const scheduled = screen.getByLabelText("5 scheduled");
  expect(scheduled.textContent).toBe("5");
  expect(scheduled.querySelector("svg")).toBeTruthy();

  rerender(
    <ThreadNavigation
      threads={[sleepingThread]}
      selected={thread.thread_id}
      optimisticallyAwake={new Set([thread.thread_id])}
      onSelect={() => {}}
    />,
  );
  const waking = screen.getByRole("img", {
    name: "Ready for your reply",
  });
  expect(waking.querySelector('[data-eye-state="awake"]')).toBeTruthy();
  expect(waking.classList.contains("saturate-50")).toBe(false);
  expect(waking.closest("button")?.dataset.threadState).toBe("awake");
  expect(waking.closest("button")?.classList.contains("opacity-85")).toBe(
    false,
  );
});

// agentmesh: [[gateway#Thread activity tests]]
it("shows the running child command even when the thread itself has no lease", () => {
  render(
    <ThreadStatePanel
      thread={{
        ...thread,
        activity: {
          ...activity,
          phase: "idle",
          status: "active",
          running_tool: {
            tool_name: "bash",
            tool_args: '{"command":"npm run investigate -- --verbose"}',
          },
        },
      }}
    />,
  );
  expect(
    screen
      .getByRole("img", { name: "Running command" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(true);
  expect(screen.getByText("npm run investigate -- --verbose")).toBeTruthy();
  const queue = screen.getByRole("region", { name: "Work queue" });
  expect(within(queue).getByText("5")).toBeTruthy();
  expect(within(queue).getByText("2 prompts · 3 tools")).toBeTruthy();
});

// agentmesh: [[gateway#Thread activity tests]]
it("keeps the bot grounded during an admission wait and explains a scheduled retry", () => {
  const waiting = {
    ...thread,
    status: "waiting",
    activity: {
      ...activity,
      status: "active",
      phase: "idle",
      mailbox_depth: 0,
      awaiting_admission: true,
    },
  };
  const { rerender } = render(<ThreadStatePanel thread={waiting} />);
  expect(
    screen
      .getByRole("img", { name: "Waiting for budget" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(false);
  rerender(
    <ThreadStatePanel
      thread={{
        ...waiting,
        activity: {
          ...waiting.activity,
          schedules: [{ key: "model-retry", due_at: 1_789_000_030 }],
        },
      }}
    />,
  );
  expect(
    screen
      .getByRole("img", { name: "Waiting to retry" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(false);
  expect(
    within(screen.getByRole("region", { name: "Scheduled work" })).getByText(
      "Model retry",
    ),
  ).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{
        ...waiting,
        activity: {
          ...waiting.activity,
          phase: "leased",
          schedules: [{ key: "quiescence", due_at: 1_789_000_045 }],
        },
      }}
    />,
  );
  expect(
    screen
      .getByRole("img", { name: "Waiting for command shutdown" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(false);
  expect(
    within(screen.getByRole("region", { name: "Scheduled work" })).getByText(
      "Command shutdown deadline",
    ),
  ).toBeTruthy();
  rerender(
    <ThreadStatePanel
      thread={{
        ...waiting,
        activity: {
          ...waiting.activity,
          status: "parked",
          phase: "idle",
          sandbox_active: false,
          awaiting_admission: false,
          schedules: [],
        },
      }}
    />,
  );
  const needsAttention = screen.getByRole("img", {
    name: "Needs attention",
  });
  expect(
    needsAttention.querySelector('[data-eye-state="attention"]'),
  ).toBeTruthy();
  expect(
    needsAttention
      .querySelector("g")
      ?.classList.contains("motion-safe:animate-bot-blink"),
  ).toBe(false);
});

// agentmesh: [[gateway#Thread activity tests]]
it("marks a budget-held thread as paused with an amber attention badge", () => {
  const held = {
    ...thread,
    status: "waiting",
    activity: {
      ...activity,
      status: "active",
      phase: "leased",
      sandbox_active: false,
      mailbox_depth: 0,
      awaiting_admission: true,
      budget_held: true,
    },
  };
  render(
    <ThreadNavigation
      threads={[held]}
      selected={held.thread_id}
      onSelect={() => {}}
    />,
  );

  const indicator = screen.getByRole("img", {
    name: "Paused: budget exhausted",
  });
  expect(indicator.querySelector('[data-eye-state="sleeping"]')).toBeTruthy();
  expect(indicator.classList.contains("ring-amber-400/70")).toBe(true);
  expect(indicator.classList.contains("ring-destructive/50")).toBe(false);
  const badge = screen.getByRole("img", {
    name: "Paused: budget exhausted badge",
  });
  expect(badge.textContent).toBe("!");
  expect(badge.classList.contains("bg-amber-400")).toBe(true);
  expect(screen.getByText("Paused: budget exhausted")).toBeTruthy();
});

// agentmesh: [[gateway#Thread activity tests]]
it("stops the bot for a terminal thread even if its saved command remains", () => {
  render(
    <ThreadStatePanel
      thread={{
        ...thread,
        live: false,
        status: "failed",
        activity: {
          ...activity,
          status: "failed",
          phase: "terminal",
          running_tool: {
            tool_name: "bash",
            tool_args: '{"command":"check-build"}',
          },
        },
      }}
    />,
  );
  expect(
    screen
      .getByRole("img", { name: "Failed" })
      .firstElementChild?.classList.contains("motion-safe:animate-bot-hop"),
  ).toBe(false);
  expect(
    screen
      .getByRole("img", { name: "Failed" })
      .querySelector("g")
      ?.classList.contains("motion-safe:animate-bot-blink"),
  ).toBe(false);
});
