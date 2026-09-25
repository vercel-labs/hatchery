import type { Thread } from "@/lib/api-types";

export function threadToolCounts(thread: Thread) {
  const active = thread.live && thread.activity?.phase !== "terminal";
  const running = active && thread.activity?.running_tool ? 1 : 0;
  const queued = active ? (thread.activity?.queued_tools ?? 0) : 0;
  return { running, queued, pending: running + queued };
}

export function pendingPrompts(thread: Thread) {
  return thread.live && thread.activity?.phase !== "terminal"
    ? (thread.activity?.pending_prompts ?? 0)
    : 0;
}

export function runningCommand(thread: Thread): string {
  const tool = thread.activity?.running_tool;
  if (!tool) return "";
  try {
    const args: unknown = JSON.parse(tool.tool_args);
    if (
      args &&
      typeof args === "object" &&
      "command" in args &&
      typeof args.command === "string"
    )
      return args.command;
  } catch {
    // Keep the original arguments visible if an older tool cannot be decoded.
  }
  return tool.tool_args || tool.tool_name;
}

export function threadActivity(thread: Thread) {
  const activity = thread.activity;
  const status = activity?.status ?? thread.status;
  const archived = thread.archived || status === "archived";
  const terminal = archived || activity?.phase === "terminal" || !thread.live;
  const failed = status === "failed" || status === "cancelled";
  const budgetHeld = Boolean(
    !terminal &&
    activity?.budget_held &&
    (thread.status === "waiting" || activity.phase !== "leased"),
  );
  const retrying = activity?.schedules.some(
    (schedule) => schedule.key === "model-retry",
  );
  const waitingForCommandShutdown = activity?.schedules.some(
    (schedule) => schedule.key === "quiescence",
  );
  const parked = activity?.phase !== "leased" && status === "parked";
  const working =
    !terminal &&
    !parked &&
    !budgetHeld &&
    !retrying &&
    !waitingForCommandShutdown &&
    (activity
      ? activity.phase === "leased" || Boolean(activity.running_tool)
      : status === "active" || status === "handing_off");
  let label: string;
  if (archived) label = "Archived";
  else if (terminal)
    label = failed
      ? status === "failed"
        ? "Failed"
        : "Cancelled"
      : "Finished";
  else if (parked) label = "Needs attention";
  else if (budgetHeld) label = "Paused: budget exhausted";
  else if (activity?.running_tool) {
    const name = activity.running_tool.tool_name;
    label =
      name === "bash"
        ? "Running command"
        : name === "message_task"
          ? "Messaging subagent"
          : name === "delegate"
            ? "Delegating task"
            : name === "skill_view"
              ? "Reading skill"
              : name === "review_proposal"
                ? "Reviewing proposal"
                : `Running ${name.replaceAll("_", " ")}`;
  } else if (activity?.consolidating.length)
    label = `Curating ${activity.consolidating[0]}`;
  else if (retrying) label = "Waiting to retry";
  else if (waitingForCommandShutdown) label = "Waiting for command shutdown";
  else if (activity?.phase === "leased")
    label =
      status === "parked"
        ? "Resuming"
        : status === "idle" || status === "sleeping"
          ? "Working in background"
          : "Working";
  else if (activity && activity.mailbox_depth > 0)
    label = "Queued for processing";
  else if (activity?.awaiting_admission)
    label =
      thread.status === "waiting"
        ? "Waiting for budget"
        : "Waiting for turn admission";
  else if (status === "waiting") label = "Waiting for budget";
  else if (status === "active")
    label = activity ? "Waiting for work" : "Working";
  else if (status === "handing_off") label = "Publishing changes";
  else if (thread.parent_thread_id && thread.task_status === "completed")
    label = "Completed";
  else if (thread.parent_thread_id && thread.task_status === "cancelled")
    label = "Cancelled";
  else if (thread.parent_thread_id && thread.task_status === "working")
    label = "Waiting for input";
  else label = "Ready for your reply";

  return {
    label,
    working,
    budgetHeld,
    tone:
      failed || parked
        ? "error"
        : budgetHeld
          ? "attention"
          : terminal ||
              (thread.parent_thread_id !== "" &&
                (thread.task_status === "completed" ||
                  thread.task_status === "cancelled"))
            ? "muted"
            : working
              ? "working"
              : thread.parent_thread_id && thread.task_status === "working"
                ? "waiting"
                : status === "idle" && !activity?.mailbox_depth
                  ? "ready"
                  : "waiting",
  };
}
