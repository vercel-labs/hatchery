import { Fragment } from "react";
import type { Repository } from "@/lib/api-types";

type ChangeStatus = Repository["changes"][number]["status"];
const changeColors: Record<ChangeStatus, string> = {
  added: "text-green-700 dark:text-green-300",
  deleted: "text-red-700 dark:text-red-300",
  modified: "text-amber-700 dark:text-amber-300",
};

type FileNode = {
  name: string;
  path: string;
  children: Map<string, FileNode>;
  file: boolean;
  status?: ChangeStatus;
};

export function fileTree(paths: string[], changes: Map<string, ChangeStatus>) {
  const root: FileNode = {
    name: "",
    path: "",
    children: new Map(),
    file: false,
  };
  for (const path of paths) {
    let node = root;
    for (const name of path.split("/")) {
      let child = node.children.get(name);
      if (!child) {
        child = {
          name,
          path: node.path ? `${node.path}/${name}` : name,
          children: new Map(),
          file: false,
        };
        node.children.set(name, child);
      }
      node = child;
    }
    node.file = true;
    node.status = changes.get(path);
  }
  return root;
}

export function FileTree({
  node,
  selected,
  onSelect,
}: {
  node: FileNode;
  selected: string;
  onSelect: (path: string) => void;
}) {
  return (
    <ul className="space-y-0.5">
      {Array.from(node.children.values())
        .sort(
          (a, b) =>
            Number(a.file) - Number(b.file) || a.name.localeCompare(b.name),
        )
        .map((child) => (
          <Fragment key={child.path}>
            {child.file ? (
              <li>
                <button
                  type="button"
                  className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted ${selected === child.path ? "bg-muted font-medium" : ""}`}
                  aria-current={selected === child.path ? "true" : undefined}
                  aria-label={`${child.name}${child.children.size ? " (previous file)" : ""}${child.status ? ` ${child.status}` : ""}`}
                  title={child.path}
                  onClick={() => onSelect(child.path)}
                >
                  <span aria-hidden className="text-muted-foreground">
                    ▤
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    {child.name}
                    {child.children.size ? " (previous file)" : ""}
                  </span>
                  {child.status ? (
                    <span
                      className={`font-mono ${changeColors[child.status]}`}
                      aria-label={child.status}
                    >
                      {child.status.slice(0, 1).toUpperCase()}
                    </span>
                  ) : null}
                </button>
              </li>
            ) : null}
            {child.children.size ? (
              <li>
                <details open>
                  <summary className="cursor-pointer px-2 py-1.5 text-xs text-muted-foreground">
                    {child.name}/
                  </summary>
                  <div className="ml-3 border-l pl-1">
                    <FileTree
                      node={child}
                      selected={selected}
                      onSelect={onSelect}
                    />
                  </div>
                </details>
              </li>
            ) : null}
          </Fragment>
        ))}
    </ul>
  );
}
