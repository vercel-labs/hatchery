export async function tracesUrl(traceId: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(traceId),
  );
  const otelTraceId = Array.from(new Uint8Array(digest).slice(0, 16), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return `https://traces.playground-vercel.tools/traces/${otelTraceId}`;
}
