import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, expect, it, vi } from "vitest";
// Ported from agentmesh console tests/repository.test.tsx: threads are addressed
// by chat (`chat_id`), agents live under agents/<id>/.
import RepositoryPanel from "@/features/repository/repository-panel";
import type { Repository } from "@/lib/api-types";

const sha = "a".repeat(40);
const view: Repository = {
  branch: "consolidations/mira/wiki/test",
  sha,
  main_sha: "b".repeat(40),
  base_sha: "c".repeat(40),
  merged: false,
  summary: "Document the team procedure",
  remote: "file:///test.git",
  local_review: true,
  review: { workspace: "auto", serve: "review", wiki: "review" },
  checkout: null,
  files: [{ path: "wiki/procedure.md", mode: "100644", size: 20, oid: sha }],
  changes: [
    {
      path: "wiki/procedure.md",
      status: "added",
      before_mode: null,
      after_mode: "100644",
    },
  ],
};
afterEach(() => vi.unstubAllGlobals());

// agentmesh: [[gateway#Console regression tests]]
it("pins file previews and approval to the displayed proposal and retains its diff after merging", async () => {
  let merged = false;
  const previews: URLSearchParams[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const params = new URL(path, "http://mesh.test").searchParams;
      if (params.has("path")) {
        previews.push(params);
        return Response.json({
          path: params.get("path"),
          before: null,
          after: { text: "Team procedure\n", mode: "100644" },
          diff: "+Team procedure\n",
          diff_truncated: false,
        });
      }
      return Response.json({ ...view, merged });
    }),
  );
  const approve = vi.fn(async () => {
    merged = true;
  });
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <RepositoryPanel
        chatId="chat_one"
        proposal={view.branch}
        onApprove={approve}
      />
    </SWRConfig>,
  );
  await screen.findByLabelText("File diff");
  expect(previews[0].get("revision")).toBe(sha);
  expect(previews[0].get("proposal")).toBe(view.branch);
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Approve & merge" }));
  await screen.findByText("Merged into main");
  expect(approve).toHaveBeenCalledWith(sha);
  expect(screen.getByLabelText("File diff").textContent).toContain(
    "+Team procedure",
  );
  expect(screen.queryByRole("button", { name: "Approve & merge" })).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("shows both a deleted file and the new directory that replaces it", async () => {
  const paths = ["wiki/guide", "wiki/guide/note.md"];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const params = new URL(path, "http://mesh.test").searchParams;
      if (params.has("path"))
        return Response.json({
          path: params.get("path"),
          before: null,
          after: { text: "New note", mode: "100644" },
          diff: "+New note\n",
          diff_truncated: false,
        });
      return Response.json({
        ...view,
        files: [{ path: paths[1], mode: "100644", size: 8, oid: sha }],
        changes: [
          { path: paths[0], status: "deleted" },
          { path: paths[1], status: "added" },
        ],
      });
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <RepositoryPanel chatId="replacement" />
    </SWRConfig>,
  );
  await screen.findByRole("button", { name: "guide (previous file) deleted" });
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "note.md added" }));
  await waitFor(() =>
    expect(screen.getByLabelText("File diff").textContent).toContain(
      "New note",
    ),
  );
});

// agentmesh: [[gateway#Console regression tests]]
it("shows wiki proposals in Repository with their original thread and revision", async () => {
  const { RepositoryView } =
    await import("@/features/repository/repository-view");
  const previews: URLSearchParams[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const params = new URL(input, "http://mesh.test").searchParams;
      if (params.has("path")) {
        previews.push(params);
        return Response.json({
          path: "wiki/procedure.md",
          after: { text: "Published procedure", mode: "100644" },
          diff: "+Proposed procedure\n",
        });
      }
      return Response.json(view);
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <RepositoryView
        refresh={async () => {}}
        rosters={[
          {
            agent_id: "mira",
            local_review: true,
            waiting: [],
            budget: null,
            threads: [
              {
                thread_id: "wiki-thread",
                chat_id: "chat_wiki",
                parent_thread_id: "",
                depth: 0,
                upstream: "main",
                children: [],
                status: "done",
                live: false,
                archived: false,
                task_id: "",
                task_handle: "",
                task_status: "",
                deliverables: [],
                summary: "Save the procedure",
                result: "",
                error: "",
                input_tokens: 1,
                output_tokens: 1,
                proposals: [
                  {
                    section: "wiki",
                    branch: view.branch,
                    merged: false,
                    url: "",
                  },
                ],
              },
            ],
          },
        ]}
      />
    </SWRConfig>,
  );
  await screen.findByText("Published procedure");
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", {
      name: "Repository version: main. Switch version",
    }),
  );
  await user.click(
    await screen.findByRole("menuitem", { name: /mira \/ wiki/i }),
  );
  await screen.findByLabelText("File diff");
  expect(previews.at(-1)?.get("chat_id")).toBe("chat_wiki");
  expect(previews.at(-1)?.get("proposal")).toBe(view.branch);
  expect(previews.at(-1)?.get("revision")).toBe(sha);
  expect(screen.getByRole("button", { name: "Approve & merge" })).toBeTruthy();
});

// agentmesh: [[gateway#Console regression tests]]
it("shows every changed file together and keeps each file view independent", async () => {
  const previews: URLSearchParams[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const params = new URL(input, "http://mesh.test").searchParams;
      if (params.has("path")) {
        previews.push(params);
        return Response.json(
          params.get("path") === "wiki/procedure.md"
            ? {
                path: "wiki/procedure.md",
                after: { text: "Complete procedure", mode: "100644" },
                diff: "+Updated procedure\n",
                diff_truncated: false,
              }
            : {
                path: "agents/mira/notes.md",
                after: { text: "Complete notes", mode: "100644" },
                diff: "-Old notes\n+New notes\n",
                diff_truncated: false,
              },
        );
      }
      return Response.json({
        ...view,
        changes: [
          { path: "wiki/procedure.md", status: "modified" },
          { path: "agents/mira/notes.md", status: "modified" },
        ],
      });
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map() }}>
      <RepositoryPanel chatId="chat_one" compact />
    </SWRConfig>,
  );
  await screen.findByText("+Updated procedure");
  await screen.findByText("+New notes");
  expect(screen.getAllByLabelText("File diff")).toHaveLength(2);
  expect(screen.queryByRole("combobox", { name: "Changed file" })).toBeNull();
  expect(previews.map((params) => params.get("revision"))).toEqual([sha, sha]);
  await userEvent
    .setup()
    .click(
      screen.getByRole("button", { name: "Show full file: wiki/procedure.md" }),
    );
  await screen.findByText("Complete procedure");
  expect(screen.getByText("+New notes")).toBeTruthy();
  expect(screen.getByText("-Old notes")).toBeTruthy();
});

// agentmesh: [[gateway#Console regression tests]]
it("switches thread reviews between the full and latest-main diffs", async () => {
  const requests: URLSearchParams[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const params = new URL(input, "http://mesh.test").searchParams;
      requests.push(params);
      if (params.has("path"))
        return Response.json({
          path: params.get("path"),
          before: null,
          after: { text: "Current contents", mode: "100644" },
          diff:
            params.get("comparison") === "latest"
              ? "+Latest main diff\n"
              : "+Full thread diff\n",
          diff_truncated: false,
        });
      return Response.json(view);
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <RepositoryPanel chatId="chat_one" compact />
    </SWRConfig>,
  );
  await screen.findByText("+Full thread diff");
  expect(
    screen.getByRole("button", { name: "Full diff" }).getAttribute("aria-pressed"),
  ).toBe("true");

  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Latest diff" }));
  await screen.findByText("+Latest main diff");
  expect(
    screen
      .getByRole("button", { name: "Latest diff" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  expect(
    requests.some(
      (params) =>
        params.has("path") && params.get("comparison") === "latest",
    ),
  ).toBe(true);
});

// agentmesh: [[gateway#Console regression tests]]
it("retries a failed diff without hiding the other files", async () => {
  let failing = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const params = new URL(input, "http://mesh.test").searchParams;
      if (params.get("path") === "wiki/procedure.md" && failing)
        return Response.json({ detail: "File unavailable" }, { status: 503 });
      if (params.has("path"))
        return Response.json({
          path: params.get("path"),
          diff: `+${params.get("path")}`,
          after: { text: "Saved text", mode: "100644" },
        });
      return Response.json({
        ...view,
        changes: [
          { path: "wiki/procedure.md", status: "added" },
          { path: "wiki/working.md", status: "added" },
        ],
      });
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      <RepositoryPanel chatId="chat_one" compact />
    </SWRConfig>,
  );
  await screen.findByText("+wiki/working.md");
  await screen.findByRole("alert");
  failing = false;
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Retry wiki/procedure.md" }));
  await screen.findByText("+wiki/procedure.md");
  expect(screen.getByText("+wiki/working.md")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("refreshes the stack to one new revision while preserving file disclosure and view state", async () => {
  let revision = "a".repeat(40);
  let holdUpdatedPreview = false;
  let releaseUpdatedPreview: (() => void) | undefined;
  const previews: URLSearchParams[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string) => {
      const params = new URL(input, "http://mesh.test").searchParams;
      if (params.has("path")) {
        previews.push(params);
        if (
          holdUpdatedPreview &&
          params.get("path") === "wiki/procedure.md" &&
          params.get("revision")?.startsWith("b")
        )
          await new Promise<void>((resolve) => {
            releaseUpdatedPreview = resolve;
          });
        return Response.json({
          path: params.get("path"),
          diff: `+${params.get("path")}`,
          after: {
            text: revision.startsWith("a")
              ? "Earlier contents"
              : "Updated contents",
            mode: "100644",
          },
        });
      }
      return Response.json({
        ...view,
        sha: revision,
        changes: [
          { path: "wiki/procedure.md", status: "added" },
          { path: "wiki/second.md", status: "added" },
        ],
      });
    }),
  );
  render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <RepositoryPanel chatId="chat_one" compact />
    </SWRConfig>,
  );
  await screen.findByText("+wiki/second.md");
  const user = userEvent.setup();
  await user.click(
    screen.getByRole("button", { name: "wiki/second.md", exact: true }),
  );
  await user.click(
    screen.getByRole("button", { name: "Show full file: wiki/procedure.md" }),
  );
  await screen.findByText("Earlier contents");
  previews.length = 0;
  revision = "b".repeat(40);
  holdUpdatedPreview = true;
  await user.click(screen.getByRole("button", { name: "Refresh files" }));
  await waitFor(() => expect(releaseUpdatedPreview).toBeTypeOf("function"));
  expect(screen.getByText("Earlier contents")).toBeTruthy();
  expect(screen.queryByText("Loading diff…")).toBeNull();
  releaseUpdatedPreview?.();
  await screen.findByText("Updated contents");
  expect(
    screen
      .getByRole("button", { name: "wiki/second.md", exact: true })
      .getAttribute("aria-expanded"),
  ).toBe("false");
  await waitFor(() => expect(previews).toHaveLength(2));
  expect(previews.map((params) => params.get("revision"))).toEqual([
    "b".repeat(40),
    "b".repeat(40),
  ]);
});
