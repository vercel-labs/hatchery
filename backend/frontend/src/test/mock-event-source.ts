// Hatchery's chat event stream. Tests push events with `MockEventSource.emit`.
export class MockEventSource {
  static instances: MockEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  static emit(data: unknown) {
    for (const source of MockEventSource.instances)
      if (!source.closed)
        source.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }
}
