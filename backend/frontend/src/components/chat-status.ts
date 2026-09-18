export function submissionLabel(agentId: string | null): string {
  return agentId === null ? "Assigning an agent…" : "Thinking…";
}
