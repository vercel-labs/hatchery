import assert from "node:assert/strict";
import test from "node:test";

import { tracesUrl } from "./traces.ts";

test("converts an AI SDK trace ID to a Traces URL using the first 16 SHA-256 bytes", async () => {
  assert.equal(
    await tracesUrl("trace_6b3cf6f282cd"),
    "https://traces.playground-vercel.tools/traces/1a362da7aca90587cb4947ec6b3f54f6",
  );
});
