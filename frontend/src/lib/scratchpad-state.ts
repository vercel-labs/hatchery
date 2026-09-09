import type { ScratchpadView } from "./api.ts";

export type ScratchpadDraft = {
  content: string;
  expectedVersion: number;
  conflictHeadVersion: number | null;
};

export function draftFromView(view: ScratchpadView): ScratchpadDraft {
  return {
    content: view.snapshot.content,
    expectedVersion: view.snapshot.version,
    conflictHeadVersion: null,
  };
}

export function recordScratchpadConflict(
  draft: ScratchpadDraft,
  current: ScratchpadView,
): ScratchpadDraft {
  return { ...draft, conflictHeadVersion: current.head_version };
}

export function overwriteExpectedVersion(draft: ScratchpadDraft): number {
  if (draft.conflictHeadVersion === null) {
    throw new Error("overwrite requires explicit conflict confirmation");
  }
  return draft.conflictHeadVersion;
}

export function shouldRenderScratchpadDiff(view: ScratchpadView): boolean {
  return view.unread && view.snapshot.version === view.head_version;
}
