import assert from "node:assert/strict";
import test from "node:test";

import type { ChatMessagePart, ChatUIMessage } from "./messages.ts";
import { sharingTimeline, type SharedThread } from "./sharing.ts";

function thread(overrides: Partial<SharedThread> = {}): SharedThread {
  return {
    id: "sharing-1",
    provider: "slack",
    label: "#engineering",
    url: "https://example.slack.com/archives/C1/p123",
    text: "I’ll post progress here.\nPlease reply in this thread.",
    tool_call_id: "call-1",
    message_id: "external-root-1",
    ...overrides,
  };
}

function sent(
  sharing = thread(),
  callId = "call-1",
): Extract<ChatMessagePart, { type: "dynamic-tool" }> {
  return {
    type: "dynamic-tool",
    toolName: "start_shared_thread",
    toolCallId: callId,
    state: "output-available",
    input: {},
    output: { status: "sent", sharing, extra: "ignored" },
  };
}

function message(parts: ChatMessagePart[], id = "assistant-1"): ChatUIMessage {
  return { id, role: "assistant", parts };
}

const before: ChatMessagePart = { type: "text", text: "Before sharing" };
const after: ChatMessagePart = { type: "text", text: "After sharing" };

test("extracts sent sharing and places the marker/notification at the exact tool position", () => {
  // A successful result anchors to the actual part, even without a saved call ID.
  const sharing = thread({ tool_call_id: null });
  const result = sharingTimeline([message([before, sent(sharing), after])], []);
  assert.deepEqual(result.messages[0].parts, [
    before,
    { type: "shared-thread", sharing },
    after,
  ]);
  assert.deepEqual(result.threads, [sharing]);
  assert.deepEqual(result.fallback, []);
});

test("recognizes static tool parts as well as dynamic tools", () => {
  const part: ChatMessagePart = {
    type: "tool-start_shared_thread",
    toolCallId: "call-1",
    state: "output-available",
    input: {},
    output: { status: "sent", sharing: thread() },
  };
  const result = sharingTimeline([message([part])], []);
  assert.deepEqual(result.messages[0].parts, [{ type: "shared-thread", sharing: thread() }]);
});

test("deduplicates persisted, streamed, and replayed sharing by stable record ID", () => {
  const saved = thread({ label: "#renamed" });
  const result = sharingTimeline([
    message([sent(), sent()]),
    message([sent()], "replayed-assistant"),
  ], [saved, saved]);
  assert.deepEqual(result.threads, [saved]);
  assert.deepEqual(result.messages.flatMap((item) => item.parts), [
    { type: "shared-thread", sharing: saved },
  ]);
  assert.deepEqual(result.fallback, []);
});

test("the same record returned by another call still has only one notification and no JSON card", () => {
  const result = sharingTimeline([message([sent(), sent(thread(), "call-2")])], [thread()]);
  assert.deepEqual(result.messages[0].parts, [{ type: "shared-thread", sharing: thread() }]);
});

test("recovers saved notifications at an unfinished or failed tool call without its result", () => {
  const unfinished: ChatMessagePart = {
    type: "dynamic-tool",
    toolName: "start_shared_thread",
    toolCallId: "call-1",
    state: "input-available",
    input: {},
  };
  const failed: ChatMessagePart = { ...unfinished, state: "output-error", errorText: "Turn failed" };
  for (const part of [unfinished, failed]) {
    const result = sharingTimeline([message([before, part, after])], [thread()]);
    assert.deepEqual(result.messages[0].parts, [
      before,
      { type: "shared-thread", sharing: thread() },
      after,
    ]);
    assert.deepEqual(result.fallback, []);
  }
});

test("missing tool anchors fall back at the end, never at the external root message ID", () => {
  const original = message([before], "external-root-1");
  const unanchored = thread({ id: "sharing-2", tool_call_id: null });
  const result = sharingTimeline([original, message([after])], [thread(), unanchored, thread()]);
  assert.deepEqual(result.messages, [original, message([after])]);
  assert.deepEqual(result.fallback, [thread(), unanchored]);
  assert.deepEqual(sharingTimeline([], [thread()]).fallback, [thread()]);
});

test("a saved fallback moves to its tool position once messages arrive without duplication", () => {
  const saved = [thread()];
  assert.equal(sharingTimeline([], saved).fallback.length, 1);
  const reloaded = sharingTimeline([message([before, sent(), after])], saved);
  assert.deepEqual(reloaded.messages[0].parts, [before, { type: "shared-thread", sharing: thread() }, after]);
  assert.equal(reloaded.threads.length, 1);
  assert.deepEqual(reloaded.fallback, []);
});

test("preserves multiple destinations, including separate threads on the same platform", () => {
  const slack = thread({ id: "sharing-2", tool_call_id: "call-2" });
  const github = thread({ id: "sharing-3", provider: "github", tool_call_id: null, label: "org/repo#42" });
  const result = sharingTimeline([message([sent(), sent(slack, "call-2")])], [thread(), slack, github]);
  assert.deepEqual(result.threads, [thread(), slack, github]);
  assert.deepEqual(result.messages[0].parts, [
    { type: "shared-thread", sharing: thread() },
    { type: "shared-thread", sharing: slack },
  ]);
  assert.deepEqual(result.fallback, [github]);
});

test("ignores invalidated streamed results but keeps durable sharing as a fallback", () => {
  const parts: ChatMessagePart[] = [before, { type: "step-start" }, sent(), { type: "data-reload", data: null }];
  const withoutSaved = sharingTimeline([message(parts)], []);
  assert.deepEqual(withoutSaved.threads, []);
  const withSaved = sharingTimeline([message(parts)], [thread()]);
  assert.deepEqual(withSaved.messages[0].parts, [before, { type: "step-start" }]);
  assert.deepEqual(withSaved.fallback, [thread()]);
});

test("leaves one-off send_message, unsuccessful results, and malformed output unchanged", () => {
  const parts: ChatMessagePart[] = [
    { type: "dynamic-tool", toolName: "send_message", toolCallId: "one-off", state: "output-available", input: {}, output: { status: "sent", sharing: thread() } },
    { type: "dynamic-tool", toolName: "start_shared_thread", toolCallId: "failed", state: "output-available", input: {}, output: { status: "failed", sharing: thread() } },
    { type: "dynamic-tool", toolName: "start_shared_thread", toolCallId: "invalid", state: "output-available", input: {}, output: { status: "sent", sharing: { id: "incomplete" } } },
  ];
  const result = sharingTimeline([message(parts)], []);
  assert.deepEqual(result.messages[0].parts, parts);
  assert.deepEqual(result.threads, []);
  assert.deepEqual(result.fallback, []);
});
