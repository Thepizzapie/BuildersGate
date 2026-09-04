import { useMemo, useState } from "react";
import { Drawer, ScrollArea, TextInput } from "@mantine/core";
import { useAppState, type AssetGroup } from "../store";
import { Ti } from "./Ti";

const SPRITE = /\.(png|webp)$/i;
const AUDIO = /\.(wav|mp3|ogg|flac)$/i;
const MODEL = /\.(glb|gltf|obj)$/i;

function currentPath(group: AssetGroup): string {
  return group.approved?.path || group.candidates?.[0]?.path || group.revisions[group.revisions.length - 1]?.path || "";
}

export function AssetBrowser({ opened, onClose, onScreen }: {
  opened: boolean; onClose(): void; onScreen(id: string): void;
}) {
  const { asset_groups: groups } = useAppState();
  const [query, setQuery] = useState("");
  const rows = useMemo(() => groups.map((group) => ({ group, path: currentPath(group) }))
    .filter(({ group, path }) => `${group.logical_name} ${path}`.toLowerCase().includes(query.trim().toLowerCase())), [groups, query]);

  function open(path: string) {
    onClose();
    if (SPRITE.test(path)) { onScreen("spriteedit"); window.setTimeout(() => window.SpriteEdit?.open(path), 0); return; }
    if (AUDIO.test(path)) { onScreen("audiolab"); window.setTimeout(() => window.AudioLab?.open(path), 0); return; }
    if (MODEL.test(path)) { onScreen("modeledit"); window.setTimeout(() => window.ModelEdit?.open(path), 0); return; }
    onScreen("assets");
  }

  return <Drawer opened={opened} onClose={onClose} title="Asset workspace" size="min(420px, 92vw)" position="left"
                 classNames={{ content: "bg4-command", header: "bg4-command-head" }}>
    <TextInput data-autofocus value={query} onChange={(e) => setQuery(e.currentTarget.value)}
               leftSection={<Ti name="search" size={15} />} placeholder="Find an asset by name or path" />
    <ScrollArea.Autosize mah={440} mt="sm"><div className="bg4-asset-browser">
      {rows.map(({ group, path }) => <button key={group.logical_name} onClick={() => open(path)}>
        <Ti name={SPRITE.test(path) ? "photo" : AUDIO.test(path) ? "wave-sine" : MODEL.test(path) ? "box" : "file"} size={16} />
        <span><b>{group.logical_name}</b><small>{path || "No revision on disk"}</small></span>
        <span className="kind">{SPRITE.test(path) ? "Sprite" : AUDIO.test(path) ? "Audio" : MODEL.test(path) ? "3D" : "Asset"}</span>
      </button>)}
      {!rows.length && <div className="bg4-command-empty">No matching assets. Open the library to rescan the project.</div>}
    </div></ScrollArea.Autosize>
  </Drawer>;
}
