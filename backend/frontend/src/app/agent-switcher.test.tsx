import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { AgentSwitcher } from "@/app/agent-switcher";
import type { Agent } from "@/lib/api";

function agent(id: string, name: string): Agent {
  return {
    id,
    name,
    repos: [],
    resources: [],
    color: "blue-700",
    created_at: "2026-09-25T00:00:00Z",
  };
}

const agents = [agent("mira", "Mira"), agent("sol", "Sol")];

// Ported from agentmesh console tests/agent-switcher.test.tsx. Agents keep
// Hatchery's display names; the thread count comes from the agent's chats.
it("opens a clear agent menu and selects another agent", async () => {
  const onSelect = vi.fn();
  const user = userEvent.setup();
  render(
    <AgentSwitcher
      agents={agents}
      agent={agents[0]}
      threadCounts={{ sol: 1 }}
      onSelect={onSelect}
    />,
  );

  const trigger = screen.getByRole("button", {
    name: "Current agent: Mira. Switch agent",
  });
  expect(trigger.textContent).toContain("0 threads");
  await user.click(trigger);
  const sol = await screen.findByRole("menuitem", { name: /^sol/i });
  expect(sol.textContent).toContain("1 thread");
  expect(screen.getByLabelText("Selected")).toBeTruthy();
  await user.click(sol);

  expect(onSelect).toHaveBeenCalledWith("sol");
});

it("deletes an agent from its row without selecting it, but never the last agent", async () => {
  const onSelect = vi.fn();
  const onDelete = vi.fn();
  const user = userEvent.setup();
  const { rerender } = render(
    <AgentSwitcher
      agents={agents}
      agent={agents[0]}
      threadCounts={{}}
      onSelect={onSelect}
      onDelete={onDelete}
    />,
  );

  await user.click(screen.getByRole("button", { name: /switch agent/i }));
  await user.click(await screen.findByRole("menuitem", { name: "Delete Sol" }));
  expect(onDelete).toHaveBeenCalledWith(agents[1]);
  expect(onSelect).not.toHaveBeenCalled();

  rerender(
    <AgentSwitcher
      agents={[agents[0]]}
      agent={agents[0]}
      threadCounts={{}}
      onSelect={onSelect}
      onDelete={onDelete}
    />,
  );
  await user.click(screen.getByRole("button", { name: /switch agent/i }));
  expect(
    (await screen.findByRole("menuitem", { name: "Delete Mira" })).getAttribute(
      "aria-disabled",
    ),
  ).toBe("true");
});
