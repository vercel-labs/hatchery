"""Pure, defensive projections of chats and worker tasks into a hierarchy."""

import dataclasses
import typing

from worker import models


@dataclasses.dataclass(frozen=True)
class ThreadSummary:
    """The small piece of chat state needed to create the hierarchy root."""

    id: str
    title: str
    summary: str | None = None
    status: str | None = None


class RootSummary(typing.Protocol):
    id: str
    title: str


@dataclasses.dataclass(frozen=True)
class HierarchyNode:
    id: str
    kind: typing.Literal["thread", "task", "fx"]
    parent_id: str | None
    root_task_id: str | None
    depth: int
    objective: str | None
    delegation_order: int
    title: str
    status: str | None
    summary: str | None
    task: models.Task | None = None
    children: tuple["HierarchyNode", ...] = ()


def project(
    root: RootSummary,
    tasks: typing.Iterable[models.Task],
) -> HierarchyNode:
    """Build one thread-rooted tree, attaching malformed task links to the root.

    ``root`` may be a ``ThreadSummary`` or the existing app ``Thread`` model. Stored
    depth and root IDs are treated as hints only. The projection derives both
    from parent links so stale metadata cannot make the result cyclic. Tasks
    from another chat and duplicate task IDs are ignored.
    """
    root_id = str(root.id)
    root_title = str(root.title)
    root_summary = getattr(root, "summary", None) or getattr(root, "artifact", None)
    root_status = getattr(root, "status", None)
    by_id: dict[str, models.Task] = {}
    for task in tasks:
        if task.chat_id == root_id and task.id not in by_id and task.id != root_id:
            by_id[task.id] = task

    parent_ids: dict[str, str | None] = {}
    for task_id, task in by_id.items():
        parent_id = task.parent_task_id
        parent_ids[task_id] = parent_id if parent_id in by_id and parent_id != task_id else None

    # Break every cycle at each member. Descendants may still safely hang from a
    # former cycle member after those members become roots.
    for task_id in by_id:
        path: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = task_id
        while current is not None and current not in positions:
            positions[current] = len(path)
            path.append(current)
            current = parent_ids[current]
        if current is not None:
            for member in path[positions[current] :]:
                parent_ids[member] = None

    children: dict[str | None, list[str]] = {None: []}
    for task_id, parent_id in parent_ids.items():
        children.setdefault(parent_id, []).append(task_id)

    def ordering(task_id: str) -> tuple[int, str, str]:
        task = by_id[task_id]
        return task.delegation_order, task.created_at, task.id

    for child_ids in children.values():
        child_ids.sort(key=ordering)

    def fx_nodes(task: models.Task, depth: int, root_task_id: str) -> tuple[HierarchyNode, ...]:
        sessions = {
            str(item["id"]): item
            for item in task.fx_sessions
            if isinstance(item.get("id"), str) and item["id"] != task.fx_session_id
        }
        parent_of = {
            session_id: (
                str(item.get("parent_id"))
                if item.get("parent_id") in sessions
                else None
            )
            for session_id, item in sessions.items()
        }
        for session_id in sessions:
            path: list[str] = []
            positions: dict[str, int] = {}
            current: str | None = session_id
            while current is not None and current not in positions:
                positions[current] = len(path)
                path.append(current)
                current = parent_of[current]
            if current is not None:
                for member in path[positions[current] :]:
                    parent_of[member] = None
        children: dict[str | None, list[str]] = {None: []}
        for session_id, parent_id in parent_of.items():
            children.setdefault(parent_id, []).append(session_id)
        for values in children.values():
            values.sort()

        def node(session_id: str, node_depth: int) -> HierarchyNode:
            identifier = f"{task.id}:{session_id}"
            return HierarchyNode(
                id=identifier,
                kind="fx",
                parent_id=(
                    f"{task.id}:{parent_of[session_id]}"
                    if parent_of[session_id]
                    else task.id
                ),
                root_task_id=root_task_id,
                depth=node_depth,
                objective="fx subagent",
                delegation_order=0,
                title=f"fx subagent {session_id[:8]}",
                status=task.status,
                summary=None,
                children=tuple(
                    node(child_id, node_depth + 1)
                    for child_id in children.get(session_id, ())
                ),
            )

        return tuple(node(session_id, depth) for session_id in children[None])

    def task_node(task_id: str, depth: int, root_task_id: str) -> HierarchyNode:
        task = by_id[task_id]
        summary = None
        if task.result:
            value = task.result.get("summary") or task.result.get("error")
            summary = str(value) if value is not None else None
        summary = summary or task.completion_message or task.last_agent_words
        return HierarchyNode(
            id=task.id,
            kind="task",
            parent_id=parent_ids[task.id] or root_id,
            root_task_id=root_task_id,
            depth=depth,
            objective=task.objective or task.prompt,
            delegation_order=task.delegation_order,
            title=task.title,
            status=task.status,
            summary=summary,
            task=task,
            children=(
                tuple(
                    task_node(child_id, depth + 1, root_task_id)
                    for child_id in children.get(task.id, ())
                )
                + fx_nodes(task, depth + 1, root_task_id)
            ),
        )

    return HierarchyNode(
        id=root_id,
        kind="thread",
        parent_id=None,
        root_task_id=None,
        depth=0,
        objective=root_summary or root_title,
        delegation_order=0,
        title=root_title,
        status=root_status,
        summary=root_summary,
        children=tuple(task_node(task_id, 1, task_id) for task_id in children[None]),
    )


def subtree(root: HierarchyNode, node_id: str) -> HierarchyNode | None:
    """Return a projected node and its descendants without mutating the tree."""
    pending = [root]
    while pending:
        node = pending.pop()
        if node.id == node_id:
            return node
        pending.extend(reversed(node.children))
    return None


project_hierarchy = project
find_subtree = subtree
