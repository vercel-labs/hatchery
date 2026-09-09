export const AUTO_SPACE_VALUE = "__auto__";

export function newChatRequest(): Record<string, never> {
  return {};
}

export function isBrandNewChat(messageCount: number): boolean {
  return messageCount === 0;
}
