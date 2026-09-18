import { ChevronRightIcon, FileTextIcon, FolderIcon, FolderOpenIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

import {
  buildStorageFileTree,
  type StorageFile,
  type StorageTreeNode,
} from "./file-tree";

export type StorageFileTreeProps = {
  files: readonly StorageFile[];
  selectedPath?: string | null;
  onSelect?: (file: StorageFile) => void;
  includeDefaults?: boolean;
  className?: string;
};

function TreeNode({
  node,
  depth,
  selectedPath,
  onSelect,
}: {
  node: StorageTreeNode;
  depth: number;
  selectedPath?: string | null;
  onSelect?: (file: StorageFile) => void;
}) {
  const inset = { paddingInlineStart: `${depth * 0.875 + 0.5}rem` };

  if (node.kind === "file") {
    return (
      <Button
        variant={node.path === selectedPath ? "secondary" : "ghost"}
        size="sm"
        className="w-full min-w-0 justify-start"
        style={inset}
        onClick={() => onSelect?.(node)}
        aria-current={node.path === selectedPath ? "page" : undefined}
      >
        <FileTextIcon data-icon="inline-start" />
        <span className="truncate">{node.name}</span>
      </Button>
    );
  }

  return (
    <Collapsible defaultOpen>
      <CollapsibleTrigger
        className="group flex h-7 w-full min-w-0 items-center gap-1 rounded-lg pr-2 text-sm outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
        style={inset}
      >
        <ChevronRightIcon className="size-3.5 shrink-0 transition-transform duration-200 group-data-panel-open:rotate-90 motion-reduce:transition-none" />
        <FolderIcon className="size-4 shrink-0 group-data-panel-open:hidden" />
        <FolderOpenIcon className="hidden size-4 shrink-0 group-data-panel-open:block" />
        <span className="truncate">{node.name}</span>
      </CollapsibleTrigger>
      <CollapsibleContent className="h-[var(--collapsible-panel-height)] overflow-hidden transition-[height,opacity] duration-200 ease-out motion-reduce:transition-none data-ending-style:h-0 data-ending-style:opacity-0 data-starting-style:h-0 data-starting-style:opacity-0">
        {node.children.map((child) => (
          <TreeNode
            key={`${child.kind}:${child.path}`}
            node={child}
            depth={depth + 1}
            selectedPath={selectedPath}
            onSelect={onSelect}
          />
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function StorageFileTree({
  files,
  selectedPath,
  onSelect,
  includeDefaults = true,
  className,
}: StorageFileTreeProps) {
  const tree = buildStorageFileTree(files, includeDefaults);

  return (
    <ScrollArea className={cn("h-full min-h-0", className)}>
      {tree.length ? (
        <div className="flex min-w-0 flex-col gap-0.5 p-1" aria-label="Agent storage">
          {tree.map((node) => (
            <TreeNode
              key={`${node.kind}:${node.path}`}
              node={node}
              depth={0}
              selectedPath={selectedPath}
              onSelect={onSelect}
            />
          ))}
        </div>
      ) : (
        <Empty>
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FolderIcon />
            </EmptyMedia>
            <EmptyTitle>No files</EmptyTitle>
            <EmptyDescription>This agent has no stored files.</EmptyDescription>
          </EmptyHeader>
        </Empty>
      )}
    </ScrollArea>
  );
}
