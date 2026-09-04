export type UtilityName = "command" | "assets" | "readiness" | "inspector" | "notifications" | "orchestration";

const EVENT = "bgate:utility";

export function claimUtility(name: UtilityName): void {
  window.dispatchEvent(new CustomEvent(EVENT, { detail: { name } }));
}

export function onUtility(fn: (name: UtilityName) => void): () => void {
  const handler = (event: Event) => fn((event as CustomEvent<{ name: UtilityName }>).detail.name);
  window.addEventListener(EVENT, handler);
  return () => window.removeEventListener(EVENT, handler);
}
