# watermarks-remover (vendored)

Upstream: [guillaumemeyer/watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover) @ v0.4.0 (MIT).

| File | Source |
|------|--------|
| `text_unicode.py` | `skills/remove-ai-marks/scripts/text_unicode.py` |
| `wm_html.py` | `skills/remove-ai-marks/scripts/container_meta.py` (`clean_html` + helpers) |
| `wm_image.py` | `skills/remove-ai-marks/scripts/image_meta.py` (`strip_png` / `strip_jpeg`) |
| `LICENSE` | upstream MIT |

Layer A text/HTML scrub + PNG/JPEG C2PA/metadata strip. Layer B rewrite and CtrlRegen pixel removal are not used.
