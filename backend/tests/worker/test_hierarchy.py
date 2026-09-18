from worker import hierarchy, models


def _task(task_id, *, parent=None, order=0, chat_id="chat_1", result=None):
    return models.Task(
        id=task_id,
        chat_id=chat_id,
        worker_id="wrk_1",
        parent_task_id=parent,
        root_task_id="stale",
        depth=99,
        objective=f"objective {task_id}",
        delegation_order=order,
        title=task_id,
        prompt=f"prompt {task_id}",
        model="openai/test",
        result=result,
        created_at=f"2026-09-17T00:00:0{order}+00:00",
        updated_at="2026-09-17T00:00:00+00:00",
    )


def test_projects_chat_and_tasks_in_delegation_order():
    projected = hierarchy.project(
        hierarchy.ThreadSummary("chat_1", "Ship it", "Root summary", "running"),
        [
            _task("second", order=2),
            _task("child", parent="first", order=0, result={"summary": "done"}),
            _task("first", order=1),
            _task("foreign", chat_id="chat_2"),
        ],
    )

    assert projected.id == "chat_1"
    assert projected.summary == "Root summary"
    assert [node.id for node in projected.children] == ["first", "second"]
    child = projected.children[0].children[0]
    assert (child.parent_id, child.root_task_id, child.depth) == ("first", "first", 2)
    assert child.objective == "objective child"
    assert child.summary == "done"
    assert hierarchy.subtree(projected, "first") == projected.children[0]
    assert hierarchy.subtree(projected, "missing") is None


def test_projection_is_cycle_and_orphan_safe():
    projected = hierarchy.project(
        hierarchy.ThreadSummary("chat_1", "Root"),
        [
            _task("a", parent="b"),
            _task("b", parent="a"),
            _task("orphan", parent="missing"),
            _task("self", parent="self"),
            _task("descendant", parent="a"),
        ],
    )

    assert {node.id for node in projected.children} == {"a", "b", "orphan", "self"}
    assert [node.id for node in hierarchy.subtree(projected, "a").children] == ["descendant"]
    assert all(node.parent_id == "chat_1" for node in projected.children)


def test_projects_nested_fx_sessions_beneath_worker_task():
    task = _task("worker")
    task.fx_session_id = "root-session"
    task.fx_sessions = [
        {"id": "child", "parent_id": "root-session"},
        {"id": "grandchild", "parent_id": "child"},
    ]

    projected = hierarchy.project(hierarchy.ThreadSummary("chat_1", "Root"), [task])

    child = projected.children[0].children[0]
    assert child.kind == "fx"
    assert child.id == "worker:child"
    assert child.parent_id == "worker"
    assert child.children[0].id == "worker:grandchild"
