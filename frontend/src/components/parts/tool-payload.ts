type JsonObject = Record<string, unknown>;

function visible(value: unknown): boolean {
  if (value == null) return false;
  if (Array.isArray(value)) return value.some(visible);
  if (typeof value === "object") return Object.values(value).some(visible);
  return true;
}

function textLines(value: string): string[] {
  const lines = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n");

  if (lines.length === 1) return lines;
  while (lines[0]?.trim() === "") lines.shift();
  while (lines.at(-1)?.trim() === "") lines.pop();

  const indentation = lines
    .filter((line) => line.trim())
    .map((line) => line.match(/^\s*/)?.[0].length ?? 0);
  const commonIndent = Math.min(...indentation);
  return commonIndent > 0 ? lines.map((line) => line.slice(commonIndent)) : lines;
}

function scalarLines(value: unknown): string[] {
  return textLines(typeof value === "string" ? value : String(value));
}

function repoValues(repos: unknown[], object: JsonObject): unknown[] {
  const branch = typeof object.branch === "string" ? object.branch.trim() : "";
  const gitSha = typeof object.git_sha === "string" ? object.git_sha.trim() : "";

  return repos.map((repo, index) =>
    index === 0 && typeof repo === "string" && !branch && !gitSha
      ? `${repo} (default branch)`
      : repo,
  );
}

function renderArray(values: unknown[], indent: number): string[] {
  const prefix = " ".repeat(indent);
  return values.filter(visible).flatMap((value) => {
    if (Array.isArray(value)) {
      return [`${prefix}-`, ...renderArray(value, indent + 2)];
    }
    if (typeof value === "object" && value !== null) {
      const lines = renderObject(value as JsonObject, indent + 2);
      if (lines.length === 0) return [];
      return [
        `${prefix}- ${lines[0].slice(indent + 2)}`,
        ...lines.slice(1),
      ];
    }

    const lines = scalarLines(value);
    return [
      `${prefix}- ${lines[0] ?? ""}`,
      ...lines.slice(1).map((line) =>
        line ? `${" ".repeat(indent + 2)}${line}` : "",
      ),
    ];
  });
}

function renderObject(object: JsonObject, indent: number): string[] {
  const prefix = " ".repeat(indent);
  return Object.entries(object).flatMap(([key, rawValue]) => {
    if (!visible(rawValue)) return [];
    const value = key === "repos" && Array.isArray(rawValue)
      ? repoValues(rawValue, object)
      : rawValue;

    if (Array.isArray(value)) {
      return [`${prefix}${key}:`, ...renderArray(value, indent + 2)];
    }
    if (typeof value === "object" && value !== null) {
      return [`${prefix}${key}:`, ...renderObject(value as JsonObject, indent + 2)];
    }

    const lines = scalarLines(value);
    if (lines.length <= 1) return [`${prefix}${key}: ${lines[0] ?? ""}`];
    return [
      `${prefix}${key}:`,
      ...lines.map((line) =>
        line ? `${" ".repeat(indent + 2)}${line}` : "",
      ),
    ];
  });
}

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

  if (Array.isArray(value)) return renderArray(value, 0).join("\n");
  if (typeof value === "object" && value !== null) {
    return renderObject(value as JsonObject, 0).join("\n");
  }
  return String(value);
}
