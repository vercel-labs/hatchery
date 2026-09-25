import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { Thread, ThreadActivity } from "@/lib/api-types";
import { ThreadNavigation } from "@/features/threads/thread-navigation";
import { ThreadHierarchy } from "@/features/threads/thread-hierarchy";
import { Transcript } from "@/features/threads/transcript";
import { TaskToolCard } from "@/features/threads/task-handoff";

const active: ThreadActivity = {
  status: "active",
  phase: "leased",
  sandbox_active: true,
  mailbox_depth: 0,
  pending_prompts: 0,
  queued_tools: 0,
  running_tool: null,
  consolidating: [],
  awaiting_admission: false,
  budget_held: false,
  quiet_until: 0,
  schedules: [],
  updated_at: 1789261620,
};
function thread(id: string, parent = "", values: Partial<Thread> = {}): Thread {
  return {
    thread_id: id,
    parent_thread_id: parent,
    depth: parent ? 1 : 0,
    upstream: "main",
    children: [],
    status: "idle",
    live: true,
    archived: false,
    task_id: id,
    task_handle: "",
    task_status: parent ? "working" : "",
    deliverables: [],
    summary: id,
    result: "",
    error: "",
    input_tokens: 0,
    output_tokens: 0,
    proposals: [],
    ...values,
  };
}

// agentmesh: [[subagents#Console]]
it("bounds active bots to four, counts overflow and finished descendants, and expands from the stack", async () => {
  const root = thread("Poem");
  const workers = Array.from({ length: 7 }, (_, index) =>
    thread(`Writer ${index + 1}`, "Poem", {
      status: "active",
      activity: active,
    }),
  );
  const finished = [
    thread("Verse A", "Writer 1", { task_status: "completed" }),
    thread("Verse B", "Writer 1", { task_status: "completed" }),
  ];
  const attention = thread("Needs help", "Poem", {
    status: "parked",
    error: "Operator action required",
    activity: {
      ...active,
      status: "parked",
      phase: "idle",
      sandbox_active: false,
    },
  });
  render(
    <ThreadNavigation
      threads={[root, ...workers, ...finished, attention]}
      selected="Poem"
      onSelect={vi.fn()}
    />,
  );
  const stack = screen.getByRole("button", {
    name: "Expand subagents for Poem",
  });
  expect(
    within(stack).getByText("7 active, 1 need attention, 2 finished"),
  ).toBeTruthy();
  expect(
    [...stack.querySelectorAll("[data-bot-stack]")].map((item) =>
      item.getAttribute("data-bot-stack"),
    ),
  ).toEqual(["1", "1", "1", "4", "1", "2"]);
  expect(stack.querySelectorAll(".motion-safe\\:animate-bot-hop")).toHaveLength(
    4,
  );
  expect(screen.queryByText("Writer 2")).toBeNull();
  await userEvent.click(stack);
  expect(screen.getByText("Writer 2")).toBeTruthy();
  await userEvent.click(
    screen.getByRole("button", { name: "Collapse subagents for Poem" }),
  );
  expect(screen.queryByText("Writer 2")).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("replaces the status text as children arrive and keeps disclosure separate from thread selection", async () => {
  const onSelect = vi.fn();
  const parent = thread("Poem", "", { status: "active", activity: active });
  const child = thread("First verse", "Poem", {
    status: "active",
    activity: active,
  });
  const { rerender } = render(
    <ThreadNavigation threads={[parent]} selected="Poem" onSelect={onSelect} />,
  );
  expect(screen.getByText("Thinking")).toBeTruthy();
  const search = screen.getByRole("textbox", { name: "Search threads" });
  rerender(
    <ThreadNavigation
      threads={[parent, child]}
      selected="Poem"
      onSelect={onSelect}
    />,
  );
  expect(screen.queryByText("Thinking")).toBeNull();
  expect(screen.getByRole("textbox", { name: "Search threads" })).toBe(search);
  expect(screen.getByRole("img", { name: "Working" })).toBeTruthy();
  await userEvent.click(
    screen.getByRole("button", { name: "Expand subagents for Poem" }),
  );
  expect(screen.getByText("First verse")).toBeTruthy();
  expect(onSelect).not.toHaveBeenCalled();
  await userEvent.click(
    screen.getByRole("button", { name: "Collapse subagents for Poem" }),
  );
  await userEvent.click(screen.getByText("Poem"));
  expect(onSelect).toHaveBeenCalledExactlyOnceWith("Poem");

  rerender(
    <ThreadNavigation
      threads={[
        thread("Poem"),
        {
          ...child,
          status: "idle",
          task_status: "completed",
          activity: undefined,
        },
      ]}
      selected="Poem"
      onSelect={onSelect}
    />,
  );
  expect(screen.queryByText("Ready for your reply")).toBeNull();
  expect(
    screen.getByRole("img", { name: "Ready for your reply" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Expand subagents for Poem" }).title,
  ).toContain("1 finished");
});

// agentmesh: [[subagents#Console]]
it("uses the same live badges for parents and leaves and replaces stale work with completion and archive outcomes", () => {
  const parent = thread("Planner");
  const leaf = thread("Writer", "Planner");
  const onSelect = vi.fn();
  const { rerender } = render(
    <ThreadNavigation
      threads={[parent, leaf]}
      selected="Writer"
      onSelect={onSelect}
    />,
  );
  expect(screen.queryAllByRole("img", { name: / badge$/ })).toHaveLength(0);
  const command = {
    ...active,
    running_tool: {
      tool_name: "bash",
      tool_args: '{"command":"cat verse.md"}',
    },
  };
  rerender(
    <ThreadNavigation
      threads={[
        { ...parent, status: "active", activity: command },
        {
          ...leaf,
          task_status: "completed",
          status: "active",
          activity: command,
        },
      ]}
      selected="Writer"
      onSelect={onSelect}
    />,
  );
  expect(
    screen.getAllByRole("img", { name: "Running command badge" }),
  ).toHaveLength(2);
  expect(screen.queryByRole("img", { name: "Completed badge" })).toBeNull();
  rerender(
    <ThreadNavigation
      threads={[
        { ...parent, status: "active", activity: active },
        {
          ...leaf,
          task_status: "working",
          status: "active",
          activity: {
            ...active,
            running_tool: { tool_name: "message_task", tool_args: "{}" },
          },
        },
      ]}
      selected="Writer"
      onSelect={onSelect}
    />,
  );
  expect(screen.getByRole("img", { name: "Thinking badge" })).toBeTruthy();
  expect(
    screen.getByRole("img", { name: "Messaging subagent badge" }),
  ).toBeTruthy();
  const completed = {
    ...leaf,
    task_status: "completed" as const,
    activity: { ...command, status: "idle", phase: "terminal" },
  };
  rerender(
    <ThreadNavigation
      threads={[parent, completed]}
      selected="Writer"
      onSelect={onSelect}
    />,
  );
  expect(
    screen.queryByRole("img", { name: "Running command badge" }),
  ).toBeNull();
  expect(screen.getByRole("img", { name: "Completed badge" })).toBeTruthy();
  expect(screen.getAllByText("Completed")).toHaveLength(1);
  rerender(
    <ThreadNavigation
      threads={[parent, { ...completed, archived: true }]}
      selected="Writer"
      onSelect={onSelect}
    />,
  );
  expect(screen.getByRole("img", { name: "Archived badge" })).toBeTruthy();
  expect(
    screen.getByRole("img", { name: "Archived", exact: true }),
  ).toBeTruthy();
  expect(screen.queryByRole("img", { name: "Completed badge" })).toBeNull();
  expect(screen.getAllByText("Archived")).toHaveLength(1);
});

// agentmesh: [[subagents#Console]]
it("marks completion only for explicitly completed delegated tasks", () => {
  render(
    <ThreadNavigation
      threads={[
        thread("Main", "", { task_status: "completed" }),
        thread("Finished worker", "Main", { task_status: "completed" }),
        thread("Stopped worker", "Main", { live: false }),
        thread("Stopped root", "", {
          live: false,
          activity: { ...active, phase: "terminal" },
        }),
      ]}
      selected="Finished worker"
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getAllByRole("img", { name: "Completed badge" })).toHaveLength(
    1,
  );
  expect(
    within(screen.getByRole("treeitem", { name: /Finished worker/ })).getByRole(
      "img",
      { name: "Completed badge" },
    ),
  ).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("lets the operator collapse the currently selected descendant's ancestor", async () => {
  render(
    <ThreadNavigation
      threads={[thread("Root"), thread("Leaf", "Root")]}
      selected="Leaf"
      onSelect={vi.fn()}
    />,
  );
  expect(screen.getByText("Leaf")).toBeTruthy();
  await userEvent.click(
    screen.getByRole("button", { name: "Collapse subagents for Root" }),
  );
  expect(screen.queryByText("Leaf")).toBeNull();
  const disclosure = screen.getByRole("button", {
    name: "Expand subagents for Root",
  });
  disclosure.focus();
  await userEvent.keyboard("{Enter}");
  expect(screen.getByText("Leaf")).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("shows only the selected subtree with live tool chips and clears chips when work settles", async () => {
  const onSelect = vi.fn();
  const parent = thread("Whole poem");
  const planner = thread("First half", "Whole poem");
  const leaf = thread("First verse", "First half", {
    status: "active",
    activity: {
      ...active,
      running_tool: {
        tool_name: "bash",
        tool_args: '{"command":"cat verse.md"}',
      },
      pending_prompts: 2,
    },
  });
  const sibling = thread("Second half", "Whole poem");
  const { rerender } = render(
    <ThreadHierarchy
      thread={planner}
      threads={[parent, planner, leaf, sibling]}
      onSelect={onSelect}
    />,
  );
  expect(screen.queryByText("Second half")).toBeNull();
  expect(screen.getByLabelText("1 tool running").title).toContain(
    "cat verse.md",
  );
  expect(screen.getByLabelText("2 prompts queued")).toBeTruthy();
  await userEvent.click(screen.getByRole("button", { name: /First verse/ }));
  expect(onSelect).toHaveBeenLastCalledWith("First verse");
  const finished = {
    ...leaf,
    task_status: "completed" as const,
    status: "idle",
    activity: { ...active, status: "idle", phase: "idle" },
  };
  rerender(
    <ThreadHierarchy
      thread={planner}
      threads={[parent, planner, finished, sibling]}
      onSelect={onSelect}
    />,
  );
  expect(screen.queryByLabelText("1 tool running")).toBeNull();
  expect(screen.queryByLabelText("2 prompts queued")).toBeNull();
  expect(screen.getByText("1 finished")).toBeTruthy();
  await userEvent.click(
    screen.getByRole("button", { name: "Collapse First half" }),
  );
  expect(screen.queryByText("First verse")).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "Open parent: Whole poem" }),
  );
  expect(onSelect).toHaveBeenLastCalledWith("Whole poem");
});

// agentmesh: [[subagents#Console]]
it("distinguishes parent assignments and follow-ups from direct human messages, including older assignments", async () => {
  const onThread = vi.fn();
  render(
    <Transcript
      parentThreadId="Planner"
      parentThread={thread("Planner")}
      onThread={onThread}
      messages={[
        {
          role: "user",
          source: "operator",
          parts: [{ kind: "text", text: "Write five lines" }],
        },
        {
          role: "assistant",
          parts: [{ kind: "text", text: "Five lines ready" }],
        },
        {
          role: "user",
          source: "parent",
          parts: [{ kind: "text", text: "Report the result" }],
        },
        {
          role: "user",
          source: "operator",
          request_id: "human-17",
          parts: [{ kind: "text", text: "Keep my wording" }],
        },
      ]}
    />,
  );
  expect(
    screen.getByText("Write five lines").closest("article")?.dataset
      .messageSource,
  ).toBe("parent");
  expect(
    screen.getByText("Report the result").closest("article")?.dataset
      .messageSource,
  ).toBe("parent");
  expect(
    screen.getByText("Keep my wording").closest("article")?.dataset
      .messageSource,
  ).toBe("user");
  await userEvent.click(
    screen.getAllByRole("button", { name: "Parent agent" })[0],
  );
  expect(onThread).toHaveBeenCalledWith("Planner");
});

// agentmesh: [[subagents#Console]]
it("shows messages sent to the parent inline as chat messages instead of collapsed work", () => {
  const { rerender } = render(
    <Transcript
      parentThreadId="Planner"
      parentThread={thread("Planner")}
      messages={[
        {
          role: "user",
          source: "parent",
          parts: [{ kind: "text", text: "Draft the first verse" }],
        },
        {
          role: "assistant",
          turn: 1,
          parts: [
            {
              kind: "tool_call",
              tool_name: "bash",
              tool_call_id: "read-notes",
              tool_args: '{"command":"cat notes.md"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "bash",
              tool_call_id: "read-notes",
              result: { exit_code: 0, stdout: "meter: iambic" },
            },
          ],
        },
        {
          role: "assistant",
          turn: 1,
          parts: [
            {
              kind: "tool_call",
              tool_name: "message_parent",
              tool_call_id: "ask-parent",
              tool_args: '{"text":"Should the **last line** rhyme?"}',
            },
          ],
        },
      ]}
    />,
  );
  const sending = screen.getByRole("region", {
    name: "Message to parent agent",
  });
  expect(sending.dataset.parentMessage).toBe("sending");
  expect(within(sending).getByText("Sending to parent agent")).toBeTruthy();
  expect(within(sending).getByText("last line").tagName).toBe("STRONG");
  expect(sending.closest("article")?.dataset.messageSource).toBe("assistant");
  // Real work stays behind the disclosure; the message does not.
  const work = screen.getByRole("group", { name: "Agent activity" });
  expect(work.hasAttribute("open")).toBe(false);
  expect(work.contains(sending)).toBe(false);
  expect(within(work).queryByText("Messaged parent")).toBeNull();

  rerender(
    <Transcript
      parentThreadId="Planner"
      parentThread={thread("Planner")}
      messages={[
        {
          role: "assistant",
          turn: 1,
          parts: [
            {
              kind: "tool_call",
              tool_name: "message_parent",
              tool_call_id: "ask-parent",
              tool_args: '{"text":"Should the last line rhyme?"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "message_parent",
              tool_call_id: "ask-parent",
              result: { status: "ok" },
            },
          ],
        },
        {
          role: "assistant",
          turn: 2,
          parts: [
            {
              kind: "tool_call",
              tool_name: "message_parent",
              tool_call_id: "late-parent",
              tool_args: '{"text":"Also, how long?"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "message_parent",
              tool_call_id: "late-parent",
              result_kind: "error",
              result: "Task is no longer active",
            },
          ],
        },
      ]}
    />,
  );
  const [sent, failed] = screen.getAllByRole("region", {
    name: "Message to parent agent",
  });
  expect(sent.dataset.parentMessage).toBe("sent");
  expect(within(sent).getByText("Sent to parent agent")).toBeTruthy();
  expect(within(sent).getByText("Should the last line rhyme?")).toBeTruthy();
  expect(failed.dataset.parentMessage).toBe("failed");
  expect(
    within(failed).getByText("Failed to send to parent agent"),
  ).toBeTruthy();
  expect(within(failed).getByText("Task is no longer active")).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Agent activity" })).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("keeps messages and historical completion in one board even without the original delegation", async () => {
  const onThread = vi.fn();
  render(
    <Transcript
      threads={[thread("Writer", "Planner", { task_status: "working" })]}
      onThread={onThread}
      messages={[
        {
          role: "user",
          source: "task",
          reporting_thread_id: "Writer",
          task_handle: "task-2",
          task_status: "working",
          task_event: "message",
          parts: [
            {
              kind: "text",
              text: "Message from delegated task:\nTask: task-2\nObjective: Write the closing verse\nMessage:\nShould the last line rhyme?",
            },
          ],
        },
        {
          role: "user",
          source: "task",
          reporting_thread_id: "Writer",
          task_handle: "task-2",
          task_status: "completed",
          task_event: "completion",
          parts: [
            {
              kind: "text",
              text: "Delegated task completed:\nTask: task-2\nObjective: Write the closing verse\nSummary: Draft ready\nResult:\nSalt on the window\nLight on the rails\nChild roster:\n- task-2: completed",
            },
          ],
        },
        {
          role: "assistant",
          parts: [{ kind: "text", text: "I have the verse." }],
        },
      ]}
    />,
  );
  expect(screen.queryByText("Should the last line rhyme?")).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-2" }),
  );
  const board = screen.getByRole("region", {
    name: "Conversation with task-2",
  });
  expect(within(board).getAllByRole("listitem")).toHaveLength(2);
  expect(within(board).queryByText("I have the verse.")).toBeNull();
  expect(screen.getByText("I have the verse.")).toBeTruthy();
  expect(screen.getByText("Should the last line rhyme?")).toBeTruthy();
  expect(screen.getByText("Subagent completed")).toBeTruthy();
  expect(screen.queryByRole("group", { name: "Agent activity" })).toBeNull();
  await userEvent.click(screen.getByText("View result & artifacts"));
  expect(
    screen.getByText("Salt on the window Light on the rails").textContent,
  ).toBe("Salt on the window\nLight on the rails");
  await userEvent.click(
    screen.getByRole("button", { name: "Open subagent: Writer" }),
  );
  expect(onThread).toHaveBeenCalledWith("Writer");
});

// agentmesh: [[subagents#Console]]
it("names rejected task messaging as a failure instead of a successful send", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "assistant",
          parts: [
            {
              kind: "tool_call",
              tool_name: "message_task",
              tool_call_id: "message-1",
              tool_args: '{"task_id":"task-9","text":"Continue"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "message_task",
              tool_call_id: "message-1",
              result_kind: "error",
              result: "Unknown task. Valid task: task-1",
            },
          ],
        },
      ]}
    />,
  );
  await userEvent.click(screen.getByText("Subagent action failed"));
  expect(screen.getByText("Task action failed")).toBeTruthy();
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-9" }),
  );
  expect(screen.getByText("Sending message failed")).toBeTruthy();
  expect(screen.queryByText("Message accepted")).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("does not link a child's own complete call to one of its descendants", () => {
  render(
    <TaskToolCard
      threads={[thread("Writer", "Planner", { task_handle: "task-1" })]}
      onThread={vi.fn()}
      item={{
        kind: "tool",
        key: "complete",
        name: "complete",
        call: {
          tool_args:
            '{"summary":"Ten lines ready","result":"The lines are ready"}',
        },
        result: { result: { status: "finalizing", task_id: "task-1" } },
      }}
    />,
  );
  expect(screen.getByText("Completion handoff started")).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Open subagent: Writer" }),
  ).toBeNull();
});
