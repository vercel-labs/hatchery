import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Transcript } from "@/features/threads/transcript";
import type { Message } from "@/lib/api-types";

afterEach(() => vi.unstubAllGlobals());

const command: Message = {
  role: "assistant",
  parts: [
    {
      id: "call-part",
      kind: "tool_call",
      tool_name: "bash",
      tool_args:
        '{"command":"cat memories/note.md","description":"Read the saved note"}',
      tool_call_id: "read-note",
    },
  ],
};

async function openWork() {
  const summary = screen
    .getByRole("group", { name: "Agent activity" })
    .querySelector("summary");
  if (!summary) throw new Error("Work disclosure summary was not rendered");
  await userEvent.setup().click(summary);
}

async function openTool(card: HTMLElement) {
  const summary = card.querySelector("summary");
  if (!summary) throw new Error("Tool disclosure summary was not rendered");
  await userEvent.setup().click(summary);
}
const result: Message = {
  role: "tool",
  parts: [
    {
      id: "result-part",
      kind: "tool_result",
      tool_name: "bash",
      tool_call_id: "read-note",
      result: {
        exit_code: 7,
        stdout: "partial output",
        stderr: "note not found",
      },
    },
  ],
};

// agentmesh: [[gateway#Thread activity tests]]
it("distinguishes a queued command from the running one and shows results before the batch is flushed", async () => {
  const messages: Message[] = [
    {
      role: "assistant",
      parts: [
        {
          kind: "tool_call",
          id: "first-part",
          tool_name: "bash",
          tool_call_id: "first",
          tool_args: '{"command":"sleep 15"}',
        },
        {
          kind: "tool_call",
          id: "second-part",
          tool_name: "bash",
          tool_call_id: "second",
          tool_args: '{"command":"sleep 15"}',
        },
      ],
    },
  ];
  const { rerender } = render(
    <Transcript
      messages={messages}
      toolProgress={{ running: "first", queued: ["second"], results: [] }}
    />,
  );
  const user = userEvent.setup();
  const working = screen.getByText("Working");
  expect(working.className).toBe("thinking-shimmer text-[11px] font-medium");
  await user.click(working);
  const [first, second] = screen.getAllByRole("group", {
    name: "bash tool call",
  });
  expect(within(first).getByText("Running")).toBeTruthy();
  expect(within(second).getByText("Queued")).toBeTruthy();
  await openTool(first);

  const firstResult = {
    kind: "tool_result",
    tool_call_id: "first",
    tool_name: "bash",
    result: { exit_code: 0, stdout: "first complete", stderr: "" },
  };
  rerender(
    <Transcript
      messages={messages}
      toolProgress={{ running: "second", queued: [], results: [firstResult] }}
    />,
  );
  expect(within(first).getByText("Completed")).toBeTruthy();
  expect(within(first).getByLabelText("Tool output").textContent).toBe(
    "first complete",
  );
  expect(first.hasAttribute("open")).toBe(true);
  expect(within(second).getByText("Running")).toBeTruthy();
  expect(screen.queryByText("Awaiting result")).toBeNull();

  const secondResult = {
    kind: "tool_result",
    tool_call_id: "second",
    tool_name: "bash",
    result: { exit_code: 7, stdout: "", stderr: "second failed" },
  };
  rerender(
    <Transcript
      messages={[
        ...messages,
        { role: "tool", parts: [firstResult, secondResult] },
      ]}
      toolProgress={{ running: null, queued: [], results: [] }}
    />,
  );
  expect(screen.getAllByRole("group", { name: "bash tool call" })).toHaveLength(
    2,
  );
  expect(within(first).getByText("Completed")).toBeTruthy();
  expect(within(second).getByText("Failed")).toBeTruthy();
  expect(screen.queryByText("Running")).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("keeps the same user bubble when an optimistic prompt becomes durable", () => {
  const optimistic: Message = {
    role: "user",
    request_id: "request-17",
    timestamp: 1788890518,
    pending: "queued",
    parts: [{ kind: "text", text: "Keep this visible" }],
  };
  const { rerender, container } = render(
    <Transcript messages={[optimistic]} />,
  );
  const text = screen.getByText("Keep this visible");
  const article = text.closest("article");
  const bubble = text.parentElement;
  expect(article?.className).toContain("items-end");
  expect(bubble?.className).toContain("w-fit");
  expect(screen.getByRole("status", { name: "Message queued" })).toBeTruthy();
  expect(container.querySelector("time")).toBeNull();

  rerender(
    <Transcript
      messages={[
        {
          role: "user",
          request_id: "request-17",
          timestamp: 1788890520,
          parts: [
            { kind: "text", id: "saved-user", text: "Keep this visible" },
          ],
        },
      ]}
    />,
  );
  expect(screen.getByText("Keep this visible")).toBe(text);
  expect(text.closest("article")).toBe(article);
  expect(text.parentElement).toBe(bubble);
  expect(screen.queryByRole("status", { name: "Message queued" })).toBeNull();
  expect(container.querySelector("time")?.dateTime).toBe(
    "2026-09-08T18:02:00.000Z",
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("keeps the same assistant text node when streaming output becomes durable", () => {
  const prompt: Message = {
    role: "user",
    request_id: "request-18",
    timestamp: 1788890520,
    parts: [{ kind: "text", text: "Stream smoothly" }],
  };
  const { rerender } = render(
    <Transcript messages={[prompt]} pendingTurn={3} responding />,
  );
  const article = screen
    .getByRole("status", { name: "Assistant is thinking" })
    .closest("article");
  expect(screen.getByText("Thinking").className).toBe(
    "thinking-shimmer text-[11px] font-medium",
  );

  rerender(
    <Transcript
      messages={[prompt]}
      live="No flashing"
      liveTurn={3}
      responding
    />,
  );
  const text = screen.getByLabelText("Streaming response");
  expect(text.closest("article")).toBe(article);

  rerender(
    <Transcript
      messages={[
        prompt,
        {
          role: "assistant",
          turn: 3,
          timestamp: 1788890524,
          parts: [{ kind: "text", id: "saved-reply", text: "No flashing" }],
        },
      ]}
    />,
  );
  expect(screen.getByText("No flashing").closest("[data-message-text]")).toBe(
    text,
  );
  expect(text.closest("article")).toBe(article);
  expect(screen.queryByLabelText("Streaming response")).toBeNull();
  expect(
    screen.queryByRole("status", { name: "Assistant is thinking" }),
  ).toBeNull();
  expect(article?.querySelector("time")?.dateTime).toBe(
    "2026-09-08T18:02:04.000Z",
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("renders assistant responses as Markdown while leaving user messages plain", () => {
  render(
    <Transcript
      messages={[
        {
          role: "user",
          parts: [{ kind: "text", text: "Keep **my prompt** plain" }],
        },
        {
          role: "assistant",
          parts: [
            {
              kind: "text",
              text: [
                "Use **four skills**:",
                "",
                "1. `api`",
                "2. [coding guide](https://example.com/guide)",
              ].join("\n"),
            },
          ],
        },
      ]}
    />,
  );

  expect(
    screen.getByText("Keep **my prompt** plain").querySelector("strong"),
  ).toBeNull();
  expect(screen.getByText("four skills").tagName).toBe("STRONG");
  expect(screen.getByText("api").tagName).toBe("CODE");
  const link = screen.getByRole("link", { name: "coding guide" });
  expect(link.getAttribute("href")).toBe("https://example.com/guide");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(link.getAttribute("rel")).toBe("noreferrer");
});

// agentmesh: [[gateway#Console regression tests]]
it("keeps a new live response separate from the previous completed reply before the next user message commits", () => {
  render(
    <Transcript
      messages={[
        { role: "user", parts: [{ kind: "text", text: "Hello" }] },
        { role: "assistant", parts: [{ kind: "text", text: "Hi there." }] },
      ]}
      live="Here is the answer to your next question."
    />,
  );
  expect(
    screen.getByLabelText("Streaming response").closest("article"),
  ).not.toBe(screen.getByText("Hi there.").closest("article"));
});

// agentmesh: [[gateway#Console regression tests]]
it("groups a command, its collapsible result, and the final answer under one assistant label", async () => {
  render(
    <Transcript
      messages={[
        { role: "user", parts: [{ kind: "text", text: "Read my note" }] },
        command,
        result,
        {
          role: "assistant",
          parts: [{ kind: "text", text: "The note is missing." }],
        },
      ]}
    />,
  );
  expect(screen.getAllByText("assistant", { exact: true })).toHaveLength(1);
  expect(screen.queryByText("tool", { exact: true })).toBeNull();
  const activity = screen.getByText("Worked").closest("details");
  expect(activity?.hasAttribute("open")).toBe(false);
  await openWork();
  expect(activity?.hasAttribute("open")).toBe(true);
  const card = screen.getByRole("group", { name: "bash tool call" });
  expect(within(card).getByText("Read the saved note")).toBeTruthy();
  expect(within(card).getByText(/exit 7/)).toBeTruthy();
  expect(card.hasAttribute("open")).toBe(false);
  await openTool(card);
  expect(card.hasAttribute("open")).toBe(true);
  expect(within(card).getByLabelText("Command").textContent).toBe(
    "cat memories/note.md",
  );
  expect(within(card).getByLabelText("Tool output").textContent).toBe(
    "partial output",
  );
  expect(within(card).getByLabelText("Standard error").textContent).toBe(
    "note not found",
  );
  expect(card.closest("article")?.textContent).toContain(
    "The note is missing.",
  );
  expect(screen.queryByText(/undefined/)).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("labels exit 124 as timed out while preserving captured output", async () => {
  const timeoutResult: Message = {
    role: "tool",
    parts: [
      {
        kind: "tool_result",
        tool_name: "bash",
        tool_call_id: "read-note",
        result: {
          exit_code: 124,
          stdout: "clone completed",
          stderr: "command killed after 300s",
        },
      },
    ],
  };
  render(<Transcript messages={[command, timeoutResult]} />);
  await openWork();
  const card = screen.getByRole("group", { name: "bash tool call" });
  expect(within(card).getByText("Timed out")).toBeTruthy();
  expect(within(card).getByText(/exit 124/)).toBeTruthy();
  expect(within(card).queryByText("Failed")).toBeNull();

  await openTool(card);
  expect(within(card).getByLabelText("Tool output").textContent).toBe(
    "clone completed",
  );
  expect(within(card).getByLabelText("Standard error").textContent).toBe(
    "command killed after 300s",
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("shows durable work time and keeps tool-turn notes inside the work disclosure", async () => {
  const { container } = render(
    <Transcript
      messages={[
        {
          role: "user",
          timestamp: 1788890500,
          parts: [{ kind: "text", text: "Check the note" }],
        },
        {
          role: "assistant",
          timestamp: 1788890503,
          turn: 4,
          parts: [
            {
              kind: "text",
              text: "I’ll inspect the saved note before answering.",
            },
            {
              kind: "tool_call",
              tool_name: "bash",
              tool_call_id: "timed-read",
              tool_args:
                '{"command":"cat memories/note.md","description":"Read the saved note"}',
            },
          ],
        },
        {
          role: "tool",
          timestamp: 1788890509,
          parts: [
            {
              kind: "tool_result",
              tool_name: "bash",
              tool_call_id: "timed-read",
              result: { exit_code: 0, stdout: "Remember this", stderr: "" },
            },
          ],
        },
        {
          role: "assistant",
          timestamp: 1788890511,
          parts: [{ kind: "text", text: "The note says remember this." }],
        },
      ]}
    />,
  );

  const disclosure = screen.getByText("Worked for 11s").closest("details");
  const note = screen.getByText(
    "I’ll inspect the saved note before answering.",
  );
  const answer = screen.getByText("The note says remember this.");
  expect(disclosure?.contains(note)).toBe(true);
  expect(disclosure?.contains(answer)).toBe(false);
  expect(disclosure?.hasAttribute("open")).toBe(false);
  expect(
    Array.from(container.querySelectorAll("time"), (time) => time.dateTime),
  ).toEqual(["2026-09-08T18:01:40.000Z", "2026-09-08T18:01:51.000Z"]);
  await userEvent.setup().click(screen.getByText("Worked for 11s"));
  expect(disclosure?.hasAttribute("open")).toBe(true);
});

// agentmesh: [[gateway#Console regression tests]]
it("uses a compact Bash summary and terminal-style expanded output for older calls", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "assistant",
          parts: [
            {
              kind: "tool_call",
              tool_name: "bash",
              tool_call_id: "echo",
              tool_args: '{"command":"echo \'hello world\'"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "bash",
              tool_call_id: "echo",
              result: { exit_code: 0, stdout: "hello world", stderr: "" },
            },
          ],
        },
      ]}
    />,
  );
  await openWork();
  const card = screen.getByRole("group", { name: "bash tool call" });
  expect(within(card).getByText("Echo hello world in Bash")).toBeTruthy();
  const [terminal, disclosure] = card.querySelectorAll("summary svg");
  expect(terminal.className.baseVal).toContain("group-hover/tool:opacity-0");
  expect(disclosure.className.baseVal).toContain(
    "group-hover/tool:opacity-100",
  );
  expect(disclosure.className.baseVal).not.toContain("group-focus-within/tool");
  expect(within(card).getByText("Completed").className).toContain("sr-only");
  await openTool(card);
  expect(within(card).getByLabelText("Command").textContent).toBe(
    "echo 'hello world'",
  );
  expect(within(card).getByLabelText("Tool output").textContent).toBe(
    "hello world",
  );
  expect(within(card).queryByText("Command")).toBeNull();
  expect(within(card).queryByText("Output")).toBeNull();
});

// agentmesh: [[gateway#Thread activity tests]]
it("shimmers while reading a skill and changes to past tense when the read completes", async () => {
  const skillCall: Message = {
    role: "assistant",
    parts: [
      {
        kind: "tool_call",
        tool_name: "skill_view",
        tool_call_id: "read-skill",
        tool_args: '{"name":"react-best-practices","file_path":null}',
      },
    ],
  };
  const { rerender } = render(
    <Transcript
      messages={[skillCall]}
      toolProgress={{ running: "read-skill", queued: [], results: [] }}
    />,
  );
  await openWork();
  const card = screen.getByRole("group", { name: "skill_view tool call" });
  const reading = within(card).getByText("Reading React Best Practices skill");
  expect(reading.className).toContain("thinking-shimmer");
  expect(card.querySelector(".lucide-wrench")).toBeTruthy();
  expect(within(card).queryByLabelText("Command")).toBeNull();

  rerender(
    <Transcript
      messages={[
        skillCall,
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "skill_view",
              tool_call_id: "read-skill",
              result: {
                path: "skills/react-best-practices/SKILL.md",
                content: "Use stable keys.",
              },
            },
          ],
        },
      ]}
    />,
  );
  const completed = within(card).getByText("Read React Best Practices skill");
  expect(completed.className).not.toContain("thinking-shimmer");
  expect(within(card).getByText("Completed")).toBeTruthy();
  expect(screen.queryByText(/in Bash/)).toBeNull();
  await openTool(card);
  expect(within(card).getByLabelText("Tool input").textContent).toContain(
    '"name": "react-best-practices"',
  );
});

// agentmesh: [[gateway#Thread activity tests]]
it("uses distinct verbs and icons for every non-Bash tool family", async () => {
  const calls = [
    {
      kind: "tool_call",
      tool_name: "signal",
      tool_call_id: "morning-signal",
      tool_args: '{"note":"Prepare the brief","delay":"15m"}',
    },
    {
      kind: "tool_call",
      tool_name: "cancel",
      tool_call_id: "cancel-signal",
      tool_args: '{"signal_id":"old-signal"}',
    },
    {
      kind: "tool_call",
      tool_name: "open_repository",
      tool_call_id: "open-repository",
      tool_args: '{"repository":"vercel/ai"}',
    },
    {
      kind: "tool_call",
      tool_name: "delegate",
      tool_call_id: "delegate-task",
      tool_args: '{"objective":"Review the SDK","repository":"vercel/ai"}',
    },
    {
      kind: "tool_call",
      tool_name: "message_parent",
      tool_call_id: "message-parent",
      tool_args: '{"text":"Reviewing the SDK now"}',
    },
    {
      kind: "tool_call",
      tool_name: "complete",
      tool_call_id: "complete-task",
      tool_args:
        '{"summary":"Review complete","result":"The SDK passed review","deliverables":null}',
    },
  ];
  render(
    <Transcript
      messages={[
        { role: "assistant", parts: calls },
        {
          role: "tool",
          parts: calls.map((call) => ({
            kind: "tool_result",
            tool_name: call.tool_name,
            tool_call_id: call.tool_call_id,
            result: { status: "ok" },
          })),
        },
      ]}
    />,
  );
  await openWork();

  const signal = screen.getByRole("group", { name: "signal tool call" });
  expect(within(signal).getByText("Scheduled signal for 15m")).toBeTruthy();
  expect(signal.querySelector(".lucide-calendar-clock")).toBeTruthy();

  const cancel = screen.getByRole("group", { name: "cancel tool call" });
  expect(within(cancel).getByText("Cancelled signal")).toBeTruthy();
  expect(cancel.querySelector(".lucide-calendar-x-2")).toBeTruthy();

  const repository = screen.getByRole("group", {
    name: "open_repository tool call",
  });
  expect(within(repository).getByText("Opened vercel/ai")).toBeTruthy();
  expect(repository.querySelector(".lucide-git-branch")).toBeTruthy();

  const delegate = screen.getByRole("button", {
    name: "Expand conversation with subagent",
  });
  expect(within(delegate).getByText("Delegated task")).toBeTruthy();
  expect(delegate.querySelector(".lucide-git-fork")).toBeTruthy();

  const parentMessage = screen.getByRole("region", {
    name: "Message to parent agent",
  });
  expect(within(parentMessage).getByText("Sent to parent agent")).toBeTruthy();
  expect(within(parentMessage).getByText("Reviewing the SDK now")).toBeTruthy();
  expect(parentMessage.querySelector(".lucide-send")).toBeTruthy();

  const completion = screen.getByRole("group", { name: "complete tool call" });
  expect(
    within(completion).getByText("Completion handoff started"),
  ).toBeTruthy();
  expect(completion.querySelector(".lucide-clipboard-check")).toBeTruthy();
});

// agentmesh: [[gateway#Console regression tests]]
it("shows a secret request as an inline approval prompt and saves it securely", async () => {
  const requests: Array<{ path: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      requests.push({ path, init });
      return Response.json({ name: "CALLBACK_BEARER_TOKEN", outcome: "sent" });
    }),
  );
  const user = userEvent.setup();
  render(
    <Transcript
      owner="mira"
      messages={[
        {
          role: "assistant",
          parts: [
            {
              kind: "tool_call",
              tool_name: "secret_request",
              tool_call_id: "request-secret",
              tool_args: JSON.stringify({
                name: "CALLBACK_BEARER_TOKEN",
                note: "Use this to authenticate callback requests.",
              }),
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "secret_request",
              tool_call_id: "request-secret",
              result: {
                name: "CALLBACK_BEARER_TOKEN",
                status: "requested",
              },
            },
          ],
        },
      ]}
    />,
  );

  const card = screen.getByRole("region", {
    name: "Secret request for CALLBACK_BEARER_TOKEN",
  });
  expect(within(card).getByText("Secret requested")).toBeTruthy();
  expect(
    within(card).getByText("Use this to authenticate callback requests."),
  ).toBeTruthy();
  expect(screen.queryByText(/^Work/)).toBeNull();
  expect(screen.queryByLabelText("Tool input")).toBeNull();

  const input = within(card).getByLabelText<HTMLInputElement>(
    "Value for CALLBACK_BEARER_TOKEN",
  );
  await user.type(input, "callback-secret");
  await user.click(
    within(card).getByRole("button", { name: "Approve & save" }),
  );

  await waitFor(() =>
    expect(within(card).getByText("Secret saved for this agent.")).toBeTruthy(),
  );
  expect(requests).toHaveLength(1);
  expect(requests[0].path).toBe(
    "/api/agents/mira/secrets/CALLBACK_BEARER_TOKEN",
  );
  // Hatchery authenticates with its session cookie.
  expect(requests[0].init?.credentials).toBe("include");
  expect(requests[0].init?.headers).toMatchObject({
    "Content-Type": "application/json",
  });
  expect(JSON.parse(String(requests[0].init?.body))).toMatchObject({
    request_id: expect.any(String),
    value: "callback-secret",
  });
  expect(screen.queryByDisplayValue("callback-secret")).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("pairs multiple bash calls by ID even when results arrive in a different order", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "assistant",
          parts: [
            {
              kind: "tool_call",
              tool_name: "bash",
              tool_call_id: "first",
              tool_args: '{"command":"pwd"}',
            },
            {
              kind: "tool_call",
              tool_name: "bash",
              tool_call_id: "second",
              tool_args: '{"command":"date"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "bash",
              tool_call_id: "second",
              result_kind: "error",
              result: "Sandbox unavailable",
            },
            {
              kind: "tool_result",
              tool_name: "bash",
              tool_call_id: "first",
              result: { exit_code: 0, stdout: "/workspace/self", stderr: "" },
            },
          ],
        },
      ]}
    />,
  );
  await openWork();
  const cards = screen.getAllByRole("group", { name: "bash tool call" });
  expect(cards).toHaveLength(2);
  expect(within(cards[0]).getByLabelText("Command").textContent).toBe("pwd");
  expect(within(cards[0]).getByLabelText("Tool output").textContent).toBe(
    "/workspace/self",
  );
  expect(within(cards[0]).getByText("Completed")).toBeTruthy();
  expect(within(cards[1]).getByLabelText("Command").textContent).toBe("date");
  expect(within(cards[1]).getByLabelText("Tool output").textContent).toBe(
    "Sandbox unavailable",
  );
  expect(within(cards[1]).getByText("Failed")).toBeTruthy();
});

// agentmesh: [[gateway#Console regression tests]]
it("keeps disclosure state and streams into the same assistant group as a tool completes", async () => {
  const { rerender } = render(<Transcript messages={[command]} />);
  await openWork();
  const card = screen.getByRole("group", { name: "bash tool call" });
  expect(within(card).getByText("Awaiting result")).toBeTruthy();
  await openTool(card);
  rerender(
    <Transcript messages={[command, result]} live="The note is missing." />,
  );
  expect(screen.getByRole("group", { name: "bash tool call" })).toBe(card);
  expect(card.hasAttribute("open")).toBe(true);
  expect(screen.getByLabelText("Streaming response").closest("article")).toBe(
    card.closest("article"),
  );
  expect(screen.getAllByText("assistant", { exact: true })).toHaveLength(1);
  rerender(
    <Transcript
      messages={[
        command,
        result,
        {
          role: "assistant",
          parts: [{ kind: "text", text: "The note is missing." }],
        },
        { role: "user", parts: [{ kind: "text", text: "Thanks" }] },
        {
          role: "assistant",
          parts: [{ kind: "text", text: "You're welcome." }],
        },
      ]}
    />,
  );
  expect(screen.queryByLabelText("Streaming response")).toBeNull();
  expect(screen.getAllByText("The note is missing.")).toHaveLength(1);
  expect(screen.getAllByText("assistant", { exact: true })).toHaveLength(2);
});

// agentmesh: [[gateway#Console regression tests]]
it("separates a delayed autonomous reply and keeps recorded times stable on rerender", () => {
  const messages: Message[] = [
    {
      role: "assistant",
      timestamp: 1788890520,
      parts: [{ kind: "text", text: "Checking the job." }],
    },
    {
      role: "assistant",
      timestamp: 1788890820,
      parts: [{ kind: "text", text: "The job recovered five minutes later." }],
    },
  ];
  const { rerender, container } = render(<Transcript messages={messages} />);
  expect(screen.getByText("Checking the job.").closest("article")).not.toBe(
    screen
      .getByText("The job recovered five minutes later.")
      .closest("article"),
  );
  expect(
    Array.from(container.querySelectorAll("time"), (time) => time.dateTime),
  ).toEqual(["2026-09-08T18:02:00.000Z", "2026-09-08T18:07:00.000Z"]);
  rerender(
    <Transcript messages={messages} live="Continuing the investigation." />,
  );
  expect(container.querySelectorAll("time")).toHaveLength(2);
  expect(container.querySelector("time")?.dateTime).toBe(
    "2026-09-08T18:02:00.000Z",
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("renders a fired signal as assistant activity above its public reply", () => {
  render(
    <Transcript
      messages={[
        {
          role: "assistant",
          timestamp: 1788890520,
          parts: [{ kind: "text", text: "I scheduled the check." }],
        },
        {
          role: "user",
          source: "signal",
          timestamp: 1788894120,
          parts: [{ kind: "text", text: "Signal: check the website" }],
        },
        {
          role: "assistant",
          timestamp: 1788894120,
          parts: [{ kind: "text", text: "The website is ready." }],
        },
      ]}
    />,
  );
  const signal = screen.getByText("Signal: check the website");
  const reply = screen.getByText("The website is ready.");
  expect(signal.closest("article")).toBe(reply.closest("article"));
  expect(screen.queryByText("user", { exact: true })).toBeNull();
  expect(reply.closest("article")?.querySelector("details")?.open).toBe(false);
});

// agentmesh: [[gateway#Console regression tests]]
it("renders an API handler prompt as a formatted assistant-side signal", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "user",
          source: "api",
          request_id: "api:callback-17",
          timestamp: 1788894120,
          parts: [
            {
              kind: "text",
              text: 'Callback hit: {"method":"GET","path":"/api/callback","query":[],"body":""}',
            },
          ],
        },
        {
          role: "assistant",
          timestamp: 1788894121,
          parts: [{ kind: "text", text: "I received the callback." }],
        },
      ]}
    />,
  );

  const card = screen.getByLabelText("API signal");
  const reply = screen.getByText("I received the callback.");
  expect(card.closest("article")).toBe(reply.closest("article"));
  expect(screen.queryByText("user", { exact: true })).toBeNull();
  expect(within(card).getByText("Callback hit")).toBeTruthy();

  await openWork();
  await openTool(card);
  expect(card.querySelector("pre")?.textContent).toBe(
    'Callback hit:\n{\n  "method": "GET",\n  "path": "/api/callback",\n  "query": [],\n  "body": ""\n}',
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("renders a scheduled prompt as an autonomous schedule signal", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "user",
          source: "schedule",
          request_id: "schedule:cat-joke-17",
          timestamp: 1788894120,
          parts: [{ kind: "text", text: "Generate the next cat joke." }],
        },
        {
          role: "assistant",
          timestamp: 1788894121,
          parts: [
            { kind: "text", text: "Why was the cat so good at video games?" },
          ],
        },
      ]}
    />,
  );

  const card = screen.getByLabelText("Schedule signal");
  const reply = screen.getByText("Why was the cat so good at video games?");
  expect(card.closest("article")).toBe(reply.closest("article"));
  expect(screen.queryByText("user", { exact: true })).toBeNull();
  await openWork();
  await openTool(card);
  expect(card.querySelector("pre")?.textContent).toBe(
    "Generate the next cat joke.",
  );
});

// agentmesh: [[gateway#Console regression tests]]
// agentmesh: [[tests#Delegation forms a supervised thread tree]]
it("renders a delegated task report inside its conversation, not as human input", async () => {
  render(
    <Transcript
      messages={[
        {
          role: "user",
          source: "task",
          task_id: "task-42",
          task_status: "completed",
          timestamp: 1788894120,
          parts: [
            {
              kind: "text",
              text: "Delegated task report:\nStatus: completed\nSummary: Verified",
            },
          ],
        },
        {
          role: "assistant",
          timestamp: 1788894120,
          parts: [{ kind: "text", text: "I integrated the result." }],
        },
      ]}
    />,
  );
  await userEvent.click(
    screen.getByRole("button", { name: "Expand conversation with task-42" }),
  );
  const report = screen.getByText(/Delegated task report/);
  const reply = screen.getByText("I integrated the result.");
  expect(report.closest("article")).toBe(reply.closest("article"));
  expect(screen.queryByText("user", { exact: true })).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("leaves historical messages without fabricated timestamps", () => {
  const { container } = render(
    <Transcript
      messages={[
        {
          role: "assistant",
          parts: [{ kind: "text", text: "An older reply." }],
        },
      ]}
    />,
  );
  expect(screen.getByText("An older reply.")).toBeTruthy();
  expect(container.querySelector("time")).toBeNull();
});
