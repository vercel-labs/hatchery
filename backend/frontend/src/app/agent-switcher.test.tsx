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
  const sol = await screen.findByRole("menuitem", { name: /sol/i });
  expect(sol.textContent).toContain("1 thread");
  expect(screen.getByLabelText("Selected")).toBeTruthy();
  await user.click(sol);

  expect(onSelect).toHaveBeenCalledWith("sol");
});
