export type StorageFile = {
  path: string;
  size?: number;
  modifiedAt?: string;
};

export type StorageFileNode = StorageFile & {
  kind: "file";
  name: string;
};

export type StorageDirectoryNode = {
  kind: "directory";
  name: string;
  path: string;
  children: StorageTreeNode[];
};

export type StorageTreeNode = StorageFileNode | StorageDirectoryNode;

export const defaultStorageEntries: readonly StorageFile[] = [
  { path: "AGENTS.md" },
  { path: "skills/.gitkeep" },
  { path: "memories/.gitkeep" },
  { path: "scripts/.gitkeep" },
  { path: "schedules/.gitkeep" },
];

function normalizedParts(path: string) {
  const parts = path.replaceAll("\\", "/").split("/").filter(Boolean);
  if (parts.some((part) => part === "." || part === "..")) return [];
  return parts;
}

export function buildStorageFileTree(
  files: readonly StorageFile[],
  includeDefaults = true,
): StorageTreeNode[] {
  const root: StorageDirectoryNode = {
    kind: "directory",
    name: "",
    path: "",
    children: [],
  };
  const entries = includeDefaults ? [...defaultStorageEntries, ...files] : [...files];
  const directories = new Map<string, StorageDirectoryNode>([["", root]]);
  const seenFiles = new Set<string>();

  for (const file of entries) {
    const parts = normalizedParts(file.path);
    if (parts.length === 0) continue;

    let directory = root;
    for (const [index, part] of parts.slice(0, -1).entries()) {
      const path = parts.slice(0, index + 1).join("/");
      let child = directories.get(path);
      if (!child) {
        child = { kind: "directory", name: part, path, children: [] };
        directories.set(path, child);
        directory.children.push(child);
      }
      directory = child;
    }

    const path = parts.join("/");
    if (seenFiles.has(path)) {
      const existing = directory.children.find(
        (node): node is StorageFileNode => node.kind === "file" && node.path === path,
      );
      if (existing && file !== defaultStorageEntries.find((entry) => entry.path === file.path)) {
        Object.assign(existing, file, { path, name: parts.at(-1)! });
      }
      continue;
    }

    seenFiles.add(path);
    directory.children.push({ ...file, kind: "file", path, name: parts.at(-1)! });
  }

  const sort = (nodes: StorageTreeNode[]) => {
    nodes.sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === "directory" ? -1 : 1;
      return left.name.localeCompare(right.name);
    });
    for (const node of nodes) {
      if (node.kind === "directory") {
        node.children = node.children.filter(
          (child) => !(child.kind === "file" && child.name === ".gitkeep"),
        );
        sort(node.children);
      }
    }
  };
  sort(root.children);

  return root.children;
}
