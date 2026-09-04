export function urlParam(name: string): string {
  return new URLSearchParams(window.location.search).get(name) || "";
}

export function setUrlParams(patch: Record<string, string | number | null | undefined>): void {
  const url = new URL(window.location.href);
  Object.entries(patch).forEach(([name, value]) => {
    if (value === null || value === undefined || value === "") url.searchParams.delete(name);
    else url.searchParams.set(name, String(value));
  });
  window.history.replaceState(window.history.state, "", url);
}
