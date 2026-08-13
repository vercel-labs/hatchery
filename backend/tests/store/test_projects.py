from store import projects


async def test_create_is_idempotent_by_name():
    first = await projects.create("sdk")
    second = await projects.create("sdk")
    assert first.id == second.id


async def test_default_project_is_stable():
    project = await projects.get_default()
    assert project.name == projects.DEFAULT_NAME
    assert (await projects.get_default()).id == project.id


async def test_memory_and_repos_round_trip():
    project = await projects.create("sdk")
    await projects.set_memory(project.id, "direction: port all e2e tests")
    await projects.set_repos(project.id, ["vercel/workflow", "vercel/vercel-py"])
    loaded = await projects.get(project.id)
    assert loaded is not None
    assert loaded.memory == "direction: port all e2e tests"
    assert loaded.repos == ["vercel/workflow", "vercel/vercel-py"]


async def test_for_repo_routes_to_owning_project():
    project = await projects.create("sdk")
    await projects.set_repos(project.id, ["vercel/workflow"])
    owner = await projects.for_repo("vercel/workflow")
    assert owner is not None and owner.id == project.id
    assert await projects.for_repo("vercel/other") is None


async def test_get_unknown_returns_none():
    assert await projects.get("prj_missing") is None
    assert await projects.set_memory("prj_missing", "x") is None


async def test_listing_orders_by_creation():
    await projects.create("a")
    await projects.create("b")
    names = [p.name for p in await projects.list_projects()]
    assert names == ["a", "b"]
