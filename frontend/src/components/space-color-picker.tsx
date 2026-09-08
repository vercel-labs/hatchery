"use client";

import { CheckIcon } from "lucide-react";

import {
  ACCENT_COLORS,
  type AccentColor,
  resolveSpaceColor,
} from "@/lib/space-colors";
import { cn } from "@/lib/utils";
import {
  ToggleGroup,
  ToggleGroupItem,
} from "@/components/ui/toggle-group";

export function SpaceColorPicker({
  value,
  onValueChange,
  label = "Accent color",
}: {
  value: AccentColor | null;
  onValueChange: (value: AccentColor | null) => void;
  label?: string;
}) {
  return (
    <ToggleGroup
      aria-label={label}
      value={value ? [value] : []}
      onValueChange={(colors) =>
        onValueChange((colors.at(-1) as AccentColor | undefined) ?? null)
      }
      className="flex-wrap"
    >
      {ACCENT_COLORS.map((color) => {
        const selected = value === color;
        const name = color[0].toUpperCase() + color.slice(1);
        return (
          <ToggleGroupItem
            key={color}
            value={color}
            aria-label={`${name} accent`}
            title={name}
            className={cn(
              "size-8 rounded-full p-0",
              selected && "ring-2 ring-ring ring-offset-2 ring-offset-background",
            )}
          >
            <span
              className="flex size-5 items-center justify-center rounded-full text-white"
              style={{ backgroundColor: resolveSpaceColor(color) }}
            >
              {selected && <CheckIcon aria-hidden="true" />}
            </span>
          </ToggleGroupItem>
        );
      })}
    </ToggleGroup>
  );
}
