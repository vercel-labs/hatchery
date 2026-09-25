import {
  useRef,
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
} from "react";

type ElementRef = RefObject<HTMLElement | null>;

export function ResizeHandle({
  containerRef,
  paneRef,
  variable,
  direction,
  label,
  minimum,
  maximum,
  minimumContent,
  defaultValue,
  orientation = "vertical",
  className = "",
}: {
  containerRef: ElementRef;
  paneRef: ElementRef;
  variable: `--${string}`;
  direction: 1 | -1;
  label: string;
  minimum: number;
  maximum: number;
  minimumContent: number;
  defaultValue: number;
  orientation?: "horizontal" | "vertical";
  className?: string;
}) {
  const handleRef = useRef<HTMLDivElement>(null);
  const value = useRef<number | null>(null);
  const drag = useRef<{
    pointerId: number;
    startPosition: number;
    startSize: number;
  } | null>(null);
  const horizontal = orientation === "horizontal";

  function currentSize() {
    const bounds = paneRef.current?.getBoundingClientRect();
    return (
      value.current ??
      (horizontal ? bounds?.height : bounds?.width) ??
      defaultValue
    );
  }

  function resize(size: number) {
    const bounds = containerRef.current?.getBoundingClientRect();
    const containerSize = horizontal ? bounds?.height : bounds?.width;
    const available =
      containerSize === undefined ? maximum : containerSize - minimumContent;
    const upper = Math.max(minimum, Math.min(maximum, available));
    const next = Math.round(Math.min(upper, Math.max(minimum, size)));
    value.current = next;
    containerRef.current?.style.setProperty(variable, `${next}px`);
    handleRef.current?.setAttribute("aria-valuenow", String(next));
    handleRef.current?.setAttribute("aria-valuetext", `${next} pixels`);
  }

  function keyDown(event: KeyboardEvent<HTMLDivElement>) {
    let size: number | null = null;
    if (!horizontal && event.key === "ArrowLeft")
      size = currentSize() - 16 * direction;
    if (!horizontal && event.key === "ArrowRight")
      size = currentSize() + 16 * direction;
    if (horizontal && event.key === "ArrowUp")
      size = currentSize() - 16 * direction;
    if (horizontal && event.key === "ArrowDown")
      size = currentSize() + 16 * direction;
    if (event.key === "Home") size = minimum;
    if (event.key === "End") size = maximum;
    if (size === null) return;
    event.preventDefault();
    resize(size);
  }

  function pointerDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    drag.current = {
      pointerId: event.pointerId,
      startPosition: horizontal ? event.clientY : event.clientX,
      startSize: currentSize(),
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function pointerMove(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (!active || active.pointerId !== event.pointerId) return;
    const position = horizontal ? event.clientY : event.clientX;
    resize(active.startSize + (position - active.startPosition) * direction);
  }

  function pointerUp(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  return (
    <div
      ref={handleRef}
      role="separator"
      tabIndex={0}
      aria-label={label}
      aria-orientation={orientation}
      aria-valuemin={minimum}
      aria-valuemax={maximum}
      aria-valuenow={defaultValue}
      aria-valuetext={`${defaultValue} pixels`}
      className={`group relative z-10 touch-none bg-border outline-none before:absolute before:transition-colors hover:before:bg-ring/30 focus-visible:before:bg-ring/50 active:before:bg-ring/50 ${horizontal ? "h-px cursor-row-resize before:inset-x-0 before:-top-1 before:-bottom-1" : "w-px cursor-col-resize before:inset-y-0 before:-right-1 before:-left-1"} ${className}`}
      onKeyDown={keyDown}
      onPointerDown={pointerDown}
      onPointerMove={pointerMove}
      onPointerUp={pointerUp}
      onLostPointerCapture={() => {
        drag.current = null;
      }}
    />
  );
}
