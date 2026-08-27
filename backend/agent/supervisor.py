"""Durable supervision schedule for subagents."""

from vercel import workflow

workflows = workflow.Workflows()


@workflows.step
async def check(launch_id: str) -> bool:
    """Run one periodic check; true means supervision is finished."""
    from app import server

    return await server.supervise_task(launch_id, "periodic")


@workflows.workflow
async def supervise(launch_id: str) -> None:
    for delay in (60, 120):
        await workflow.sleep(delay)
        if await check(launch_id):
            return
    while True:
        await workflow.sleep(300)
        if await check(launch_id):
            return
