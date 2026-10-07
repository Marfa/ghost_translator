#!/usr/bin/env python3
"""Refresh vendor/wm from guillaumemeyer/watermarks-remover (Layer A only).

Stdlib only. Fetches a release tag (default: latest) and rebuilds:

  text_unicode.py  ← service/scripts/text_unicode.py
  wm_html.py       ← clean_html + helpers from container_meta.py
                     (embedded data-URI image scrub omitted)
  wm_image.py      ← strip_png / strip_jpeg + helpers; local clean_image_bytes
  LICENSE / README / VERSION
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

UPSTREAM = "guillaumemeyer/watermarks-remover"
API = f"https://api.github.com/repos/{UPSTREAM}"
RAW = f"https://raw.githubusercontent.com/{UPSTREAM}"

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "vendor" / "wm"
VERSION_FILE = OUT / "VERSION"


def _http_json(url: str) -> dict | list:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ghost-sync-vendor"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ghost-sync-vendor"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _latest_tag() -> str:
    data = _http_json(f"{API}/releases/latest")
    tag = data.get("tag_name")
    if not tag:
        raise SystemExit("no latest release tag")
    return str(tag)


def _current_tag() -> str | None:
    if not VERSION_FILE.is_file():
        return None
    text = VERSION_FILE.read_text(encoding="utf-8").strip()
    return text or None


def _fetch_src(tag: str, path: str) -> str:
    return _http_bytes(f"{RAW}/{tag}/{path}").decode("utf-8")


def _module_nodes(src: str) -> tuple[ast.Module, list[str]]:
    tree = ast.parse(src)
    return tree, src.splitlines()


def _assign_src(tree: ast.Module, lines: list[str], name: str) -> str:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return "\n".join(lines[node.lineno - 1 : node.end_lineno])
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise KeyError(name)


def _def_src(tree: ast.Module, lines: list[str], name: str) -> str:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise KeyError(name)


def _build_wm_html(container_src: str, tag: str) -> str:
    tree, lines = _module_nodes(container_src)
    clean = _def_src(tree, lines, "clean_html")
    drop = (
        "    out, uri_actions = _clean_embedded_data_uris(out)\n"
        "    if uri_actions:\n"
        "        actions.extend(uri_actions)\n"
        "\n"
        "    if not actions:"
    )
    if drop not in clean:
        raise SystemExit(
            "clean_html layout changed; update vendor_watermarks_remover.py "
            "(expected data-URI block before 'if not actions:')"
        )
    clean = clean.replace(drop, "    if not actions:")

    parts = [
        f'"""HTML AI-provenance scrub (Layer A) — from watermarks-remover '
        f"container_meta.py {tag}.\"\"\"",
        "",
        "from __future__ import annotations",
        "",
        "import re",
        "from collections.abc import Iterator",
        "",
        _assign_src(tree, lines, "AI_META_NAME_RE"),
        "",
        _assign_src(tree, lines, "_META_TAG_RE"),
        _assign_src(tree, lines, "_META_ATTR_RE"),
        "",
        _assign_src(tree, lines, "_GENERATOR_AI_RE"),
        _def_src(tree, lines, "_meta_attrs"),
        "",
        _def_src(tree, lines, "_is_cms_generator_meta"),
        "",
        _assign_src(tree, lines, "_JSONLD_CLOSE_RE"),
        "",
        _assign_src(tree, lines, "_HTML_SPACE"),
        "",
        _def_src(tree, lines, "_find_tag_end"),
        "",
        _def_src(tree, lines, "_iter_script_blocks"),
        "",
        _def_src(tree, lines, "_script_tag_is_jsonld"),
        "",
        clean,
        "",
    ]
    out = "\n".join(parts)
    ast.parse(out)
    return out


def _build_wm_image(image_src: str, tag: str) -> str:
    tree, lines = _module_nodes(image_src)
    const_order = [
        "PNG_SIG",
        "JPEG_SOI",
        "MAX_PNG_TEXT_DECOMPRESSED_BYTES",
        "C2PA_MARKERS",
        "AI_META_HINTS",
        "AI_GENERATOR_PRODUCTS",
        "JPEG_C2PA_MARKERS",
        "JPEG_COM_AI_HINTS",
        "_GENERATOR_TEXT_KEYS",
    ]
    func_order = [
        "PngTextBudgetExceeded",
        "_contains_any",
        "_zlib_decompress_bounded",
        "_png_text_entries",
        "_generator_product_hits",
        "_png_text_hits",
        "_text_chunk_is_ai",
        "strip_png",
        "strip_jpeg",
    ]
    parts = [
        f'"""PNG/JPEG C2PA and AI metadata strip — from watermarks-remover '
        f"image_meta.py {tag}.\"\"\"",
        "",
        "from __future__ import annotations",
        "",
        "import struct",
        "import zlib",
        "",
    ]
    for name in const_order:
        parts.append(_assign_src(tree, lines, name))
        parts.append("")
    for name in func_order:
        parts.append(_def_src(tree, lines, name))
        parts.append("")
    parts.extend(
        [
            "def detect_format(data: bytes) -> str:",
            "    if data.startswith(PNG_SIG):",
            '        return "png"',
            "    if data.startswith(JPEG_SOI):",
            '        return "jpeg"',
            '    return "unknown"',
            "",
            "",
            "def clean_image_bytes(data: bytes) -> tuple[bytes, list[str], str]:",
            '    """Strip C2PA/AI metadata from PNG/JPEG bytes. Other formats returned unchanged."""',
            "    fmt = detect_format(data)",
            '    if fmt == "png":',
            "        cleaned, actions = strip_png(data, strip_all_text=True)",
            "        return cleaned, actions, fmt",
            '    if fmt == "jpeg":',
            "        cleaned, actions = strip_jpeg(data, strip_all_app=True)",
            "        return cleaned, actions, fmt",
            '    return data, [f"unsupported format ({fmt}); left unchanged"], fmt',
            "",
        ]
    )
    out = "\n".join(parts)
    ast.parse(out)
    return out


def _readme(tag: str) -> str:
    return f"""# watermarks-remover (vendored)

Upstream: [{UPSTREAM}](https://github.com/{UPSTREAM}) @ {tag} (MIT).

| File | Source |
|------|--------|
| `text_unicode.py` | `service/scripts/text_unicode.py` |
| `wm_html.py` | `service/scripts/container_meta.py` (`clean_html` + helpers; embedded data-URI image scrub omitted) |
| `wm_image.py` | `service/scripts/image_meta.py` (`strip_png` / `strip_jpeg` + helpers; `clean_image_bytes` local adapter) |
| `LICENSE` | upstream MIT |

Layer A text/HTML scrub + PNG/JPEG C2PA/metadata strip. Layer B rewrite, CtrlRegen pixel removal, and container formats beyond HTML/PNG/JPEG are not used.

Regenerate: `python scripts/vendor_watermarks_remover.py`. CI opens a weekly PR via `.github/workflows/vendor-watermarks-remover.yml`.
"""


def vendor(tag: str) -> None:
    print(f"vendoring {UPSTREAM}@{tag}", flush=True)
    text_unicode = _fetch_src(tag, "service/scripts/text_unicode.py")
    container = _fetch_src(tag, "service/scripts/container_meta.py")
    image = _fetch_src(tag, "service/scripts/image_meta.py")
    license_txt = _fetch_src(tag, "LICENSE")

    ast.parse(text_unicode)
    wm_html = _build_wm_html(container, tag)
    wm_image = _build_wm_image(image, tag)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "text_unicode.py").write_text(text_unicode, encoding="utf-8", newline="\n")
    (OUT / "wm_html.py").write_text(wm_html, encoding="utf-8", newline="\n")
    (OUT / "wm_image.py").write_text(wm_image, encoding="utf-8", newline="\n")
    (OUT / "LICENSE").write_text(license_txt, encoding="utf-8", newline="\n")
    (OUT / "README.md").write_text(_readme(tag), encoding="utf-8", newline="\n")
    VERSION_FILE.write_text(tag + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUT}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="upstream git tag (default: latest release)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if VERSION matches latest release; 1 if newer exists",
    )
    args = parser.parse_args(argv)

    try:
        tag = args.tag or _latest_tag()
    except urllib.error.URLError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 2

    if args.check:
        current = _current_tag()
        print(f"current={current or '(none)'} latest={tag}")
        return 0 if current == tag else 1

    try:
        vendor(tag)
    except (KeyError, SyntaxError, SystemExit) as exc:
        print(f"vendor failed: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
