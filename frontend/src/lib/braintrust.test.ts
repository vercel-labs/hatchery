import assert from "node:assert/strict";
import test from "node:test";

import { braintrustTraceUrl } from "./braintrust.ts";

test("converts an AI SDK trace ID to Braintrust's root span ID", async () => {
  assert.equal(
    await braintrustTraceUrl("trace_6b3cf6f282cd"),
    "https://www.braintrust.dev/app/anbuzin%27s%20projects/p/braintrust-coffee-flame/logs?r=1a362da7aca90587cb4947ec6b3f54f6",
  );
});
