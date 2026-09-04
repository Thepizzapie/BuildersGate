import { useEffect, useState } from "react";
import { Ti } from "./Ti";

export type Ground = {
  id: "dark" | "light" | "system" | "orbit" | "blueprint" | "pocket" | "candy" | "mythic" | "flux";
  icon: string;
  label: string;
  note: string;
};

export const GROUNDS: Ground[] = [
  { id: "dark", icon: "moon", label: "Dark", note: "Low-glare charcoal and ember" },
  { id: "light", icon: "sun", label: "Light", note: "Cool paper and deep ink" },
  { id: "orbit", icon: "planet", label: "Orbit", note: "Vanta glass and spectral edges" },
  { id: "blueprint", icon: "ruler-2", label: "Blueprint", note: "Drafting blue and safety yellow" },
  { id: "pocket", icon: "device-gamepad-2", label: "Pocket", note: "Four-tone LCD and raspberry controls" },
  { id: "candy", icon: "device-gamepad", label: "Candy cab", note: "Pearl shell and cobalt arcade controls" },
  { id: "mythic", icon: "swords", label: "Mythic", note: "Obsidian, spellfire and gilded relics" },
  { id: "flux", icon: "activity", label: "Flux", note: "Builders Gate blue and orange, alive to time and focus" },
  { id: "system", icon: "device-desktop", label: "System", note: "Follow this device" },
];

export function readGround(): Ground["id"] {
  try {
    const saved = localStorage.getItem("bgate-theme");
    return GROUNDS.some((g) => g.id === saved) ? saved as Ground["id"] : "system";
  } catch { return "system"; }
}

export function useGround(): [Ground["id"], (id: Ground["id"]) => void] {
  const [mode, setMode] = useState<Ground["id"]>(readGround);
  useEffect(() => {
    const sync = (event: Event) => {
      const next = (event as CustomEvent<{ mode?: string }>).detail?.mode;
      setMode(GROUNDS.some((g) => g.id === next) ? next as Ground["id"] : readGround());
    };
    window.addEventListener("bgate:theme", sync);
    return () => window.removeEventListener("bgate:theme", sync);
  }, []);
  const choose = (id: Ground["id"]) => { window.setTheme?.(id); setMode(id); };
  return [mode, choose];
}

export function ThemeSample({ ground, compact = false }: { ground: Ground; compact?: boolean }) {
  const moveFlux = (event: React.PointerEvent<HTMLSpanElement>) => {
    if (ground.id !== "flux") return;
    const rect = event.currentTarget.getBoundingClientRect();
    event.currentTarget.style.setProperty("--flux-preview-x", `${event.clientX - rect.left}px`);
    event.currentTarget.style.setProperty("--flux-preview-y", `${event.clientY - rect.top}px`);
  };
  return (
    <span className={`bg4-theme-sample${compact ? " compact" : ""}`} data-ground={ground.id}
          aria-hidden="true" onPointerMove={moveFlux}>
      <span className="rail"><i /><i /><i /></span>
      <span className="stage"><i className="head" /><i className="card"><b /><b /></i></span>
      {ground.id === "flux" && <span className="flux-live">
        <i className="flux-focus" />
        <svg viewBox="0 0 100 100" preserveAspectRatio="none">
          <path className="route" d="M7 74 C30 74 31 25 54 25 S72 66 94 48" />
          <path className="pulse" d="M7 74 C30 74 31 25 54 25 S72 66 94 48" />
          <circle cx="7" cy="74" r="3" /><circle cx="54" cy="25" r="3" /><circle cx="94" cy="48" r="3" />
        </svg>
      </span>}
    </span>
  );
}

export function ThemeGrid() {
  const [mode, choose] = useGround();
  return (
    <div className="bg4-theme-grid" role="radiogroup" aria-label="Colour theme">
      {GROUNDS.map((ground) => (
        <button key={ground.id} type="button" role="radio" data-ground={ground.id} aria-checked={mode === ground.id}
                className={mode === ground.id ? "on" : ""} onClick={() => choose(ground.id)}>
          <ThemeSample ground={ground} />
          <span className="copy"><span className="name"><Ti name={ground.icon} size={14} />{ground.label}</span>
            <span className="note">{ground.note}</span></span>
          <span className="check">{mode === ground.id && <Ti name="check" size={13} />}</span>
        </button>
      ))}
    </div>
  );
}
