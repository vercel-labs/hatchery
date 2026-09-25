import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, expect, it, vi } from "vitest";
// Ported from agentmesh console tests/workspace-panel.test.tsx: the thread is
// addressed by its chat, under /api/chats/{id}/thread/filesystem.
import WorkspacePanel from "@/features/threads/workspace-panel";

afterEach(() => vi.unstubAllGlobals());

// agentmesh: [[gateway#Console regression tests]]
it("shows a startup loader while a new thread sandbox is being created", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      Response.json(
        { detail: "Thread sandbox is unavailable" },
        { status: 404 },
      ),
    ),
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <WorkspacePanel
        chatId="chat_new"
        visible
        waitForCreation
      />
    </SWRConfig>,
  );

  const status = await screen.findByRole("status");
  expect(status.textContent).toContain("Starting sandbox");
  expect(
    status
      .querySelector("svg")
      ?.classList.contains("motion-safe:animate-spin"),
  ).toBe(true);
  expect(screen.queryByRole("alert")).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("browses untracked repositories in the selected thread sandbox", async () => {
  const directories: Record<
    string,
    Array<{ name: string; path: string; kind: string }>
  > = {
    "": [
      { name: ".hatchery", path: ".hatchery", kind: "directory" },
      { name: "repos", path: "repos", kind: "directory" },
      { name: "self", path: "self", kind: "directory" },
    ],
    repos: [{ name: "acme", path: "repos/acme", kind: "directory" }],
    "repos/acme": [{ name: "app", path: "repos/acme/app", kind: "directory" }],
    "repos/acme/app": [
      {
        name: "untracked.log",
        path: "repos/acme/app/untracked.log",
        kind: "file",
      },
    ],
  };
  const requested: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      requested.push(input);
      const url = new URL(input, "http://mesh.test");
      const path = url.searchParams.get("path") ?? "";
      if (url.pathname.endsWith("/file"))
        return Response.json({
          path,
          text: "uncommitted checkout output\n",
          notice: null,
          truncated: false,
          bytes_read: 28,
        });
      return Response.json({
        path,
        entries: directories[path] ?? [],
        truncated: false,
      });
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <WorkspacePanel chatId="chat_a" visible />
    </SWRConfig>,
  );
  const user = userEvent.setup();

  expect(
    screen
      .getByRole("separator", { name: "Resize workspace tree" })
      .getAttribute("aria-orientation"),
  ).toBe("horizontal");

  await user.click(await screen.findByRole("button", { name: /^repos/ }));
  await user.click(await screen.findByRole("button", { name: /^acme/ }));
  await user.click(await screen.findByRole("button", { name: /^app/ }));
  await user.click(
    await screen.findByRole("button", { name: "untracked.log" }),
  );
  expect(await screen.findByText("uncommitted checkout output")).toBeTruthy();
  expect(
    requested.some((request) => request.includes("/api/chats/chat_a/thread/filesystem/file")),
  ).toBe(true);
});

// agentmesh: [[gateway#Console regression tests]]
it("stops polling while the workspace tab is hidden", async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn(async () =>
    Response.json({ path: "", entries: [], truncated: false }),
  );
  vi.stubGlobal("fetch", fetcher);
  const view = render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <WorkspacePanel chatId="chat_a" visible={false} />
    </SWRConfig>,
  );
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000);
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
  view.unmount();
  vi.useRealTimers();
});
