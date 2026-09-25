import assert from "node:assert/strict";
import test from "node:test";

import type { ChatUIMessage } from "../../lib/messages.ts";
import { liveMessages } from "./live-transcript.ts";

test("splits a streamed turn into steps so the answer after tools stays public", () => {
  const streamed = {
    id: "live",
    role: "assistant",
    parts: [
      { type: "step-start" },
      { type: "text", text: "Checking.", state: "done" },
      {
        type: "tool-bash",
        toolCallId: "call-1",
        state: "output-available",
        input: { command: "ls" },
        output: { exit_code: 0, stdout: "a" },
      },
      { type: "step-start" },
      { type: "text", text: "Done.", state: "streaming" },
    ],
  } as unknown as ChatUIMessage;

  const [first, result, answer] = liveMessages([streamed]);

  assert.deepEqual(
    first.parts?.map((part) => part.kind),
    ["text", "tool_call"],
  );
  assert.equal(first.parts?.[1].tool_args, '{"command":"ls"}');
  assert.equal(result.role, "tool");
  assert.deepEqual(result.parts?.[0].result, { exit_code: 0, stdout: "a" });
  assert.deepEqual(answer.parts, [
    { kind: "text", id: "live:4", text: "Done.", streaming: true },
  ]);
});

test("keeps user identity and turns tool errors and assignments into console parts", () => {
  const [user, assignment, step, result] = liveMessages([
    {
      id: "msg_1",
      role: "user",
      metadata: { author: "Ada", origin: "ui" },
      parts: [{ type: "text", text: "Go" }],
    },
    {
      id: "live",
      role: "assistant",
      parts: [
        {
          type: "data-agent-assignment",
          data: { state: "assigned", agent_id: "docs", agent_name: "Docs" },
        },
        {
          type: "tool-create_subagent",
          toolCallId: "call-2",
          state: "output-error",
          input: {},
          errorText: "no sandbox",
        },
      ],
    },
  ] as unknown as ChatUIMessage[]);

  assert.equal(user.request_id, "msg_1");
  assert.equal(user.author, "Ada");
  assert.equal(assignment.source, "signal");
  assert.equal(assignment.parts?.[0].text, "Assigned Docs");
  assert.equal(step.parts?.[0].tool_name, "create_subagent");
  assert.equal(result.parts?.[0].result_kind, "error");
  assert.equal(result.parts?.[0].result, "no sandbox");
});
