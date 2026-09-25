import { fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { expect, it, vi } from "vitest";
import { ResizeHandle } from "@/components/resize-handle";

function Harness({
  direction,
  label,
  variable,
  orientation = "vertical",
}: {
  direction: 1 | -1;
  label: string;
  variable: `--${string}`;
  orientation?: "horizontal" | "vertical";
}) {
  const container = useRef<HTMLDivElement>(null);
  const pane = useRef<HTMLElement>(null);
  return (
    <div ref={container} data-testid={`${label} container`}>
      <aside ref={pane} data-testid={`${label} pane`} />
      <ResizeHandle
        containerRef={container}
        paneRef={pane}
        variable={variable}
        direction={direction}
        label={label}
        minimum={200}
        maximum={700}
        minimumContent={300}
        defaultValue={400}
        orientation={orientation}
      />
    </div>
  );
}

function dimensions(element: HTMLElement, width: number, height = 0) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
    width,
    height,
  } as DOMRect);
}

it("resizes left- and right-anchored panes with arrow keys", () => {
  const left = render(
    <Harness direction={1} label="Resize navigation" variable="--navigation" />,
  );
  dimensions(screen.getByTestId("Resize navigation container"), 1_000);
  dimensions(screen.getByTestId("Resize navigation pane"), 400);
  const navigation = screen.getByRole("separator", {
    name: "Resize navigation",
  });
  fireEvent.keyDown(navigation, { key: "ArrowRight" });
  expect(
    (left.container.firstElementChild as HTMLElement).style.getPropertyValue(
      "--navigation",
    ),
  ).toBe("416px");
  expect(navigation.getAttribute("aria-valuenow")).toBe("416");
  left.unmount();

  const right = render(
    <Harness direction={-1} label="Resize changes" variable="--changes" />,
  );
  dimensions(screen.getByTestId("Resize changes container"), 1_000);
  dimensions(screen.getByTestId("Resize changes pane"), 400);
  const changes = screen.getByRole("separator", { name: "Resize changes" });
  fireEvent.keyDown(changes, { key: "ArrowLeft" });
  expect(
    (right.container.firstElementChild as HTMLElement).style.getPropertyValue(
      "--changes",
    ),
  ).toBe("416px");
  expect(changes.getAttribute("aria-valuenow")).toBe("416");
});

it("resizes stacked panes by dragging vertically and with up and down keys", () => {
  const view = render(
    <Harness
      direction={1}
      label="Resize workspace tree"
      variable="--workspace-tree"
      orientation="horizontal"
    />,
  );
  dimensions(screen.getByTestId("Resize workspace tree container"), 500, 800);
  dimensions(screen.getByTestId("Resize workspace tree pane"), 500, 300);
  const handle = screen.getByRole("separator", {
    name: "Resize workspace tree",
  });
  Object.assign(handle, {
    setPointerCapture: vi.fn(),
    hasPointerCapture: vi.fn(() => true),
    releasePointerCapture: vi.fn(),
  });

  fireEvent.pointerDown(handle, { button: 0, pointerId: 7, clientY: 300 });
  fireEvent.pointerMove(handle, { pointerId: 7, clientY: 360 });
  expect(
    (view.container.firstElementChild as HTMLElement).style.getPropertyValue(
      "--workspace-tree",
    ),
  ).toBe("360px");
  expect(handle.getAttribute("aria-orientation")).toBe("horizontal");

  fireEvent.keyDown(handle, { key: "ArrowUp" });
  expect(
    (view.container.firstElementChild as HTMLElement).style.getPropertyValue(
      "--workspace-tree",
    ),
  ).toBe("344px");
});
