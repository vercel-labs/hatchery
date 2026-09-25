// An AI SDK UI message stream response the test drives chunk by chunk, standing
// in for POST /api/chat and GET /api/chat/{id}/stream.
export function uiStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(current) {
      controller = current;
    },
  });
  return {
    response: new Response(body, {
      headers: {
        "content-type": "text/event-stream",
        "x-vercel-ai-ui-message-stream": "v1",
      },
    }),
    push(...chunks: object[]) {
      for (const chunk of chunks)
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`));
    },
    text(id: string, text: string) {
      this.push(
        { type: "text-start", id },
        { type: "text-delta", id, delta: text },
      );
    },
    close() {
      controller.enqueue(encoder.encode("data: [DONE]\n\n"));
      controller.close();
    },
    fail() {
      controller.error(new Error("connection lost"));
    },
  };
}
