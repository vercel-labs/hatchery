import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, expect, it, vi } from "vitest";
// Ported from agentmesh console tests/agent-api-view.test.tsx against the serve
// endpoints; the per-agent GitHub connection is not part of Hatchery.
import { AgentApiView } from "@/features/agents/agent-api-view";

afterEach(() => vi.unstubAllGlobals());

// agentmesh: [[gateway#Console regression tests]]
it("shows available routes, route failures, and secret inventory metadata", async () => {
  let paused = false;
  const scheduleActions: string[] = [];
  let finishPause: (() => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/agents/mira/routes")
        return Response.json({
          revision: "abcdef1234567890",
          routes: [
            {
              path: "/weather",
              methods: ["GET"],
              url: "https://mira.mesh.test/weather",
              description: "Current weather summary",
              available: true,
              error: null,
            },
            {
              path: "/deploy",
              methods: ["POST"],
              url: "https://mira.mesh.test/deploy",
              description: "",
              available: false,
              error: "Handler import failed",
            },
          ],
        });
      if (path === "/api/agents/mira/schedules" && init?.method === "GET")
        return Response.json({
          revision: "abcdef1234567890",
          reconciled_revision: "abcdef1234567890",
          schedules: [
            {
              name: "weather-refresh",
              description: "Refresh cached forecasts",
              kind: "cron",
              value: "0 8 * * *",
              timezone: "America/New_York",
              enabled: true,
              paused,
              available: true,
              error: null,
              next_at: 1_800_000_000,
              running: false,
              last_run: null,
            },
          ],
        });
      if (path === "/api/agents/mira/schedules/weather-refresh/pause") {
        scheduleActions.push(path);
        return new Promise<Response>((resolve) => {
          finishPause = () => {
            paused = true;
            resolve(
              Response.json({
                name: "weather-refresh",
                paused,
                outcome: "sent",
              }),
            );
          };
        });
      }
      if (path === "/api/agents/mira/secrets")
        return Response.json({
          secrets: [
            {
              name: "WEATHER_TOKEN",
              stored: false,
              updated_at: null,
              note: "Needed for weather requests",
            },
            {
              name: "DEPLOY_TOKEN",
              stored: true,
              updated_at: 0,
              note: null,
            },
          ],
        });
      throw new Error(`Unexpected request: ${path}`);
    }),
  );

  render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      <AgentApiView agentId="mira" agentName="Mira" />
    </SWRConfig>,
  );

  expect(await screen.findByText("Current weather summary")).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "https://mira.mesh.test/weather" }),
  ).toBeTruthy();
  expect(screen.getByText("Handler import failed")).toBeTruthy();
  expect(screen.getByText("Unavailable")).toBeTruthy();
  expect(screen.getByText("Needed for weather requests")).toBeTruthy();
  expect(screen.getByText("Refresh cached forecasts")).toBeTruthy();
  expect(screen.getByText("America/New_York")).toBeTruthy();
  expect(screen.getByText(/Updated/)).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Set WEATHER_TOKEN" }),
  ).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Actions for DEPLOY_TOKEN" }),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Rotate DEPLOY_TOKEN" }),
  ).toBeNull();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Pause" }));
  expect(
    await screen.findByRole("button", { name: "Pausing..." }),
  ).toBeTruthy();
  expect(
    screen.getByLabelText<HTMLInputElement>("Value for WEATHER_TOKEN").disabled,
  ).toBe(false);
  finishPause?.();
  await screen.findByRole("button", { name: "Resume" });
  expect(scheduleActions).toEqual([
    "/api/agents/mira/schedules/weather-refresh/pause",
  ]);
});

// agentmesh: [[gateway#Console regression tests]]
it("sets a requested secret without retaining its value in the form", async () => {
  let stored = false;
  const posts: Array<{ path: string; body: Record<string, unknown> }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/agents/mira/routes")
        return Response.json({ revision: "abc", routes: [] });
      if (path === "/api/agents/mira/schedules")
        return Response.json({
          revision: "abc",
          reconciled_revision: "abc",
          schedules: [],
        });
      if (path === "/api/agents/mira/secrets" && init?.method === "GET")
        return Response.json({
          secrets: [
            {
              name: "WEATHER_TOKEN",
              stored,
              updated_at: stored ? 1_700_000_001 : null,
              note: "Needed for weather requests",
            },
          ],
        });
      if (path === "/api/agents/mira/secrets/WEATHER_TOKEN") {
        posts.push({
          path,
          body: JSON.parse(String(init?.body)) as Record<string, unknown>,
        });
        stored = true;
        return Response.json({ name: "WEATHER_TOKEN", outcome: "sent" });
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  const user = userEvent.setup();
  render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      <AgentApiView agentId="mira" agentName="Mira" />
    </SWRConfig>,
  );
  const input = await screen.findByLabelText<HTMLInputElement>(
    "Value for WEATHER_TOKEN",
  );

  await user.type(input, "weather-secret-value");
  await user.click(screen.getByRole("button", { name: "Set WEATHER_TOKEN" }));

  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Actions for WEATHER_TOKEN" }),
    ).toBeTruthy(),
  );
  expect(posts).toHaveLength(1);
  expect(posts[0].path).toBe("/api/agents/mira/secrets/WEATHER_TOKEN");
  expect(posts[0].body).toMatchObject({
    request_id: expect.any(String),
    value: "weather-secret-value",
  });
  expect(input.value).toBe("");
  expect(screen.queryByText("weather-secret-value")).toBeNull();
});

// agentmesh: [[gateway#Console regression tests]]
it("reveals locally, hides immediately, and requires inline delete confirmation", async () => {
  let stored = true;
  const posts: string[] = [];
  let finishReveal: (() => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/agents/mira/routes")
        return Response.json({ revision: "abc", routes: [] });
      if (path === "/api/agents/mira/schedules")
        return Response.json({
          revision: "abc",
          reconciled_revision: "abc",
          schedules: [],
        });
      if (path === "/api/agents/mira/secrets" && init?.method === "GET")
        return Response.json({
          secrets: [
            {
              name: "DEPLOY_TOKEN",
              stored,
              updated_at: stored ? 1_700_000_002 : null,
              note: "Needed for deployments",
            },
          ],
        });
      if (init?.method === "POST") posts.push(path);
      if (path.endsWith("/reveal"))
        return new Promise<Response>((resolve) => {
          finishReveal = () =>
            resolve(
              Response.json({
                name: "DEPLOY_TOKEN",
                value: "local-only-value",
              }),
            );
        });
      if (path.endsWith("/delete")) {
        stored = false;
        return Response.json({ name: "DEPLOY_TOKEN", outcome: "sent" });
      }
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
  const user = userEvent.setup();
  render(
    <SWRConfig value={{ provider: () => new Map(), shouldRetryOnError: false }}>
      <AgentApiView agentId="mira" agentName="Mira" />
    </SWRConfig>,
  );

  await user.click(
    await screen.findByRole("button", { name: "Actions for DEPLOY_TOKEN" }),
  );
  await user.click(await screen.findByRole("menuitem", { name: "Rotate" }));
  const rotateInput = screen.getByLabelText<HTMLInputElement>(
    "New value for DEPLOY_TOKEN",
  );
  await user.click(screen.getByRole("button", { name: "Reveal DEPLOY_TOKEN" }));
  expect(
    await screen.findByRole("button", { name: "Revealing DEPLOY_TOKEN" }),
  ).toBeTruthy();
  expect(rotateInput.disabled).toBe(false);
  finishReveal?.();
  expect(await screen.findByText("local-only-value")).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Hide DEPLOY_TOKEN" }));
  expect(screen.queryByText("local-only-value")).toBeNull();

  expect(
    screen.getByRole("button", { name: "Rotate DEPLOY_TOKEN" }),
  ).toBeTruthy();
  await user.click(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.queryByLabelText("New value for DEPLOY_TOKEN")).toBeNull();

  await user.click(
    screen.getByRole("button", { name: "Actions for DEPLOY_TOKEN" }),
  );
  await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
  const confirmation = screen.getByRole("group", {
    name: "Confirm delete DEPLOY_TOKEN",
  });
  expect(
    within(confirmation).getByText("Permanently delete DEPLOY_TOKEN?"),
  ).toBeTruthy();
  expect(posts).toEqual(["/api/agents/mira/secrets/DEPLOY_TOKEN/reveal"]);
  await user.click(
    within(confirmation).getByRole("button", { name: "Delete" }),
  );

  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Set DEPLOY_TOKEN" }),
    ).toBeTruthy(),
  );
  expect(posts).toEqual([
    "/api/agents/mira/secrets/DEPLOY_TOKEN/reveal",
    "/api/agents/mira/secrets/DEPLOY_TOKEN/delete",
  ]);
  expect(
    screen.queryByRole("group", { name: "Confirm delete DEPLOY_TOKEN" }),
  ).toBeNull();
});
