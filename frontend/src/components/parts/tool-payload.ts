export type ToolPayloadValue =
  | { type: "scalar"; value: string }
  | { type: "array"; values: ToolPayloadValue[] }
  | { type: "object"; entries: [string, ToolPayloadValue][] };

function payloadValue(value: unknown): ToolPayloadValue | null {
  if (value == null) return null;

  if (Array.isArray(value)) {
    const values = value
      .map(payloadValue)
      .filter((item): item is ToolPayloadValue => item !== null);
    return values.length === 0 ? null : { type: "array", values };
  }

  if (typeof value === "object") {
    const object = value as Record<string, unknown>;
    const branch = typeof object.branch === "string" ? object.branch.trim() : "";
    const gitSha =
      typeof object.git_sha === "string" ? object.git_sha.trim() : "";
    const entries = Object.entries(object).flatMap<[string, ToolPayloadValue]>(
      ([key, item]) => {
        const annotated =
          key === "repos" && Array.isArray(item) && !branch && !gitSha
            ? item.map((repo, index) =>
                index === 0 && typeof repo === "string"
                  ? `${repo} (default branch)`
                  : repo,
              )
            : item;
        const child = payloadValue(annotated);
        return child === null ? [] : [[key, child]];
      },
    );
    return entries.length === 0 ? null : { type: "object", entries };
  }

  return { type: "scalar", value: String(value) };
}

export function toolPayload(payload: unknown): ToolPayloadValue | null {
  if (typeof payload !== "string") return payloadValue(payload);

  try {
    const parsed = JSON.parse(payload) as unknown;
    if (typeof parsed === "object" && parsed !== null) {
      return payloadValue(parsed);
    }
  } catch {
    // Plain text is already a useful payload.
  }

  return payloadValue(payload);
}
