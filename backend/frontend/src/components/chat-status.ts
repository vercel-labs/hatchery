export function submissionLabel(spaceId: string | null): string {
  return spaceId === null ? "Assigning a space…" : "Thinking…";
}
