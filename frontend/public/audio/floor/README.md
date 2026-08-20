# The studio floor's soundtrack

Empty on purpose. Nothing here is committed until somebody runs the generator,
because these are generated files with a real cost attached and an empty folder
is an honest statement that nobody has paid it yet.

```bash
python scripts/gen_floor_music.py --dry-run   # briefs and balance, spends nothing
python scripts/gen_floor_music.py             # generates, downloads, writes manifest.json
cd frontend && npm run build                  # copies them into bgate_ui/static
```

The floor reads `manifest.json` from this folder. No manifest means no
soundtrack, and the radio in the lounge stays scenery rather than becoming a
switch that does nothing.

**These are harness UI audio, not game assets.** That is why they live here
beside the floor's sprites rather than going through `music_generate` ->
`music_keep` -> `music_install`, which files takes as artifact revisions and
installs the kept one into the **engine project's** music library. A game
shipping the dashboard's background music in its own audio folder is the bug
that path would have introduced.

To drop a track: delete the mp3 and run `gen_floor_music.py --manifest-only`.
The manifest is rebuilt from what is actually on disk, so it can never name a
file that is not there.
