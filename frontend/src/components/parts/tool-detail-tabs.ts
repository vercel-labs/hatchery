export type ToolDetailTab = "input" | "output" | "error";

export function toolDetailTabs({
  hasInput,
  result,
}: {
  hasInput: boolean;
  result: Exclude<ToolDetailTab, "input"> | null;
}): ToolDetailTab[] {
  return [...(hasInput ? (["input"] as const) : []), ...(result ? [result] : [])];
}
