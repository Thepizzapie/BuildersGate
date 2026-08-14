import { createTheme, MantineProvider, type MantineColorsTuple } from "@mantine/core";
import { useEffect, useState, type ReactNode } from "react";

/* Mantine, wearing this dashboard's clothes.
 *
 * THE LAYERED STYLESHEET, NOT THE PLAIN ONE. `@mantine/core/styles.layer.css`
 * wraps every rule in `@layer mantine`, and app.css declares the order. Mantine
 * sits AFTER `base` and `layout` — so its components beat the generic element
 * resets this project applies to `button`, `input` and `table`, which is the
 * difference between a component library and a library-shaped pile of unstyled
 * elements — and BEFORE `components`, so a rule this project wrote for a class
 * of its own still wins where the two deliberately meet.
 *
 * THE PALETTE IS NOT MANTINE'S. Every colour below resolves to a var() this
 * project already defines, so a Mantine control inherits the ember-on-black
 * ground, follows the theme toggle, and follows a future retheme without being
 * touched. A library themed with its own defaults next to 4000 lines of bespoke
 * CSS reads as two applications in one window.
 */

// Mantine wants ten shades. The app has one accent and its hover, so the tuple
// is that pair: a control never picks a shade this project has not chosen.
const ember = Array.from({ length: 10 }, (_, i) =>
  i < 6 ? "var(--accent)" : "var(--accent-hover)") as unknown as MantineColorsTuple;

export const theme = createTheme({
  colors: { ember },
  primaryColor: "ember",
  primaryShade: 5,
  fontFamily: "var(--sans)",
  fontFamilyMonospace: "var(--mono)",
  defaultRadius: "sm",
  radius: { xs: "3px", sm: "5px", md: "8px", lg: "12px", xl: "16px" },
  /* Mantine's own surface and text variables, pointed at this project's. Done
     here rather than as props at each call site, so a component added later
     cannot forget and land on Mantine's grey. */
  other: {},
  components: {
    Paper: { defaultProps: { bg: "var(--solid-2)" } },
  },
});

/* Mantine's variables are emitted against this selector; the values are the
   app's tokens, so both stylesheets move together. Written as a plain <style>
   rather than through the theme object because Mantine only generates the
   variables it knows about, and the surface/line/text set below is what its
   components actually read. */
/* THE BRIDGE MOVED TO shell.css, and the move is the fix.
   Every island mounts its own MantineProvider, and each provider injects its
   own CSS-variables <style> into the head. With seven islands that is seven
   unlayered blocks at the same specificity as this one was, so WHICHEVER
   MOUNTED LAST WON — the ground looked right in one session and shipped a
   white dropdown on a black page in another, from identical code. The values
   live in one stylesheet now, at doubled `:root:root` specificity, so no
   amount of provider mounting can outrank them. */


/** Which ground the page is on, in the two words Mantine understands.
 *
 *  The page has four settings — dark, light, system and orbit — and Mantine has
 *  two. Orbit is glass on vanta black, so it is dark; system is whatever the OS
 *  says right now. Kept in sync rather than fixed, because a Mantine component
 *  that stays dark on a light page is exactly the seam this bridge exists to
 *  prevent. */
function usePageScheme(): "light" | "dark" {
  const read = (): "light" | "dark" => {
    const set = document.documentElement.dataset.theme;
    if (set === "light") return "light";
    if (set) return "dark";                       // dark, orbit, anything later
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  };
  const [scheme, setScheme] = useState(read);
  useEffect(() => {
    const sync = () => setScheme(read());
    // setTheme() in index.html fires this on every change, including "system".
    window.addEventListener("bgate:theme", sync);
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    mq.addEventListener("change", sync);
    return () => {
      window.removeEventListener("bgate:theme", sync);
      mq.removeEventListener("change", sync);
    };
  }, []);
  return scheme;
}

/* Every island is wrapped, not the page: there is no single React root to hang
   a provider from, and there must not be one — the shell owns the document. */
export function Themed({ children }: { children: ReactNode }) {
  const scheme = usePageScheme();
  return (
    <MantineProvider theme={theme} forceColorScheme={scheme}>
      {children}
    </MantineProvider>
  );
}
