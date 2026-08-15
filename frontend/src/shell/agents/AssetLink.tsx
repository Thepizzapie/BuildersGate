import { useCallback, useEffect, useState } from "react";
import { Menu, Loader, TextInput } from "@mantine/core";
import { Ti } from "../Ti";
import { readJSON } from "../../bridge";

/* LINK AN ASSET THE PROJECT ALREADY HAS.
 *
 * "make the new one read like the hub sign" is a sentence about a specific
 * file, and the only way to say which file was to type a path from memory and
 * hope it was right. The agent then guessed, or asked, or worked from the
 * wrong sprite -- and none of that is visible until the work comes back wrong.
 *
 * DELIBERATELY NOT AN UPLOAD. Nothing is copied, stored or attached: this
 * picks something already in the project and puts its path in the message. So
 * there is no upload endpoint, no size limit, no temp directory to clean up
 * and no second copy of a file that already has a home -- and the reference
 * stays correct when the asset is regenerated, because it is a path rather
 * than a snapshot.
 *
 * Search runs on the server (/api/assets/library takes `q`), because the
 * library is the whole project's art and filtering it in the browser means
 * shipping all of it to filter three rows.
 */

type Member = { rel: string; name?: string };
type Family = {
  key?: string; label: string; dir: string; category?: string;
  members?: Member[];
};

export type Linked = { rel: string; label: string };

const DEBOUNCE_MS = 220;
const SHOWN = 40;

export function AssetLink({ onPick }: { onPick: (asset: Linked) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [families, setFamilies] = useState<Family[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");

  const load = useCallback(async (needle: string) => {
    setLoading(true);
    const r = await readJSON<{ families?: Family[]; __error?: string }>(
      `/api/assets/library${needle ? `?q=${encodeURIComponent(needle)}` : ""}`,
      {});
    setLoading(false);
    setErr(r.__error || "");
    setFamilies(r.families || []);
  }, []);

  /* Debounced, and only while the menu is open. The library scan is not free,
     and firing it per keystroke behind a closed menu is work nobody asked for. */
  useEffect(() => {
    if (!open) return;
    const id = setTimeout(() => load(q), DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [open, q, load]);

  /* One row per FAMILY, not per file. A character is thirty sheets; listing
     every member turns a picker into a directory dump, and the family is what
     somebody means when they name an asset out loud. */
  const rows = families.slice(0, SHOWN);

  return (
    <Menu opened={open} onChange={setOpen} position="top-start" shadow="md"
          width={340} closeOnItemClick>
      <Menu.Target>
        <button className="bg4-tag" type="button" title="Link an asset from this project">
          <Ti name="photo" size={13} />
          link an asset
        </button>
      </Menu.Target>
      <Menu.Dropdown>
        <div style={{ padding: "6px 8px 8px" }}>
          <TextInput size="xs" placeholder="search the project's assets"
                     value={q} autoFocus
                     onChange={(e) => setQ(e.currentTarget.value)}
                     leftSection={<Ti name="search" size={13} />}
                     rightSection={loading ? <Loader size={12} /> : undefined}
                     /* Typing must not be read as menu navigation -- Mantine
                        moves focus to an item on a letter key otherwise, and
                        the field loses every character after the first. */
                     onKeyDown={(e) => e.stopPropagation()} />
        </div>
        {err && <Menu.Item disabled>could not read the library — {err}</Menu.Item>}
        {!err && !rows.length && (
          <Menu.Item disabled>
            {loading ? "looking…" : q ? "nothing matches" : "no assets in this project yet"}
          </Menu.Item>
        )}
        {rows.map((fam) => {
          /* The family's own directory is the reference: it is stable across a
             regenerate, which an individual sheet's filename is not. Falls back
             to the first member for a loose file that has no family dir. */
          const rel = fam.dir || fam.members?.[0]?.rel || "";
          if (!rel) return null;
          return (
            <Menu.Item key={fam.key || rel}
                       onClick={() => onPick({ rel, label: fam.label || rel })}
                       leftSection={<Ti name="photo" size={14} />}
                       rightSection={fam.members?.length
                         ? <span className="bg4-menun">{fam.members.length}</span>
                         : undefined}>
              {fam.label || rel}
              <span className="bg4-tag-sub">{rel}</span>
            </Menu.Item>
          );
        })}
        {families.length > SHOWN && (
          <Menu.Item disabled>
            {families.length - SHOWN} more — narrow the search
          </Menu.Item>
        )}
      </Menu.Dropdown>
    </Menu>
  );
}
