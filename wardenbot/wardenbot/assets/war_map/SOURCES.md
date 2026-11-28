# War map assets

- `background.webp`: composite of zoom-4 map tiles from
  https://github.com/Kastow/Foxhole-Map-Tiles at
  `08f9f6c842aec141f4f44c2d8049961c1df9d748` (Update 66).
  The upstream README states that the images belong to Clapfoot / Siege Camp
  and are used with permission. Credits: Siege Camp, Kastow, NoUDerp, Sentsu.
  Source tiles: `Tiles/4/4_{x}_{y}.png`, x and y in 0..15, 256 px each.
  They are stitched as a 4096×4096 canvas and encoded as WebP quality 88.
- `regions.json`: factual names and grid locations of the 53 regions, checked
  against the above map and the public region table in
  https://github.com/Saniee/FoxholeWarBot/blob/f1dc4b51921e2bce58b01632d26655c710c20f31/src/utils/regions.rs
  (no renderer or other application code copied).
  Coordinates are columns 0..12, rows 0..6; odd columns are offset down half a hex.
- `DejaVuSans.ttf`: DejaVu Sans, bundled for Cyrillic text in slim Docker images.
  Font license: `FONT-LICENSE.txt`.
- Live data: https://github.com/clapfoot/warapi — only the documented public API.

The renderer draws ownership markers, not an inferred frontline or unit positions.
Background art is versioned with the bot. Unknown future regions remain available
as coordinate diagrams in the region menu instead of receiving invented locations.
