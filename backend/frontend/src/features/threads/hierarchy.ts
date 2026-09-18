export type HierarchyItem = {
  id: string;
  parentId?: string | null;
};

export type HierarchyNode<T extends HierarchyItem> = {
  item: T;
  children: HierarchyNode<T>[];
  orphaned: boolean;
  cyclic: boolean;
};

export function buildHierarchy<T extends HierarchyItem>(
  items: readonly T[],
): HierarchyNode<T>[] {
  const unique = new Map<string, T>();
  for (const item of items) {
    if (!unique.has(item.id)) unique.set(item.id, item);
  }

  const cyclicIds = new Set<string>();
  for (const item of unique.values()) {
    const path: string[] = [];
    const positions = new Map<string, number>();
    let cursor: T | undefined = item;

    while (cursor) {
      const position = positions.get(cursor.id);
      if (position !== undefined) {
        for (const id of path.slice(position)) cyclicIds.add(id);
        break;
      }
      positions.set(cursor.id, path.length);
      path.push(cursor.id);
      cursor = cursor.parentId ? unique.get(cursor.parentId) : undefined;
    }
  }

  const nodes = new Map<string, HierarchyNode<T>>();
  for (const item of unique.values()) {
    nodes.set(item.id, {
      item,
      children: [],
      orphaned: Boolean(item.parentId && !unique.has(item.parentId)),
      cyclic: cyclicIds.has(item.id),
    });
  }

  const roots: HierarchyNode<T>[] = [];
  for (const item of unique.values()) {
    const node = nodes.get(item.id)!;
    const parent = item.parentId ? nodes.get(item.parentId) : undefined;
    if (!parent || node.cyclic || parent === node) {
      roots.push(node);
    } else {
      parent.children.push(node);
    }
  }

  return roots;
}
