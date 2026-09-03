export function terminalVisibilityAfterSandboxLoad(
  current: Record<string, boolean>,
  chatId: string,
  hasSandboxes: boolean,
) {
  return Object.hasOwn(current, chatId)
    ? current
    : { ...current, [chatId]: hasSandboxes };
}
