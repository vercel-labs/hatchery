const formatter = new Intl.NumberFormat("en", { notation: "compact" });
export const number = (value: number) => formatter.format(value);
export const reasonMessage = (reason: unknown) =>
  reason instanceof Error ? reason.message : "Request failed";
