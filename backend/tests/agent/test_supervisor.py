from agent import supervisor


def test_supervision_workflow_is_registered():
    assert supervisor.supervise.workflow_id.endswith("supervise")
    assert supervisor.check.name.endswith("check")
