"""HTML AI-provenance scrub (Layer A) — from watermarks-remover container_meta.py v0.4.0."""

from __future__ import annotations

import re

AI_META_NAME_RE = re.compile(
    r"generator|ai[-_ ]?generated|claude|anthropic|openai|gemini|synthid|"
    r"c2pa|content.?credential|provenance|digital.?source|aigc",
    re.I,
)

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR_RE = re.compile(
    r"""(name|property|content|generator)\s*=\s*["']([^"']*)["']""",
    re.I,
)
_GENERATOR_AI_RE = re.compile(
    r"claude|anthropic|openai|chatgpt|gemini|synthid|copilot|midjourney|dall.?e|stable.?diffusion",
    re.I,
)
_JSONLD_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>.*?</script>",
    re.I | re.DOTALL,
)


def _meta_attrs(tag: str) -> dict[str, str]:
    return dict(_META_ATTR_RE.findall(tag))


def _is_cms_generator_meta(tag: str) -> bool:
    attrs = _meta_attrs(tag)
    name_or_prop = (
        attrs.get("name") or attrs.get("property") or attrs.get("generator") or ""
    ).lower()
    if name_or_prop != "generator":
        return False
    if _GENERATOR_AI_RE.search(attrs.get("content", "")) or _GENERATOR_AI_RE.search(tag):
        return False
    return True


def clean_html(text: str) -> tuple[str, list[str]]:
    actions: list[str] = []

    def _meta_sub(m: re.Match[str]) -> str:
        tag = m.group(0)
        if _is_cms_generator_meta(tag):
            return tag
        if AI_META_NAME_RE.search(tag) or re.search(
            r"generator|claude|anthropic|openai|gemini|synthid|c2pa|aigc", tag, re.I
        ):
            actions.append(f"drop meta: {tag[:80]}")
            return ""
        return tag

    out = _META_TAG_RE.sub(_meta_sub, text)

    def _jsonld_sub(m: re.Match[str]) -> str:
        blob = m.group(0)
        if AI_META_NAME_RE.search(blob) or re.search(
            r"DigitalSourceType|trainedAlgorithmicMedia|SoftwareAgent", blob, re.I
        ):
            actions.append("drop json-ld provenance-like script")
            return ""
        return blob

    out = _JSONLD_RE.sub(_jsonld_sub, out)
    out2, n = re.subn(r"\sdata-ai[\w-]*\s*=\s*[\"'][^\"']*[\"']", "", out, flags=re.I)
    if n:
        actions.append(f"drop data-ai* attributes x{n}")
        out = out2
    if not actions:
        actions.append("no HTML AI meta removed")
    return out, actions
