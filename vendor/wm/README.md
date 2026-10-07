# watermarks-remover (vendored)

Upstream: [guillaumemeyer/watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover) @ v0.7.0 (MIT).

| File | Source |
|------|--------|
| `text_unicode.py` | `service/scripts/text_unicode.py` |
| `wm_html.py` | `service/scripts/container_meta.py` (`clean_html` + helpers; embedded data-URI image scrub omitted) |
| `wm_image.py` | `service/scripts/image_meta.py` (`strip_png` / `strip_jpeg` + helpers; `clean_image_bytes` local adapter) |
| `LICENSE` | upstream MIT |

Layer A text/HTML scrub + PNG/JPEG C2PA/metadata strip. Layer B rewrite, CtrlRegen pixel removal, and container formats beyond HTML/PNG/JPEG are not used.

Regenerate: `python scripts/vendor_watermarks_remover.py`. CI opens a weekly PR via `.github/workflows/vendor-watermarks-remover.yml`.
