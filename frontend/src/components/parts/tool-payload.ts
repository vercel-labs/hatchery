export function formatToolPayload(payload: unknown): string {
  let value = payload;

  if (typeof payload === "string") {
    try {
      const parsed = JSON.parse(payload) as unknown;
      if (typeof parsed !== "object" || parsed === null) return payload;
      value = parsed;
    } catch {
      return payload;
    }
  }

  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}
