import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps, ReactNode } from "react";
import { SWRConfig } from "swr";
import { describe, expect, it, vi } from "vitest";

import { Conversation } from "@/features/threads/conversation";
import { useThread } from "@/features/threads/use-thread";
import type { Agent, Chat } from "@/lib/api";
import type { AgentThreads, Message, ThreadDetail } from "@/lib/api-types";
import { MockEventSource } from "@/test/mock-event-source";
import { uiStream } from "@/test/ui-stream";

// Ported from agentmesh console tests/conversation.test.tsx and
// tests/stream-recovery.test.tsx. Agentmesh polls thread details and follows a
// WebSocket; Hatchery reads the stored transcript, streams the running turn
// through `useChat`, and refreshes on the chat's event stream (SSE).

const chat: Chat = {
  id: "chat_one",
  user_id: "user_test",
  agent_id: "mira",
  title: "First conversation",
  topic: "First conversation",
  trigger: "ui",
  status: "done",
  sandbox_id: null,
  artifact: null,
  attention_reason: null,
  archived_at: null,
  created_at: "2026-09-25T00:00:00Z",
};
const agents: Agent[] = [
  {
    id: "mira",
    name: "Mira",
    repos: [],
    resources: [],
    color: "blue-700",
    created_at: "2026-09-25T00:00:00Z",
  },
];
const saved: ThreadDetail = {
  thread_id: "thread-one",
  chat_id: "chat_one",
  owner: "mira",
  parent_thread_id: "",
  depth: 0,
  upstream: "main",
  children: [],
  status: "idle",
  live: true,
  archived: false,
  task_id: "",
  task_handle: "",
  task_status: "",
  deliverables: [],
  summary: "First conversation",
  result: "",
  error: "",
  input_tokens: 14,
  output_tokens: 3,
  proposals: [],
  turns: 1,
  revision: 12,
  compactions: 0,
  branch: "threads/mira/test",
  base_sha: "a".repeat(40),
  checkpoint_sha: "b".repeat(40),
  messages: [],
};
const transcript: Message[] = [
  { role: "user", id: "u1", parts: [{ kind: "text", id: "u1:0", text: "hey!" }] },
  {
    role: "assistant",
    id: "a1",
    turn: 1,
    parts: [{ kind: "text", id: "a1:0", text: "Hello Mira!" }],
  },
];
const roster: AgentThreads = {
  agent_id: "mira",
  waiting: [],
  budget: null,
  threads: [saved],
  local_review: true,
};
const emptyRepository = {
  branch: saved.branch,
  sha: saved.checkpoint_sha,
  base_sha: saved.base_sha,
  main_sha: saved.base_sha,
  merged: false,
  summary: "",
  remote: "file:///test.git",
  local_review: true,
  review: { workspace: "auto", serve: "review", wiki: "review" },
  checkout: null,
  files: [],
  changes: [],
};

type Route = (
  path: string,
  init?: RequestInit,
) => Response | Promise<Response> | undefined;

// The chat's API: transcript, thread details, sandboxes, repository, live files.
function serve(
  routes: {
    detail?: () => ThreadDetail | Response;
    transcript?: () => Message[] | Promise<Response>;
    stream?: () => Response;
  } & { route?: Route } = {},
) {
  const fetch = vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input);
    const custom = routes.route?.(path, init);
    if (custom) return custom;
    if (path === "/api/chats/chat_one/transcript") {
      const value = routes.transcript?.() ?? transcript;
      return value instanceof Promise ? value : Response.json(value);
    }
    if (path === "/api/chats/chat_one/thread") {
      const value = routes.detail?.() ?? saved;
      return value instanceof Response ? value : Response.json(value);
    }
    if (path === "/api/chat/chat_one/stream")
      return routes.stream?.() ?? new Response(null, { status: 204 });
    if (path === "/api/chats/chat_one/sandboxes") return Response.json([]);
    if (path.startsWith("/api/repository")) return Response.json(emptyRepository);
    if (path.includes("/filesystem"))
      return Response.json({ path: "", entries: [], truncated: false });
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(props: Partial<ComponentProps<typeof Conversation>> = {}) {
  return render(
    <SWRConfig
      value={{
        provider: () => new Map(),
        dedupingInterval: 0,
        shouldRetryOnError: false,
      }}
    >
      <Conversation
        chatId="chat_one"
        chat={chat}
        agents={agents}
        agentId="mira"
        roster={roster}
        onAgentChange={() => {}}
        {...props}
      />
    </SWRConfig>,
  );
}

const messages = () => within(screen.getByRole("region", { name: "Messages" }));

describe("conversation", () => {
  it("allows an idle thread to receive another message on the same route", async () => {
    const posted: Array<{ chat_id: string; messages: Array<Record<string, unknown>> }> = [];
    const stream = uiStream();
    serve({
      route: (path, init) => {
        if (path === "/api/chat" && init?.method === "POST") {
          posted.push(JSON.parse(String(init.body)));
          return stream.response;
        }
      },
    });
    mount();
    await screen.findByText("Hello Mira!");
    await waitFor(() =>
      expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
        "Workspace",
        "State",
      ]),
    );
    const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
      name: "Message agent",
    });
    expect(composer.disabled).toBe(false);
    const user = userEvent.setup();
    await user.type(composer, "Remember our plan");
    await user.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0].chat_id).toBe("chat_one");
    expect(posted[0].messages.at(-1)).toMatchObject({
      role: "user",
      parts: [{ type: "text", text: "Remember our plan" }],
    });
    await waitFor(() => expect(composer.value).toBe(""));
    expect(messages().getByText("Remember our plan")).toBeTruthy();
  });

  it("keeps a follow-up queued above the composer until its durable turn begins", async () => {
    let sentId = "";
    let durable = transcript;
    serve({
      detail: () => ({ ...saved, status: "active" }),
      transcript: () => durable,
      route: (path, init) => {
        if (path === "/api/chat" && init?.method === "POST") {
          sentId = JSON.parse(String(init.body)).messages.at(-1).id;
          return uiStream().response;
        }
      },
    });
    mount();
    await screen.findByText("Hello Mira!");
    await screen.findByRole("button", { name: "Stop" });
    const user = userEvent.setup();
    await user.type(
      screen.getByRole("textbox", { name: "Message agent" }),
      "Do this after the current response",
    );
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Submit" }));

    const queue = await screen.findByRole("region", { name: "Queued messages" });
    expect(within(queue).getByText("Do this after the current response")).toBeTruthy();
    expect(within(queue).getByText("Queued")).toBeTruthy();
    expect(messages().queryByText("Do this after the current response")).toBeNull();

    await waitFor(() => expect(sentId).not.toBe(""));
    durable = [
      ...transcript,
      {
        role: "user",
        id: sentId,
        parts: [{ kind: "text", text: "Do this after the current response" }],
      },
    ];
    act(() => MockEventSource.emit({ type: "messages.changed" }));
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Queued messages" })).toBeNull(),
    );
    expect(messages().getAllByText("Do this after the current response")).toHaveLength(1);
  });

  it("replaces provisional output with one durable response after settlement", async () => {
    const stream = uiStream();
    let durable = transcript.slice(0, 1);
    serve({
      detail: () => ({ ...saved, turns: 0, status: "active" }),
      transcript: () => durable,
      stream: () => stream.response,
    });
    mount();
    await screen.findByText("hey!");
    act(() => {
      stream.push({ type: "start", messageId: "live-1" }, { type: "start-step" });
      stream.text("t1", "Hello Mira!");
    });
    await screen.findByLabelText("Streaming response");
    expect(screen.getAllByText("Hello Mira!")).toHaveLength(1);

    durable = transcript;
    act(() => {
      stream.push({ type: "text-end", id: "t1" }, { type: "finish-step" }, { type: "finish" });
      stream.close();
    });
    await waitFor(() => {
      expect(screen.queryByLabelText("Streaming response")).toBeNull();
      expect(screen.getAllByText("Hello Mira!")).toHaveLength(1);
    });
    // A later refresh must never resurrect the provisional copy.
    act(() => MockEventSource.emit({ type: "messages.changed" }));
    await waitFor(() => expect(screen.getAllByText("Hello Mira!")).toHaveLength(1));
  });

  it("shows a recoverable budget pause instead of an active subagent message", async () => {
    const held: ThreadDetail = {
      ...saved,
      status: "active",
      activity: {
        status: "active",
        phase: "idle",
        sandbox_active: false,
        mailbox_depth: 0,
        pending_prompts: 0,
        queued_tools: 0,
        running_tool: null,
        consolidating: [],
        awaiting_admission: true,
        budget_held: true,
        quiet_until: 0,
        schedules: [],
        updated_at: 1_789_000_000,
      },
    };
    const heldRoster: AgentThreads = {
      ...roster,
      waiting: [held.thread_id],
      budget: {
        spent: 1_002_734,
        limit: 1_000_000,
        remaining: 0,
        exhausted: true,
        resets_at: 1_789_344_000,
      },
      threads: [{ ...held, status: "waiting" }],
    };
    const posts: Array<{ path: string; body: Record<string, unknown> }> = [];
    serve({
      detail: () => held,
      transcript: () => [
        transcript[0],
        {
          role: "assistant",
          parts: [
            {
              kind: "tool_call",
              tool_name: "message_task",
              tool_call_id: "relay-1",
              tool_args: '{"task_id":"task-1","text":"Continue the conversation"}',
            },
          ],
        },
        {
          role: "tool",
          parts: [
            {
              kind: "tool_result",
              tool_name: "message_task",
              tool_call_id: "relay-1",
              result: { status: "accepted", task_id: "task-1" },
            },
          ],
        },
      ],
      route: (path, init) => {
        if (init?.method === "POST") {
          posts.push({ path, body: JSON.parse(String(init.body)) });
          return Response.json({ outcome: "sent" }, { status: 202 });
        }
      },
    });
    mount({ roster: heldRoster });

    expect(await screen.findByText("Requested subagent follow-up")).toBeTruthy();
    expect(screen.queryByText("Messaging subagents")).toBeNull();
    expect(await screen.findByRole("region", { name: "Daily budget exhausted" })).toBeTruthy();
    expect(screen.getByText(/1 thread paused\./)).toBeTruthy();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Grant tokens" }));
    const amount = screen.getByRole("spinbutton", { name: "Additional tokens" });
    await user.clear(amount);
    await user.type(amount, "250000");
    await user.click(screen.getByRole("button", { name: "Grant" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].path).toBe("/api/agents/mira/grants");
    expect(posts[0].body.amount).toBe(250_000);
    expect(posts[0].body.request_id).toEqual(expect.any(String));
  });

  it("discards a failed step before displaying a retried response", async () => {
    // Hatchery's stream marks a discarded model call with a `data-reload` part.
    const stream = uiStream();
    serve({
      detail: () => ({ ...saved, turns: 0, status: "active" }),
      transcript: () => [],
      stream: () => stream.response,
    });
    mount();
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      stream.push({ type: "start", messageId: "live-2" }, { type: "start-step" });
      stream.text("t1", "Failed attempt");
    });
    await screen.findByText("Failed attempt");
    act(() => {
      stream.push(
        { type: "text-end", id: "t1" },
        { type: "finish-step" },
        { type: "data-reload", data: {} },
        { type: "start-step" },
      );
      stream.text("t2", "Recovered response");
    });
    await screen.findByText("Recovered response");
    expect(screen.queryByText("Failed attempt")).toBeNull();
  });
});

it("keeps the reply and draft visible while reviewing multiple files and proposals", async () => {
  const proposal = {
    section: "wiki",
    branch: "consolidations/mira/wiki/proposal",
    merged: false,
    url: "",
  };
  const posted: Array<{ path: string; body: Record<string, unknown> }> = [];
  serve({
    detail: () => ({ ...saved, proposals: [proposal] }),
    route: (path, init) => {
      if (init?.method === "POST") {
        posted.push({ path, body: JSON.parse(String(init.body)) });
        return Response.json({ branch: proposal.branch, commit: "e".repeat(40) });
      }
      if (path.startsWith("/api/repository")) {
        const params = new URL(path, "http://hatchery.test").searchParams;
        if (params.has("path"))
          return Response.json({
            path: params.get("path"),
            diff: params.has("proposal")
              ? `+Curated ${params.get("path")}`
              : `+Changed ${params.get("path")}`,
            after: { text: "Full file contents", mode: "100644" },
          });
        return Response.json({
          ...emptyRepository,
          sha: "d".repeat(40),
          changes: [
            { path: "wiki/procedure.md", status: "added" },
            { path: "agents/mira/notes.md", status: "modified" },
          ],
        });
      }
    },
  });
  mount();
  const user = userEvent.setup();
  await screen.findByRole("tab", { name: "Changes" });
  expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "Workspace",
    "Changes",
    "State",
  ]);
  expect(screen.getByRole("tab", { name: "Workspace" }).getAttribute("aria-selected")).toBe(
    "true",
  );
  await user.click(screen.getByRole("tab", { name: "Changes" }));
  await screen.findByText("+Changed wiki/procedure.md");
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  await user.type(composer, "Keep this draft while I review");
  await user.click(screen.getByRole("tab", { name: "State" }));
  expect(screen.getByRole("tabpanel", { name: "State" })).toBeTruthy();
  expect(composer.value).toBe("Keep this draft while I review");
  await user.keyboard("{Home}");
  expect(screen.getByRole("tab", { name: "Workspace" }).getAttribute("aria-selected")).toBe(
    "true",
  );
  await user.click(screen.getByRole("tab", { name: "Changes" }));
  await screen.findByText("+Changed agents/mira/notes.md");
  expect(screen.getAllByLabelText("File diff")).toHaveLength(2);
  await user.click(
    screen.getByRole("button", { name: "Show full file: agents/mira/notes.md" }),
  );
  await screen.findByText("Full file contents");
  await user.click(screen.getByRole("button", { name: "Wiki proposal" }));
  await screen.findByText("+Curated wiki/procedure.md");
  expect(screen.getByText("Hello Mira!")).toBeTruthy();
  expect(screen.getByRole("textbox", { name: "Message agent" })).toBe(composer);
  expect(composer.value).toBe("Keep this draft while I review");
  await user.click(screen.getByRole("button", { name: "Approve & merge" }));
  await waitFor(() => expect(posted).toHaveLength(1));
  expect(posted[0].path).toBe("/api/chats/chat_one/thread/approve");
  expect(posted[0].body).toEqual({
    branch: proposal.branch,
    expected_sha: "d".repeat(40),
  });
});

describe("stream recovery", () => {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      {children}
    </SWRConfig>
  );

  it("reports a server failure instead of treating it as a thread that has not started", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "Thread unavailable" }, { status: 503 })),
    );
    const { result } = renderHook(() => useThread("chat_one"), { wrapper });
    await waitFor(() => expect(result.current.error?.message).toBe("Thread unavailable"));
    expect(result.current.data).toBeUndefined();
  });

  it("treats a chat whose thread has not started as having no details yet", async () => {
    const fetch = vi.fn(async () =>
      Response.json({ detail: "the chat has no thread yet" }, { status: 404 }),
    );
    vi.stubGlobal("fetch", fetch);
    const { result } = renderHook(() => useThread("chat_one"), { wrapper });
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(result.current.error).toBeUndefined();
    expect(result.current.data).toBeUndefined();
  });

  it("refreshes queue state from the chat event stream while a background activation streams nothing", async () => {
    let activity = { phase: "idle", mailbox_depth: 0 };
    const quiet = {
      status: "idle",
      sandbox_active: true,
      pending_prompts: 0,
      queued_tools: 0,
      running_tool: null,
      consolidating: [],
      awaiting_admission: false,
      budget_held: false,
      quiet_until: 0,
      schedules: [],
      updated_at: 1,
    };
    serve({ detail: () => ({ ...saved, activity: { ...quiet, ...activity } }) });
    mount();
    await screen.findByText("Hello Mira!");
    expect(screen.queryByText("Working in background")).toBeNull();
    activity = { phase: "leased", mailbox_depth: 3 };
    act(() => MockEventSource.emit({ type: "chat.changed" }));
    expect(await screen.findByText("Working in background")).toBeTruthy();
  });

  it("a durable refresh replaces a live draft when the stream connection was lost", async () => {
    const stream = uiStream();
    let durable: Message[] = transcript.slice(0, 1);
    serve({
      detail: () => ({ ...saved, turns: 0, status: "active" }),
      transcript: () => durable,
      stream: () => stream.response,
    });
    mount();
    await screen.findByText("hey!");
    act(() => {
      stream.push({ type: "start", messageId: "live-3" }, { type: "start-step" });
      stream.text("t1", "One reply");
    });
    await screen.findByText("One reply");
    durable = [
      ...durable,
      { role: "assistant", id: "a2", turn: 1, parts: [{ kind: "text", text: "One reply" }] },
    ];
    act(() => stream.fail());
    await waitFor(() => {
      expect(screen.getAllByText("One reply")).toHaveLength(1);
      expect(screen.queryByLabelText("Streaming response")).toBeNull();
    });
  });

  it("keeps committed live text visible until the durable refresh arrives", async () => {
    const stream = uiStream();
    let finishRefresh: ((response: Response) => void) | undefined;
    let deferRefresh = false;
    serve({
      detail: () => ({ ...saved, turns: 0, status: "active" }),
      transcript: () =>
        deferRefresh
          ? new Promise<Response>((resolve) => {
              finishRefresh = resolve;
            })
          : transcript.slice(0, 1),
      stream: () => stream.response,
    });
    mount();
    await screen.findByText("hey!");
    act(() => {
      stream.push({ type: "start", messageId: "live-4" }, { type: "start-step" });
      stream.text("t1", "Continuous reply");
    });
    await screen.findByText("Continuous reply");
    deferRefresh = true;
    act(() => {
      stream.push({ type: "text-end", id: "t1" }, { type: "finish-step" }, { type: "finish" });
      stream.close();
    });
    await waitFor(() => expect(finishRefresh).toBeDefined());
    expect(screen.getAllByText("Continuous reply")).toHaveLength(1);
    act(() =>
      finishRefresh?.(
        Response.json([
          transcript[0],
          {
            role: "assistant",
            id: "a3",
            turn: 1,
            parts: [{ kind: "text", text: "Continuous reply" }],
          },
        ]),
      ),
    );
    await waitFor(() => expect(screen.queryByLabelText("Streaming response")).toBeNull());
    expect(screen.getAllByText("Continuous reply")).toHaveLength(1);
  });
});
