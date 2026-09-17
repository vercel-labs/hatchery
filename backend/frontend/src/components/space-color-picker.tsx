"use client";

import { CheckIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  ACCENT_FAMILIES,
  ACCENT_SHADES,
  accentColor,
  type AccentColor,
  resolveSpaceColor,
  resolveSpaceForeground,
} from "@/lib/space-colors";
import { cn } from "@/lib/utils";

export function SpaceColorPicker({
  value,
  onValueChange,
  label = "Accent color",
  allowUnselected = false,
}: {
  value: AccentColor | null;
  onValueChange: (value: AccentColor | null) => void;
  label?: string;
  allowUnselected?: boolean;
}) {
  return (
    <div role="group" aria-label={label} className="grid w-fit grid-cols-4 gap-2">
      {ACCENT_FAMILIES.map((family) => {
        const name = family[0].toUpperCase() + family.slice(1);
        return ACCENT_SHADES.map((shade) => {
          const color = accentColor(family, shade);
          const selected = value === color;
          return (
            <Button
              key={color}
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={`${name} ${shade}`}
              aria-pressed={selected}
              title={`${name} ${shade}`}
              className={cn(
                "size-7 rounded-full p-0",
                selected &&
                  "ring-2 ring-ring ring-offset-2 ring-offset-background",
              )}
              style={{
                backgroundColor: resolveSpaceColor(color),
                color: resolveSpaceForeground(family),
              }}
              onClick={() => {
                if (selected && allowUnselected) onValueChange(null);
                else onValueChange(color);
              }}
            >
              {selected && (
                <CheckIcon data-icon="inline-start" aria-hidden="true" />
              )}
            </Button>
          );
        });
      })}
    </div>
  );
}
