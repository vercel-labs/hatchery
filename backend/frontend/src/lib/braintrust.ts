export async function braintrustTraceUrl(traceId: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(traceId),
  );
  const rootSpanId = Array.from(new Uint8Array(digest).slice(0, 16), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `https://www.braintrust.dev/app/anbuzin%27s%20projects/p/braintrust-coffee-flame/logs?r=${rootSpanId}`;
}
