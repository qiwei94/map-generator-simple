# City flyover rendering

`tools/render_city_flyover.py` turns an existing draft GLB into a short Blender
flyover.  It edits only scene coordinates, camera, lighting, materials, and
render settings.  It does **not** change map meshes, global Z values, Boolean
operations, or the printable 3MF.

## Recommended workflow

First render three stills at low cost:

```powershell
& 'C:\Program Files\blender-4.0.1\blender.exe' `
  --background --python-exit-code 13 `
  --python tools\render_city_flyover.py -- `
  --input output\city\city_draft.glb `
  --output output\city\flyover.mp4 `
  --stills-dir output\city\flyover_stills `
  --blend-output output\city\flyover.blend `
  --resolution 1280x720 --duration 10 --samples 32 --preview-only
```

Inspect the opening, quarter, midpoint, three-quarter, and closing images before
rendering the movie.
Then remove `--preview-only`, use `--resolution 1920x1080`, and normally use
32–64 samples.  `--python-exit-code` is required because otherwise a Blender Python
exception can still leave the outer process with a misleading success code.

The script detects the thin model axis and normalizes imported maps to Blender
Z-up. Camera coordinates are derived from the actual model bounds.

The default is the reviewed v8 whole-city overview: a wide, oblique `coast`
traversal rather than a landmark orbit, with a 35 mm lens, a 30-degree downward
view angle, and a `2.90` camera-distance scale. Future GLB flyovers should keep
this camera grammar unless a scene-specific reviewed route explicitly replaces
it.
The five reviewed route controls are converted into 33 arc-length samples.
Camera and target use the same cubic progress, the camera moves at near-constant
speed, the downward pitch stays fixed, and Blender receives dense linear keys
instead of two independently overshooting Bezier animations. The intended
motion is a stable sightseeing helicopter, not an orbiting product shot.
It crosses almost the complete frame over the water and looks inland, keeping
both the city structure and building height legible. The default downward view
angle is 30 degrees from horizontal; adjust it deliberately with
`--view-pitch-deg 25..60`. Select the occupied frame edge with
`--coast-side east|west|north|south` and use `--reverse-route` when the opposite
travel direction reads better.

To compare another distance without changing the approved route bearing, view
pitch, or lens, use `--coast-distance-scale`. Values up to `3.5` keep the same
bearing and pitch rather than silently substituting a wider lens.

For a river bend, ring road, radial axis, valley, or another reviewed visual
divider, use a normalized path:

```powershell
--route-style path --route-points '0.08,0.22;0.28,0.35;0.52,0.48;0.74,0.68;0.93,0.82'
```

These points must come from reviewed vector/DesignSpec evidence. The renderer
does not invent geometry or infer a legal road/water source from image pixels.
The old downtown orbit remains available as `--route-style focus` with
`--focus-x-frac` and `--focus-y-frac` for explicit comparisons.

Whole-city structure is the default presentation goal: 35 mm lens, a 30-degree
oblique view, a `2.90` distance scale, and depth of field disabled. Use
`--depth-of-field` only for a deliberate close landmark shot.

The overview renderer uses matte deep blue-grey water, visibly distinct from
the neutral dark world background. `--water-preview-lift-mm` exists only for
depth-conflict diagnosis and defaults to zero. For a coast route, the Blender
scene adds a shared-vertex rectangular water apron around the crop. Its central
hole overlaps the model edge by one percent of the model span, which hides the
finite backing wall in oblique views without covering the city interior. The
apron is render-only and never changes the source GLB, printable 3MF, global
terrain Z mapping, or Boolean geometry.

At the approved `2.90` distance, the water apron must cover the complete camera
frustum for all route samples. A finite edge, black radial seam, or blue water
showing through the city is a render failure even when the source coastline is
correct. The apron uses one coplanar mesh, a conservative studio-scale reach,
and a bounded in-crop overlap. Always inspect all five stills before rendering
the movie.

On Blender 4.0 for Windows, automatic output extensions can append a frame
range and an unwanted container suffix.  The script keeps
`use_file_extension=False` and writes the exact requested MPEG-4/H.264 path.

## Chicago 25 km smooth-helicopter evidence (2026-08-27)

- Source GLB: `chicago_25km_height_roles_v19_draft.glb`
- Final movie: `chicago_25km_helicopter_smooth_v16_1080p.mp4`
- Windows path: `F:\map-generator-vault\artifacts\chicago_25km_height_roles_v19-1657e69\flyover_v4_structure\chicago_25km_helicopter_smooth_v16_1080p.mp4`
- Video request: MPEG-4/H.264, 1920×1080, 24 fps, 16.0 s, 384 frames
- Size: 21,868,057 bytes
- SHA-256: `b94f2a07f593aed69654682a81665e369a19bd477cfd0f9f07576e7c69b3336c`
- Camera: 35 mm, 30-degree downward pitch, `2.90` distance scale,
  33 arc-length-resampled motion keys, linear Blender interpolation
- Visual evidence: opening, quarter, midpoint, three-quarter, and closing
  1080p stills passed for sea continuity, city occlusion, framing, and crop-edge
  artifacts before the full render
- Render node: Windows, Blender 4.0.1, Radeon RX 6750 GRE 12 GB

This is a cinematic GLB review artifact, not print acceptance.  The source 3MF
and `design_spec.json` remain the authority for printable geometry.

The visual-role system used for future material, height, and road experiments
is documented in `doc/reference_city_demo_design_language.md`.

## Known limitation

The current GLB groups exact-height and visual-anchor buildings together as the
`landmarks` layer.  The flyover can give that group a restrained warm material,
but cannot highlight only a handful of named landmarks.  A future GLB contract
should export separate `identity_exact`, `visual_anchor_exact`, and background
building nodes when selective landmark lighting is required.
