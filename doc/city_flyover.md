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

Inspect the opening, midpoint, and closing images before rendering the movie.
Then remove `--preview-only`, use `--resolution 1920x1080`, and normally use 64
samples.  `--python-exit-code` is required because otherwise a Blender Python
exception can still leave the outer process with a misleading success code.

The script detects the thin model axis and normalizes imported maps to Blender
Z-up.  Camera coordinates are derived from the actual model bounds.  The
default focus fractions (`0.762`, `0.522`) place the visual centre on downtown
Chicago and its river; other cities should pass reviewed focus fractions rather
than reusing Chicago's crop blindly.

On Blender 4.0 for Windows, automatic output extensions can append a frame
range and an unwanted container suffix.  The script keeps
`use_file_extension=False` and writes the exact requested MPEG-4/H.264 path.

## Chicago 25 km evidence (2026-08-26)

- Source GLB: `chicago_25km_height_roles_v19_draft.glb`
- Final movie: `chicago_25km_flyover_1080p.mp4`
- Video: H.264/`avc1`, 1920×1080, 24 fps, 12.0 s, 288 frames
- Size: 10,535,949 bytes
- SHA-256: `dbc539a091bc4b1e6e3096de5e3f80df1d1021c7dae971ca78a3272beefcac60`
- Compatibility: decoded successfully with macOS AVFoundation; frames checked
  at 0.5, 3, 6, 9, and 11 seconds
- Render node: Windows, Blender 4.0.1, Radeon RX 6750 GRE 12 GB

This is a cinematic GLB review artifact, not print acceptance.  The source 3MF
and `design_spec.json` remain the authority for printable geometry.

## Known limitation

The current GLB groups exact-height and visual-anchor buildings together as the
`landmarks` layer.  The flyover can give that group a restrained warm material,
but cannot highlight only a handful of named landmarks.  A future GLB contract
should export separate `identity_exact`, `visual_anchor_exact`, and background
building nodes when selective landmark lighting is required.
