# 2026-08-23 25 km showcase repair results

This note records the repair and real-data reruns performed after the first
distributed 25 km batch exposed false-success water masks, empty Cairo output,
stale layer caches and weak city-identity ranking.  A process exit is not used
as acceptance evidence: every accepted city must have physical-area, feature,
image-content and multi-style checks.

## Branch and repair slices

- Branch: `agent/distributed-25km-showcase`
- `a0e0d3d`: pass negative southern bboxes as one CLI token, scale portable
  osmium export timeouts, ignore tiny closed coastline rings, and reject empty,
  implausibly wet or identical-style galleries.
- `6c30af9`: add deterministic river-bend, ring, radial, grid, relief and urban
  core signature analysis.  The analysis ranks/validates a frame; it does not
  control mesh vertices, global Z values or boolean operations.
- `3836e89`: line-buffer long gallery logs so a healthy GEOS stage does not
  look hung.
- `3bcb3c5`: bump the preprocess cache schema after the water-geometry repair.
  This prevents a corrected GDF profile from reusing an old full-frame water
  layer cache.
- `51d0c87`: classify road-dense, populated, low-water terrain presets as urban
  instead of treating a terrain hint as authoritative.
- `3f23843`: render water pure black in every scene type.  Landscape/ocean
  reviews no longer use a separate graphite color.
- `51ef6ca`: reuse Windows host CJK fonts from WSL and known macOS/Linux font
  paths so contact-sheet labels do not become square glyphs.
- `a82dab6`: require measured frame-scale water evidence before awarding a
  water city signature; tiny ponds no longer label a dry city as water-led.

## Automated verification

Formal non-slow command:

```bash
.venv/bin/pytest -q tests -m 'not slow'
```

Result after the final scoring change:

```text
363 passed, 2 skipped, 11 deselected, 22 warnings in 4.56s
```

The warnings are the existing LibreSSL, matplotlib/pyparsing and Pillow mode
deprecation warnings.  The focused water test inspects the center pixel of a
real rasterized landscape water polygon and requires exact `(0, 0, 0)`.

## Real rerun evidence

### Cairo, 25 km

Command path: `tools/generate_showcase_samples.py` ->
`tools/gen_area_gallery.py`, using `pbf_cache/egypt-latest.osm.pbf` and native
osmium 1.19.1 on the controller Mac.

- Requested bbox: `29.9313535,31.1059819,30.1574465,31.3654181`
- Measured area: `626.78 km2`
- Raw extracted features: 114,904 buildings, 158,621 roads and 1,191 water
  features.
- Profile: 83.41 buildings/km2, 24.01 road-km/km2, 5.12% water.
- Framing: river-bend `0.9594`, radial `0.3974`, ring `0.3425`; 25 km selected
  because it preserves the Nile bend and two-bank city relationship.
- Dense-detail printable review layers: BL 209, BO 2,870, WL 42, WO 20,
  block base 12,239 and road lines 4,733.
- Four styles completed in `1488.6s`; project validator reported
  `verified cairo` and the batch recorded no failure.
- Human selection: `dense_detail` (`3.669`) is materially better than baseline
  (`2.729`), block fill (`2.823`) and minimal (`2.675`).  It keeps the Nile as
  the dominant black axis while reducing road dominance enough for the eastern
  urban network to remain legible.

The optional large-water relation cache hit GDAL's oversized-GeoJSON object
limit.  This was not treated as success evidence: standard water extraction
still returned 1,191 features, the measured water ratio was 5.12%, the review
mask was 4.71%, and the Nile is visibly continuous with changing width.

### Cape Town, 25 km

Windows WSL reran all four styles after the water/color and font fixes.

- GDF cache: real South Africa PBF; profile water 44.6%, buildings 249.3/km2.
- Runtime: `231.9s`, `4/4 styles OK`, `verified cape_town`.
- The 2048x2048 block-fill PNG contains 1,854,958 exact black pixels, or
  `44.23%` of the image, consistent with the measured water coverage.
- The ocean is now pure black and Chinese contact-sheet labels render normally.
- The Table Mountain, bay and port composition remained intact.

### Berlin and Madrid, 25 km

- Berlin: measured 629.48 km2, 604.89 buildings/km2 and 34.39 road-km/km2.
  All four styles passed; dense detail scored `6.873` and had a 3.55% road
  pixel ratio.  It is the best of the four, but small black water/noise marks
  and still-heavy arterial lines keep it out of the homepage shortlist.
- Madrid: measured 628.03 km2, 273.68 buildings/km2 and 27.69 road-km/km2.
  All four styles passed in `1274.5s`.  Human review prefers dense detail even
  though the old aggregate metric slightly favored minimal; minimal's 13.47%
  road-pixel ratio is visibly overbearing.  This remains a style-ranking
  calibration issue, not a data-completeness failure.

### Windows repair batch

Sydney, Melbourne, Buenos Aires and Cape Town passed the strict gallery
validator.  Mexico City initially exposed two separate false-success paths:
tiny inland coastline rings flooded the frame, and then a corrected GDF reused
the stale all-water preprocess cache.  After both fixes it rendered real roads,
buildings and water.  Melbourne and Buenos Aires are viable showcase samples;
Mexico City still needs visual tuning before homepage use.

### Cairo cross-platform parity

The corrected Cairo dense-detail frame was independently recomputed in Windows
WSL from the same verified Egypt PBF, after confirming that no other Windows
gallery task was running.

- Scheduled task exit code: `0`; no `gen_area_gallery.py` process remained.
- Raw extraction matched the controller exactly: 114,904 buildings, 158,621
  roads and 1,191 water features.
- Printable layers were effectively equivalent: BL 209, BO 2,863, WL 42,
  WO 20, block base 12,239 and road lines 4,733.  The seven-BO difference from
  the controller result is a cross-version GEOS boundary decision, not missing
  source data.
- Output size matched at 2048x2048.  Against the controller PNG, 1.2213% of
  pixels differed; normalized mean absolute error was only 0.05434% of the
  channel range.  Visual inspection confirmed the same Nile bend, two-bank
  composition, road hierarchy and pure-black water.
- WSL completed the dense style in `160.9s`, but total wall time was `817.8s`
  because first-use preparation and the unsuccessful optional large-water
  relation lookup consumed most of the run.  This is evidence for cache and
  preview-path work, not a general Windows-versus-Mac performance conclusion.

## Performance finding

Do not generalize these numbers into a claim about all 16 GB Macs or Linux
workers.  For these real 25 km frames, the controller's dominant hot path was
single-core GEOS block-base construction and overlay, not osmium extraction:

- Cairo baseline preprocess: `434.6s`; block base alone `212.3s`.
- Cairo minimal preprocess: `48.7s`; block base `31.1s` because the road-tier-2
  partition was much smaller.
- Native Cairo building/road/water extraction completed in seconds per layer.

The next fast-preview performance slice should use the selected center 5 km,
skip or reuse block-base geometry, and avoid printability work that is not
needed for a visual preview.  The formal 15/25 km model path should retain its
full geometry and validation gates.

## Known limitations and next actions

- Style-level scoring still does not fully penalize a visually dominant road
  network.  City signature scoring correctly captures frame identity, but
  homepage promotion still requires human inspection.
- Recompute old landscape/ocean images to pick up pure-black water; changing
  code does not rewrite existing PNGs.
- The optional Overture CLI is absent on the current Mac and WSL environments.
  Local PBF extraction is real and non-zero, so Overture failure is not masking
  empty output.
- The optional large-water relation path can hit GDAL's oversized GeoJSON
  object limit.  Current acceptance therefore requires the standard extraction
  counts, measured water coverage and rendered mask to agree; the optional
  lookup is not trusted as a success signal.
- Keep `cloud-data` as storage and `cloud-api` as API/queue/low-priority worker;
  neither was restarted or used for this heavy repair batch.
- Runtime gallery outputs are intentionally not committed.  The code, tests
  and this evidence record are committed on the working branch.
