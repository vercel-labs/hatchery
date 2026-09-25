import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { Message } from "@/lib/api-types";
import { Transcript } from "@/features/threads/transcript";

const assignment: Message[] = [
  {
    role: "user",
    source: "operator",
    timestamp: 1789327740,
    parts: [{ kind: "text", text: "Start a subagent and have it wait." }],
  },
  {
    role: "assistant",
    turn: 1,
    timestamp: 1789327741,
    parts: [
      {
        kind: "tool_call",
        tool_name: "delegate",
        tool_call_id: "spawn-1",
        tool_args: { objective: "Say hi and wait for instructions." },
      },
    ],
  },
  {
    role: "tool",
    parts: [
      {
        kind: "tool_result",
        tool_name: "delegate",
        tool_call_id: "spawn-1",
        result: { task_id: "task-1", status: "queued" },
      },
    ],
  },
  {
    role: "assistant",
    turn: 2,
    parts: [{ kind: "text", text: "The subagent has started." }],
  },
];

const greeting: Message = {
  role: "user",
  source: "task",
  timestamp: 1789327800,
  task_id: "root/spawn-1",
  task_handle: "task-1",
  reporting_thread_id: "worker-1",
  task_event: "message",
  task_status: "working",
  parts: [
    {
      kind: "text",
      text: "Message from delegated task:\nTask: task-1\nObjective: Say hi and wait for instructions.\nMessage:\nHi! I will wait.\n\nResult: still waiting, not a completion.",
    },
  ],
};

// agentmesh: [[subagents#Console]]
it("collects directed exchanges while keeping parent replies visible in journal order", async () => {
  const messages: Message[] = [
    ...assignment,
    greeting,
    {
      role: "assistant",
      turn: 3,
      parts: [{ kind: "text", text: "Acknowledged." }],
    },
    {
      role: "user",
      source: "operator",
      parts: [{ kind: "text", text: "Ask it to keep waiting." }],
    },
    {
      role: "assistant",
      turn: 4,
      timestamp: 1789327860,
      parts: [
        {
          kind: "tool_call",
          tool_name: "message_task",
          tool_call_id: "follow-up",
          tool_args: JSON.stringify({
            task_id: "task-1",
            text: "Please keep waiting.\nI will send more work later.",
          }),
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "message_task",
          tool_call_id: "follow-up",
          result: { task_id: "task-1", status: "queued" },
        },
      ],
    },
    {
      role: "assistant",
      turn: 5,
      parts: [{ kind: "text", text: "I asked it to keep waiting." }],
    },
  ];
  const { container } = render(<Transcript messages={messages} />);
  await userEvent.click(screen.getByText("Delegated 1 task"));
  const toggle = screen.getByRole("button", {
    name: "Expand conversation with task-1",
  });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(
    screen.queryByRole("region", { name: "Conversation with task-1" }),
  ).toBeNull();
  expect(screen.getByText("Acknowledged.")).toBeTruthy();
  await userEvent.click(toggle);
  const board = screen.getByRole("region", {
    name: "Conversation with task-1",
  });
  const entries = within(board).getAllByRole("listitem");
  expect(entries).toHaveLength(3);
  expect(entries[0].textContent).toContain("Say hi and wait for instructions.");
  expect(entries[1].textContent).toContain("Hi! I will wait.");
  expect(entries[1].textContent).toContain(
    "Result: still waiting, not a completion.",
  );
  expect(entries[2].textContent).toContain(
    "Please keep waiting.\nI will send more work later.",
  );
  expect(within(board).getByText("Message queued")).toBeTruthy();
  expect(board.querySelector("time")?.dateTime).toBe(
    "2026-09-13T19:29:01.000Z",
  );
  expect(screen.getAllByText("Acknowledged.")).toHaveLength(1);
  expect(within(board).queryByText("Acknowledged.")).toBeNull();
  expect(
    Array.from(container.querySelectorAll("[data-message-text]")).map(
      (element) => element.textContent,
    ),
  ).toEqual([
    "The subagent has started.",
    "Acknowledged.",
    "I asked it to keep waiting.",
  ]);
  expect(board.contains(screen.getByText("The subagent has started."))).toBe(
    false,
  );
  expect(board.contains(screen.getByText("I asked it to keep waiting."))).toBe(
    false,
  );
  expect(screen.queryByText("Requested subagent follow-up")).toBeNull();
  expect(
    container.querySelectorAll("article[data-message-source=assistant]"),
  ).toHaveLength(3);
  await userEvent.click(
    screen.getByRole("button", { name: "Collapse conversation with task-1" }),
  );
  expect(screen.getByText("Acknowledged.")).toBeTruthy();
});

// agentmesh: [[subagents#Console]]
it("renders parent and subagent messages in the conversation board as Markdown", async () => {
  const messages: Message[] = [
    ...assignment,
    {
      ...greeting,
      parts: [
        {
          kind: "text",
          text: "Message from delegated task:\nTask: task-1\nMessage:\nSubagent is **ready** with `code`.",
        },
      ],
    },
    {
      role: "assistant",
      turn: 3,
      parts: [
        {
          kind: "tool_call",
          tool_name: "message_task",
          tool_call_id: "markdown-follow-up",
          tool_args: {
            task_id: "task-1",
            text: [
              "Parent says **continue**:",
              "",
              "- first",
              "- second",
              "",
              "    1   2   3",
              "1   O |   |   ",
              "   ---+---+---",
              "2     | X |   ",
              "   ---+---+---",
              "3     |   | X",
              "",
              "Your turn.",
            ].join("\n"),
          },
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "message_task",
          tool_call_id: "markdown-follow-up",
          result: { task_id: "task-1", status: "accepted" },
        },
      ],
    },
  ];
  render(<Transcript messages={messages} />);
  await userEvent.click(screen.getByText("Delegated 1 task"));
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-1" }),
  );
  const board = screen.getByRole("region", {
    name: "Conversation with task-1",
  });

  expect(within(board).getByText("ready").tagName).toBe("STRONG");
  expect(within(board).getByText("code").tagName).toBe("CODE");
  expect(within(board).getByText("continue").tagName).toBe("STRONG");
  expect(within(board).getByText("first").closest("ul")).toBeTruthy();
  const grid = board.querySelector("pre > code");
  expect(grid?.textContent).toBe(
    [
      "    1   2   3",
      "1   O |   |   ",
      "   ---+---+---",
      "2     | X |   ",
      "   ---+---+---",
      "3     |   | X",
      "",
    ].join("\n"),
  );
  expect(grid?.querySelector("br")).toBeNull();
  expect(within(board).getByText("Your turn.").tagName).toBe("P");
});

// agentmesh: [[subagents#Console]]
it("keeps live parent replies outside the board as history commits", async () => {
  const { rerender } = render(
    <Transcript
      messages={[...assignment, greeting]}
      responding
      live="Acknowledging"
      liveTurn={3}
    />,
  );
  await userEvent.click(screen.getByText("Delegated 1 task"));
  expect(screen.getByLabelText("Streaming response").textContent).toBe(
    "Acknowledging",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-1" }),
  );
  const board = screen.getByRole("region", {
    name: "Conversation with task-1",
  });
  expect(board.contains(screen.getByLabelText("Streaming response"))).toBe(
    false,
  );
  rerender(
    <Transcript
      messages={[
        ...assignment,
        greeting,
        {
          role: "assistant",
          turn: 3,
          parts: [{ kind: "text", text: "Acknowledged." }],
        },
      ]}
    />,
  );
  expect(screen.getByRole("region", { name: "Conversation with task-1" })).toBe(
    board,
  );
  expect(within(board).queryByText("Acknowledged.")).toBeNull();
  expect(screen.getAllByText("Acknowledged.")).toHaveLength(1);
  expect(within(board).getAllByRole("listitem")).toHaveLength(2);
  expect(screen.queryByLabelText("Streaming response")).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("keeps the parent response visible after its directed tool moves into the board", async () => {
  const messages: Message[] = [
    ...assignment,
    greeting,
    {
      role: "assistant",
      turn: 3,
      parts: [
        {
          kind: "tool_call",
          tool_name: "message_task",
          tool_call_id: "continue-waiting",
          tool_args: { task_id: "task-1", text: "Keep waiting." },
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "message_task",
          tool_call_id: "continue-waiting",
          result: { task_id: "task-1", status: "accepted" },
        },
      ],
    },
  ];
  const { rerender } = render(
    <Transcript messages={messages} pendingTurn={4} responding />,
  );
  expect(
    screen.getByRole("status", { name: "Assistant is thinking" }),
  ).toBeTruthy();
  await userEvent.click(screen.getByText("Delegated 1 task"));
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-1" }),
  );
  const board = screen.getByRole("region", {
    name: "Conversation with task-1",
  });
  expect(within(board).getByText("Keep waiting.")).toBeTruthy();
  rerender(
    <Transcript
      messages={messages}
      live="I asked the subagent to keep waiting."
      liveTurn={4}
      responding
    />,
  );
  expect(screen.getAllByLabelText("Streaming response")).toHaveLength(1);
  expect(board.contains(screen.getByLabelText("Streaming response"))).toBe(
    false,
  );
  rerender(
    <Transcript
      messages={[
        ...messages,
        {
          role: "assistant",
          turn: 4,
          parts: [
            { kind: "text", text: "I asked the subagent to keep waiting." },
          ],
        },
      ]}
    />,
  );
  expect(
    screen.getAllByText("I asked the subagent to keep waiting."),
  ).toHaveLength(1);
  expect(
    within(board).queryByText("I asked the subagent to keep waiting."),
  ).toBeNull();
  expect(within(board).getAllByRole("listitem")).toHaveLength(3);
});

// agentmesh: [[subagents#Console]]
it("keeps interleaved subagents separate and routes follow-ups by their target, including internal task IDs", async () => {
  const onThread = vi.fn();
  const messages: Message[] = [
    ...assignment,
    {
      role: "assistant",
      turn: 3,
      parts: [
        {
          kind: "tool_call",
          tool_name: "delegate",
          tool_call_id: "spawn-2",
          tool_args: { objective: "Check the weather." },
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "delegate",
          tool_call_id: "spawn-2",
          result: { task_id: "task-2", status: "queued" },
        },
      ],
    },
    {
      role: "assistant",
      turn: 4,
      parts: [{ kind: "text", text: "Both workers are ready." }],
    },
    greeting,
    {
      role: "assistant",
      turn: 5,
      parts: [
        {
          kind: "tool_call",
          tool_name: "message_task",
          tool_call_id: "ask-weather",
          tool_args: { task_id: "task-2", text: "Is it raining?" },
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "message_task",
          tool_call_id: "ask-weather",
          result: { status: "accepted" },
        },
      ],
    },
    {
      role: "assistant",
      turn: 6,
      parts: [{ kind: "text", text: "The first worker has greeted us." }],
    },
    {
      ...greeting,
      task_id: "root/spawn-2",
      task_handle: "task-2",
      reporting_thread_id: "worker-2",
      parts: [
        {
          kind: "text",
          text: "Message from delegated task:\nTask: task-2\nObjective: Check the weather.\nMessage:\nSunny skies.",
        },
      ],
    },
    {
      role: "assistant",
      turn: 7,
      parts: [
        {
          kind: "tool_call",
          tool_name: "message_task",
          tool_call_id: "wake-first",
          tool_args: { task_id: "root/spawn-1", text: "You can stop waiting." },
        },
      ],
    },
    {
      role: "tool",
      parts: [
        {
          kind: "tool_result",
          tool_name: "message_task",
          tool_call_id: "wake-first",
          result: { status: "accepted" },
        },
      ],
    },
    {
      role: "assistant",
      turn: 8,
      parts: [{ kind: "text", text: "The second worker checked the weather." }],
    },
  ];
  render(<Transcript messages={messages} onThread={onThread} />);
  await userEvent.click(screen.getByText("Delegated 2 tasks"));
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-1" }),
  );
  const first = screen.getByRole("region", {
    name: "Conversation with task-1",
  });
  expect(
    screen.queryByRole("region", { name: "Conversation with task-2" }),
  ).toBeNull();
  expect(within(first).getByText("You can stop waiting.")).toBeTruthy();
  expect(
    within(first).queryByText("The first worker has greeted us."),
  ).toBeNull();
  expect(screen.getByText("The first worker has greeted us.")).toBeTruthy();
  expect(within(first).queryByText("Is it raining?")).toBeNull();
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-2" }),
  );
  const second = screen.getByRole("region", {
    name: "Conversation with task-2",
  });
  expect(within(second).getByText("Is it raining?")).toBeTruthy();
  expect(within(second).getByText("Sunny skies.")).toBeTruthy();
  expect(
    within(second).queryByText("The second worker checked the weather."),
  ).toBeNull();
  expect(
    screen.getByText("The second worker checked the weather."),
  ).toBeTruthy();
  expect(within(second).queryByText("You can stop waiting.")).toBeNull();
  expect(within(first).getAllByRole("listitem")).toHaveLength(3);
  expect(within(second).getAllByRole("listitem")).toHaveLength(3);
  await userEvent.click(
    screen.getByRole("button", { name: "Open subagent: task-2" }),
  );
  expect(onThread).toHaveBeenCalledExactlyOnceWith("worker-2");
});

// agentmesh: [[subagents#Console]]
it("does not assign a response to one task when multiple tasks or an operator supplied the input batch", async () => {
  const second: Message = {
    ...greeting,
    task_id: "root/spawn-2",
    task_handle: "task-2",
    reporting_thread_id: "worker-2",
    parts: [
      {
        kind: "text",
        text: "Message from delegated task:\nTask: task-2\nMessage:\nThe forecast is ready.",
      },
    ],
  };
  const { rerender } = render(
    <Transcript
      messages={[...assignment, greeting, second]}
      live="Both workers reported in."
      liveTurn={3}
    />,
  );
  expect(screen.getByLabelText("Streaming response").textContent).toBe(
    "Both workers reported in.",
  );
  rerender(
    <Transcript
      messages={[
        ...assignment,
        greeting,
        {
          role: "user",
          source: "operator",
          parts: [{ kind: "text", text: "Summarize the update for me." }],
        },
      ]}
      live="Here is your summary."
      liveTurn={3}
    />,
  );
  expect(screen.getByLabelText("Streaming response").textContent).toBe(
    "Here is your summary.",
  );
  expect(
    screen.queryByRole("region", { name: "Conversation with task-1" }),
  ).toBeNull();
});

// agentmesh: [[subagents#Console]]
it("keeps a pending delegation open when its result and first child message arrive", async () => {
  const { rerender } = render(
    <Transcript messages={assignment.slice(0, 2)} responding />,
  );
  await userEvent.click(screen.getByText("Delegating 1 task"));
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with subagent" }),
  );
  const board = screen.getByRole("region", {
    name: "Conversation with subagent",
  });
  expect(within(board).getByText("Awaiting result")).toBeTruthy();
  rerender(<Transcript messages={[...assignment, greeting]} />);
  expect(screen.getByRole("region", { name: "Conversation with task-1" })).toBe(
    board,
  );
  expect(within(board).queryByText("Awaiting result")).toBeNull();
  expect(within(board).getAllByRole("listitem")).toHaveLength(2);
});
