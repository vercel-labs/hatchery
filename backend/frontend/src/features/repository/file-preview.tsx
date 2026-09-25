import { diffLines } from "./diff-lines";
import type { RepositoryFile } from "@/lib/api-types";

export function FilePreview({
  data,
  mode,
}: {
  data: RepositoryFile;
  mode: "file" | "diff";
}) {
  if (mode === "diff") {
    if (data.diff === null)
      return (
        <p className="p-6 text-sm text-muted-foreground">
          {data.after?.notice ??
            data.before?.notice ??
            "No text diff available."}
        </p>
      );
    if (!data.diff)
      return (
        <p className="p-6 text-sm text-muted-foreground">
          No text changes.{" "}
          {data.before?.mode !== data.after?.mode
            ? `Mode ${data.before?.mode ?? "absent"} → ${data.after?.mode ?? "absent"}.`
            : "This file matches the comparison base."}
        </p>
      );
    return (
      <div className="min-w-max">
        <pre
          className="min-w-max py-2 font-mono text-xs leading-6"
          aria-label="File diff"
        >
          {diffLines(data.diff).map((line, index) => (
            <div
              key={index}
              className={`flex min-w-max border-l-2 ${line.kind === "addition" ? "border-green-500 bg-green-600/10 text-green-900 dark:text-green-200" : line.kind === "deletion" ? "border-red-500 bg-red-600/10 text-red-900 dark:text-red-200" : line.kind === "hunk" ? "border-transparent bg-muted/60 text-muted-foreground" : "border-transparent"}`}
            >
              <span
                aria-hidden
                className="w-10 shrink-0 pr-2 text-right text-muted-foreground/65 select-none"
              >
                {line.before}
              </span>
              <span
                aria-hidden
                className="w-10 shrink-0 pr-3 text-right text-muted-foreground/65 select-none"
              >
                {line.after}
              </span>
              <span className="pr-5">{line.text}</span>
            </div>
          ))}
        </pre>
        {data.diff_truncated ? (
          <p className="p-4 text-xs text-muted-foreground">
            Diff truncated at 256 KiB. Use Git for the full diff.
          </p>
        ) : null}
      </div>
    );
  }
  const version = data.after;
  if (!version)
    return (
      <p className="p-6 text-sm text-muted-foreground">
        This file was deleted. Select Diff to review its previous contents.
      </p>
    );
  if (version.text === null)
    return (
      <p className="p-6 text-sm text-muted-foreground">{version.notice}</p>
    );
  return (
    <pre
      className="min-w-max p-5 font-mono text-xs leading-6"
      aria-label="File contents"
    >
      {version.text || "(empty file)"}
    </pre>
  );
}
