import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { PromptForm } from "@/components/prompt-form";

// Ported from agentmesh console tests/composer.test.tsx onto Hatchery's composer.
function Composer({
  onSend,
  onStop = () => {},
  working = false,
}: {
  onSend: (text: string) => Promise<boolean>;
  onStop?: () => Promise<void> | void;
  working?: boolean;
}) {
  return (
    <PromptForm
      isBusy={working}
      traceId={null}
      agents={[]}
      agentId={null}
      showMarkAsRead={false}
      isMarkingAsRead={false}
      onSubmit={({ text }) => onSend(text)}
      onStop={onStop}
      onAgentChange={() => {}}
      onMarkAsRead={() => {}}
    />
  );
}

it("sends with Enter and keeps Shift+Enter as a newline", async () => {
  const send = vi.fn(async () => true);
  render(<Composer onSend={send} />);
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  const user = userEvent.setup();

  await user.type(composer, "First line");
  await user.keyboard("{Shift>}{Enter}{/Shift}Second line");
  expect(composer.value).toBe("First line\nSecond line");
  expect(send).not.toHaveBeenCalled();

  await user.keyboard("{Enter}");
  expect(send).toHaveBeenCalledWith("First line\nSecond line");
  await waitFor(() => expect(composer.value).toBe(""));
  await waitFor(() => expect(document.activeElement).toBe(composer));
});

it("keeps the input focused when sending with the button", async () => {
  const send = vi.fn(async () => true);
  render(<Composer onSend={send} />);
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  const user = userEvent.setup();

  await user.type(composer, "Hello");
  await user.click(screen.getByRole("button", { name: "Submit" }));

  expect(send).toHaveBeenCalledWith("Hello");
  await waitFor(() => expect(document.activeElement).toBe(composer));
});

it("leaves the input enabled while a message is being sent", async () => {
  let finishSend: ((sent: boolean) => void) | undefined;
  const send = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        finishSend = resolve;
      }),
  );
  render(<Composer onSend={send} />);
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  const user = userEvent.setup();

  await user.type(composer, "Hello");
  await user.keyboard("{Enter}");

  expect(composer.disabled).toBe(false);
  expect(composer.value).toBe("");
  expect(document.activeElement).toBe(composer);
  finishSend?.(true);
  await waitFor(() => expect(composer.value).toBe(""));
});

it("restores the message when sending fails", async () => {
  const send = vi.fn(async () => false);
  render(<Composer onSend={send} />);
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  const user = userEvent.setup();

  await user.type(composer, "Try again");
  await user.keyboard("{Enter}");

  await waitFor(() => expect(composer.value).toBe("Try again"));
});

it("shows stop while working and empty but sends a typed queued message", async () => {
  const send = vi.fn(async () => true);
  const stop = vi.fn(async () => {});
  render(<Composer onSend={send} onStop={stop} working />);
  const composer = screen.getByRole<HTMLTextAreaElement>("textbox", {
    name: "Message agent",
  });
  const user = userEvent.setup();

  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Stop" }));
  expect(stop).toHaveBeenCalledOnce();
  expect(send).not.toHaveBeenCalled();

  await user.type(composer, "Queue this next");
  expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(send).toHaveBeenCalledWith("Queue this next");
  expect(stop).toHaveBeenCalledOnce();
});
