import type { ConsoleState, Item, Question } from "./api";

export const DISMISSED_ATTENTION_STORAGE = "bgate.dismissed-attention";

export const itemAttentionKey = (item: Item) =>
  `item:${item.id}:${item.status}:${item.execution_state || ""}:${item.updated_at || ""}`;
export const gateAttentionKey = (gate: ConsoleState["gates"][number]) => `gate:${gate.id}`;
export const questionAttentionKey = (question: Question) => `question:${question.id}`;

export function readDismissedAttention(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const value = JSON.parse(window.localStorage.getItem(DISMISSED_ATTENTION_STORAGE) || "[]");
    return Array.isArray(value) ? value.filter((key): key is string => typeof key === "string") : [];
  } catch {
    return [];
  }
}

