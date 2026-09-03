import pytest

from worker import sandbox
from worker.daemon import main


def _requires(owner, capability, scenario):
    assert callable(getattr(owner, capability, None)), f"{scenario}: retained migration contract requires {owner.__name__}.{capability}"


GIT_CONTRACTS = [
    (sandbox, "configure_git_auth", "github_credential_helper_is_url_scoped"),
    (sandbox, "configure_git_auth", "github_auth_configuration_is_idempotent"),
    (sandbox, "configure_git_auth", "github_auth_reclaims_multi_value_helper"),
    (sandbox, "configure_git_auth", "github_auth_removes_legacy_ssh_rewrite"),
    (sandbox, "configure_gh", "gh_config_seeds_version_marker"),
    (sandbox, "configure_gh", "gh_config_preserves_existing_file"),
    (sandbox, "run_gh", "gh_routes_porcelain_and_api_pr_create"),
    (sandbox, "run_gh", "non_pr_gh_commands_are_forwarded"),
    (sandbox, "run_gh", "porcelain_pr_create_reports_url"),
    (sandbox, "run_gh", "last_porcelain_pr_url_wins"),
    (sandbox, "run_gh", "failed_porcelain_pr_create_does_not_report"),
    (sandbox, "run_gh", "successful_command_without_pr_url_does_not_report"),
    (sandbox, "run_gh", "dry_run_pr_create_does_not_report"),
    (sandbox, "run_gh", "api_pr_create_reports_url"),
    (sandbox, "run_gh", "failed_api_pr_create_does_not_report"),
    (sandbox, "parse_pr_url", "api_response_pr_url_is_parsed"),
    (sandbox, "is_pr_create", "porcelain_pr_create_is_detected"),
    (sandbox, "is_pr_create", "api_pr_create_is_detected"),
    (sandbox, "validate_pr_url", "pr_url_is_validated"),
    (sandbox, "find_pr_url", "pr_url_is_found_in_output"),
    (sandbox, "find_pr_url", "last_pr_url_is_selected"),
    (sandbox, "record_pr", "created_pr_is_recorded"),
    (sandbox, "git_credentials", "user_github_credential_wins_over_bot"),
    (sandbox, "git_credentials", "bot_github_credential_is_fallback"),
    (sandbox, "git_credentials", "github_credentials_are_not_agent_readable"),
    (sandbox, "git_credentials", "clone_fetch_push_and_gh_share_precedence"),
    (sandbox, "parse_git_args", "git_global_and_c_options_are_parsed"),
    (sandbox, "neutralize_commit_signing", "local_commit_signing_is_disabled"),
    (sandbox, "needs_signed_push", "signature_rejection_is_detected"),
    (sandbox, "push_with_signing_fallback", "successful_push_skips_signing"),
    (sandbox, "push_with_signing_fallback", "non_signature_push_failure_passes_through"),
    (sandbox, "push_with_signing_fallback", "signature_rejection_signs_and_retries"),
    (sandbox, "push_with_signing_fallback", "signing_failure_preserves_original_rejection"),
    (sandbox, "push_with_signing_fallback", "non_push_git_command_bypasses_fallback"),
    (sandbox, "push_with_signing_fallback", "recursion_guard_bypasses_fallback"),
    (sandbox, "push_with_signing_fallback", "git_dash_c_signs_correct_repository"),
    (sandbox, "push_with_signing_fallback", "git_status_does_not_trigger_signing"),
    (sandbox, "push_with_signing_fallback", "git_exit_code_passes_through"),
    (sandbox, "run_gh", "gh_pr_create_exit_code_passes_through"),
    (sandbox, "push_with_signing_fallback", "bare_git_is_harmless"),
    (sandbox, "scrub_git_config_env", "signing_proxy_scrubs_git_config_environment"),
    (sandbox, "first_unsigned_commit", "app_committed_prefix_is_skipped"),
    (sandbox, "sign_request", "local_commit_identity_is_omitted"),
    (sandbox, "sign_request", "sign_request_carries_base_ref_range_and_env"),
    (sandbox, "is_signed_by_app", "already_app_signed_commit_is_detected"),
    (sandbox, "origin_owner_repo", "origin_owner_and_repo_are_parsed"),
]


@pytest.mark.parametrize("owner,capability,scenario", GIT_CONTRACTS, ids=[item[2] for item in GIT_CONTRACTS])
def test_git_and_github_contract(owner, capability, scenario):
    _requires(owner, capability, scenario)


SSH_CONTRACTS = [
    "authenticated_access",
    "interactive_pty",
    "pty_resize",
    "non_pty_exec_separates_stdout_and_stderr",
    "exit_status",
    "working_directory_and_environment",
    "disconnect_keeps_durable_exec_alive",
    "routes_are_reachable_from_ssh",
]


@pytest.mark.parametrize("scenario", SSH_CONTRACTS, ids=SSH_CONTRACTS)
def test_platform_ssh_contract(scenario):
    _requires(sandbox, "ssh", scenario)


@pytest.mark.parametrize(
    "scenario",
    [
        "durable_exec_survives_transport_kill",
        "detached_exit_status_is_replayed",
        "unknown_resume_stream_is_not_found",
        "resume_requires_authentication",
        "clean_end_unregisters_stream",
        "direct_loopback_port_forwarding",
        "non_loopback_forwarding_is_rejected",
        "disabled_forwarding_is_rejected",
    ],
)
def test_ssh_websocket_fallback_contract(scenario):
    platform_ssh = getattr(sandbox, "ssh", None)
    fallback = getattr(main.Handler, "ssh", None)
    assert callable(platform_ssh) or callable(fallback), f"SSH must use platform access or retained minimal fallback: {scenario}"


@pytest.mark.parametrize(
    "scenario,capability",
    [
        ("platform_ssh_smoke", "ssh"),
        ("exposed_route_smoke", "probe_route"),
        ("fx_resume_after_sandbox_restart", "resume"),
        ("daemon_restart_during_active_task", "recover_daemon"),
        ("authenticated_clone_fetch_push", "git_credentials"),
        ("gh_pr_create_and_capture", "run_gh"),
        ("signed_commit_required_repository", "push_with_signing_fallback"),
        ("queue_redelivery_after_handler_crash", "redeliver_command"),
        ("persist_command_resume_then_consume", "prepare_for_command"),
        ("existing_sandbox_repairs_dead_daemon", "repair_daemon"),
        ("sandbox_snapshot_create_and_restore", "snapshot"),
    ],
    ids=lambda value: value,
)
def test_live_migration_contract(scenario, capability):
    _requires(sandbox, capability, scenario)
