/* A Tabler glyph.
 *
 * The repo's own BGIcon set is 24-grid geometry drawn for the RAIL — eleven
 * destinations and a dozen actions. This shell needs ~50 more (a sword, a
 * backpack, a plug, a clapperboard), and drawing fifty new icons to that
 * standard is a week nobody asked for. Tabler is vendored locally for the same
 * reason IBM Plex is: this ships as a loopback app and an .exe, and a CDN link
 * is an icon that is simply absent offline.
 *
 * BGIcon still owns the MARK and the classic views' icons. Nothing was deleted;
 * the two sets sit side by side, and the one place they meet is the logo in the
 * rail, which stays BGIcon.logo because a theme may restyle the UI, not the
 * brand.
 */
export function Ti({ name, size, color, className }: {
  name: string; size?: number; color?: string; className?: string;
}) {
  return (
    <i className={`ti ti-${name}${className ? " " + className : ""}`}
       style={{ fontSize: size ? `${size}px` : undefined, lineHeight: 1, color }}
       aria-hidden="true" />
  );
}
