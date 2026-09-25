import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { SWRConfig } from "swr";
import { expect, it, vi } from "vitest";

import type { Agent, Chat } from "@/lib/api";
import type { AgentThreads, Message, Thread, ThreadActivity } from "@/lib/api-types";
import { routeTree } from "@/routeTree.gen";

// Ported from agentmesh console tests/console-layout.test.tsx onto AppShell and
// Hatchery routes: threads are chats (/chats/<id>), a new thread is the draft at
// /, Workspace is /agents/<id>, and the API view is /agents/<id>/api.

const mira: Agent = {
  id: "mira",
  name: "Mira",
  repos: [],
  resources: [],
  color: "blue-700",
  created_at: "2026-09-25T00:00:00Z",
};
function chat(id: string, topic: string): Chat {
  return {
    id,
    user_id: "user_test",
    agent_id: "mira",
    title: topic,
    topic,
    trigger: "ui",
    status: "done",
    sandbox_id: null,
    artifact: null,
    attention_reason: null,
    archived_at: null,
    created_at: "2026-09-25T00:00:00Z",
  };
}
const sleepingActivity: ThreadActivity = {
  status: "idle",
  phase: "idle",
  sandbox_active: false,
  mailbox_depth: 0,
  pending_prompts: 0,
  queued_tools: 0,
  running_tool: null,
  consolidating: [],
  awaiting_admission: false,
  budget_held: false,
  quiet_until: 0,
  schedules: [],
  updated_at: 1_789_000_000,
};
function thread(id: string, chatId: string, values: Partial<Thread> = {}): Thread {
  return {
    thread_id: id,
    chat_id: chatId,
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
    summary: "",
    result: "",
    error: "",
    input_tokens: 140,
    output_tokens: 30,
    proposals: [],
    ...values,
  };
}
const closed = thread("closed-thread", "chat_closed", { status: "done", live: false });
const planSaved: Message[] = [
  { role: "assistant", id: "a1", parts: [{ kind: "text", id: "a1:0", text: "Plan saved." }] },
];

type Backend = {
  chats: Chat[];
  roster: AgentThreads;
  transcripts?: Record<string, Message[]>;
  route?: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined;
};

function serve(backend: Backend) {
  const fetch = vi.fn(async (input: string, init?: RequestInit) => {
    const path = String(input);
    const custom = backend.route?.(path, init);
    if (custom) return custom;
    if (path === "/api/auth/me")
      return Response.json({ user: { id: "user_test", name: "Ada", email: null, username: null, picture: null } });
    if (path === "/api/agents") return Response.json([mira]);
    if (path === "/api/chats") return Response.json(backend.chats);
    if (path.startsWith("/api/connections/")) return Response.json({ connection: null });
    if (path === "/api/agents/warnings") return Response.json([]);
    if (path === "/api/agents/mira/threads") return Response.json(backend.roster);
    const transcript = path.match(/^\/api\/chats\/([^/]+)\/transcript$/);
    if (transcript) return Response.json(backend.transcripts?.[transcript[1]] ?? []);
    const detail = path.match(/^\/api\/chats\/([^/]+)\/thread$/);
    if (detail) {
      const found = backend.roster.threads.find((item) => item.chat_id === detail[1]);
      return found
        ? Response.json({ ...found, owner: "mira", turns: 1, compactions: 0, messages: [], revision: 1, branch: "", base_sha: "", checkpoint_sha: "" })
        : Response.json({ detail: "the chat has no thread yet" }, { status: 404 });
    }
    if (/^\/api\/chat\/[^/]+\/stream$/.test(path)) return new Response(null, { status: 204 });
    if (path.endsWith("/sandboxes")) return Response.json([]);
    if (path.includes("/filesystem")) return Response.json({ path: "", entries: [], truncated: false });
    if (path.startsWith("/api/repository"))
      return Response.json({
        branch: "main",
        sha: "a".repeat(40),
        base_sha: "a".repeat(40),
        main_sha: "a".repeat(40),
        merged: false,
        summary: "",
        remote: "file:///test.git",
        local_review: true,
        review: { workspace: "auto", serve: "review", wiki: "review" },
        checkout: null,
        files: [],
        changes: [],
      });
    if (path.startsWith("/api/agents/mira/jobs")) return Response.json([]);
    if (path === "/api/agents/mira/routes") return Response.json({ revision: "r", routes: [] });
    if (path === "/api/agents/mira/schedules")
      return Response.json({ revision: "r", reconciled_revision: "r", schedules: [] });
    if (path === "/api/agents/mira/secrets") return Response.json({ secrets: [] });
    throw new Error(`Unexpected request: ${path}`);
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function mount(path: string) {
  window.history.replaceState(null, "", path);
  const router = createRouter({ routeTree });
  render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false, dedupingInterval: 0 }}>
      <RouterProvider router={router} />
    </SWRConfig>,
  );
  return router;
}

const composer = () =>
  screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Message agent" });

it("shows a layout-matched skeleton while the console loads", async () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
  mount("/");
  expect(await screen.findByRole("status", { name: "Loading console" })).toBeTruthy();
});

it("optimistically wakes a sleeping thread as soon as its message is sent", async () => {
  serve({
    chats: [chat("chat_sleep", "Sleeping conversation")],
    roster: {
      agent_id: "mira",
      waiting: [],
      budget: null,
      threads: [thread("sleeping-thread", "chat_sleep", { activity: sleepingActivity })],
    },
    route: (path, init) =>
      path === "/api/chat" && init?.method === "POST" ? new Promise<Response>(() => {}) : undefined,
  });
  mount("/chats/chat_sleep");
  const card = () =>
    within(screen.getByRole("tree", { name: "Thread list" })).getByRole("button", {
      name: /Sleeping conversation/,
    });
  await waitFor(() => expect(card().dataset.threadState).toBe("sleeping"));
  expect(card().querySelector('[data-eye-state="sleeping"]')).toBeTruthy();

  const user = userEvent.setup();
  await user.type(composer(), "Wake up");
  await user.click(screen.getByRole("button", { name: "Submit" }));

  await waitFor(() => expect(card().dataset.threadState).toBe("awake"));
  expect(card().querySelector('[data-eye-state="awake"]')).toBeTruthy();
});

it("restores a deep-linked destination and writes navigable view paths", async () => {
  serve({
    chats: [chat("chat_closed", "Completed planning")],
    roster: { agent_id: "mira", waiting: [], budget: null, threads: [closed] },
    transcripts: { chat_closed: planSaved },
  });
  mount("/chats/chat_closed");
  const user = userEvent.setup();
  await screen.findByText("Plan saved.");

  await user.click(screen.getByRole("button", { name: "Workspace" }));
  await waitFor(() => expect(window.location.pathname).toBe("/agents/mira"));
  await user.click(screen.getByRole("button", { name: "API" }));
  await waitFor(() => expect(window.location.pathname).toBe("/agents/mira/api"));
  await screen.findByRole("heading", { name: "Mira / API" });
  await user.click(screen.getByRole("button", { name: "Repository" }));
  await waitFor(() => expect(window.location.pathname).toBe("/repository"));

  act(() => {
    window.history.pushState(null, "", "/chats/chat_closed");
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  // The conversation stayed mounted, hidden, under the other views.
  await waitFor(() =>
    expect(screen.getByText("Plan saved.").closest('[style*="display: none"]')).toBeNull(),
  );
});

it("opens a focused new conversation from a closed thread and preserves the draft on repeated New thread clicks", async () => {
  const created: Array<Record<string, unknown>> = [];
  const sent: Array<{ chat_id: string; messages: Array<{ id: string }> }> = [];
  let accept: ((response: Response) => void) | undefined;
  const backend: Backend = {
    chats: [chat("chat_closed", "Completed planning")],
    roster: { agent_id: "mira", waiting: [], budget: null, threads: [closed] },
    transcripts: { chat_closed: planSaved },
    route: (path, init) => {
      if (path === "/api/chats" && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        created.push(body);
        return new Promise<Response>((resolve) => {
          accept = resolve;
        });
      }
      if (path === "/api/chat" && init?.method === "POST") {
        sent.push(JSON.parse(String(init.body)));
        return new Promise<Response>(() => {});
      }
    },
  };
  serve(backend);
  mount("/chats/chat_closed");
  const user = userEvent.setup();
  await screen.findByText("Thread finished");

  await user.click(screen.getByRole("button", { name: "New thread", exact: true }));
  await waitFor(() => expect(window.location.pathname).toBe("/"));
  const input = await screen.findByRole<HTMLTextAreaElement>("textbox", { name: "Message agent" });
  await waitFor(() => expect(document.activeElement).toBe(input));
  expect(screen.queryByText("Plan saved.")).toBeNull();
  expect(input.disabled).toBe(false);
  expect(created).toHaveLength(0);
  await user.type(input, "Plan the launch");

  await user.click(screen.getByRole("button", { name: "New thread", exact: true }));
  expect(input.value).toBe("Plan the launch");
  await waitFor(() => expect(document.activeElement).toBe(input));
  await user.click(screen.getByRole("button", { name: "Workspace", exact: true }));
  await waitFor(() => expect(window.location.pathname).toBe("/agents/mira"));
  await user.click(screen.getByRole("button", { name: "New thread", exact: true }));
  await waitFor(() => expect(window.location.pathname).toBe("/"));
  expect(composer()).toBe(input);
  expect(input.value).toBe("Plan the launch");
  await waitFor(() => expect(document.activeElement).toBe(input));

  await user.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(created).toHaveLength(1));
  expect(created[0]).toMatchObject({ agent_id: "mira" });
  const optimistic = within(screen.getByRole("region", { name: "Messages" })).getByText(
    "Plan the launch",
  );
  const article = optimistic.closest("article");
  expect(screen.getByRole("status", { name: "Message queued" })).toBeTruthy();
  expect(screen.getByRole("status", { name: "Assistant is thinking" })).toBeTruthy();

  const draft = { ...chat(String(created[0].id), "Plan the launch"), title: "new chat" };
  backend.chats = [draft, ...backend.chats];
  accept?.(Response.json(draft));
  await waitFor(() => expect(window.location.pathname).toBe(`/chats/${draft.id}`));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0].chat_id).toBe(draft.id);
  await waitFor(() =>
    expect(screen.queryByRole("status", { name: "Message queued" })).toBeNull(),
  );
  expect(screen.getAllByText("Plan the launch").filter((node) => node.closest("article"))).toHaveLength(1);
  expect(within(screen.getByRole("region", { name: "Messages" })).getByText("Plan the launch")).toBe(optimistic);
  expect(optimistic.closest("article")).toBe(article);
});

it("keeps the first message and follow-up draft mounted while the chat is created", async () => {
  let accept!: (response: Response) => void;
  let createdId = "";
  const backend: Backend = {
    chats: [],
    roster: { agent_id: "mira", waiting: [], budget: null, threads: [] },
    route: (path, init) => {
      if (path === "/api/chats" && init?.method === "POST") {
        createdId = JSON.parse(String(init.body)).id;
        return new Promise<Response>((resolve) => {
          accept = resolve;
        });
      }
      if (path === "/api/chat" && init?.method === "POST") return new Promise<Response>(() => {});
    },
  };
  serve(backend);
  mount("/");
  const input = await screen.findByRole<HTMLTextAreaElement>("textbox", { name: "Message agent" });
  fireEvent.change(input, { target: { value: "Plan the launch" } });
  fireEvent.submit(input.closest("form")!);
  const message = await within(screen.getByRole("region", { name: "Messages" })).findByText(
    "Plan the launch",
  );
  fireEvent.change(input, { target: { value: "Include the release checklist" } });
  expect(
    screen.getByRole<HTMLButtonElement>("button", { name: "Sending message" }).disabled,
  ).toBe(true);

  const created = chat(createdId, "Plan the launch");
  backend.chats = [created];
  await act(async () => {
    accept(Response.json(created));
  });
  await waitFor(() => expect(window.location.pathname).toBe(`/chats/${createdId}`));
  expect(composer()).toBe(input);
  expect(input.value).toBe("Include the release checklist");
  expect(input.disabled).toBe(false);
  expect(
    within(screen.getByRole("region", { name: "Messages" })).getByText("Plan the launch"),
  ).toBe(message);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.queryByRole("tab", { name: "Changes" })).toBeNull();
  expect(screen.getByRole("tab", { name: "Workspace" }).getAttribute("aria-selected")).toBe("true");

  fireEvent.click(screen.getByRole("button", { name: "New thread", exact: true }));
  await waitFor(() => expect(window.location.pathname).toBe("/"));
  await waitFor(() => expect(composer().value).toBe(""));
});
