"""HTML AI-provenance scrub (Layer A) — from watermarks-remover container_meta.py v0.7.0."""

from __future__ import annotations

import re
from collections.abc import Iterator

AI_META_NAME_RE = re.compile(
    r"generator|ai[-_ ]?generated|claude|anthropic|openai|gemini|synthid|"
    r"c2pa|content.?credential|provenance|digital.?source|aigc",
    re.I,
)

_META_TAG_RE = re.compile(
    r"<meta\b[^>]*>",
    re.I,
)
_META_ATTR_RE = re.compile(
    r"""(name|property|content|generator)\s*=\s*["']([^"']*)["']""",
    re.I,
)

_GENERATOR_AI_RE = re.compile(
    r"claude|anthropic|openai|chatgpt|gemini|synthid|copilot|midjourney|dall.?e|stable.?diffusion",
    re.I,
)
def _meta_attrs(tag: str) -> dict[str, str]:
    return {name.lower(): value for name, value in _META_ATTR_RE.findall(tag)}

def _is_cms_generator_meta(tag: str) -> bool:
    """Return True for a generator meta tag that is CMS provenance, not AI."""
    attrs = _meta_attrs(tag)
    name_or_prop = (
        attrs.get("name") or attrs.get("property") or attrs.get("generator") or ""
    ).lower()
    if name_or_prop != "generator":
        return False
    return not (_GENERATOR_AI_RE.search(attrs.get("content", "")) or _GENERATOR_AI_RE.search(tag))

_JSONLD_CLOSE_RE = re.compile(r"</script>", re.I)

_HTML_SPACE = " \t\r\n\f"

def _find_tag_end(text: str, start: int) -> int:
    """Return the index just after the closing '>' of the tag at 'start'.

    Quote-aware: a '>' inside a quoted attribute value does not end the tag.
    Linear; returns len(text) when the tag is unterminated.
    """
    n = len(text)
    i = start + 1
    while i < n:
        c = text[i]
        if c == ">":
            return i + 1
        if c in "\"'":
            quote = c
            i += 1
            while i < n and text[i] != quote:
                i += 1
            i += 1  # skip closing quote
        else:
            i += 1
    return n

def _iter_script_blocks(
    text: str,
) -> Iterator[tuple[int, int, int, int]]:
    """Yield (open_start, open_end, close_start, close_end) for script blocks.

    Linear and quote-aware, with no length cap on the opening tag: each opening
    tag is scanned to its true boundary and closing tags are advanced by a
    forward pointer, so an unterminated run of '<script' costs one pass, not a
    rescan per prefix. Yields the same 4-tuple shape as _iter_tag_blocks.
    """
    closes = [m.start() for m in _JSONLD_CLOSE_RE.finditer(text)]
    ci = 0
    last_end = 0
    pos = 0
    n = len(text)
    low = text.lower()
    while True:
        i = low.find("<script", pos)
        if i < 0:
            return
        after = i + 7
        if after >= n or text[after] not in ">" + _HTML_SPACE + "/":
            pos = i + 1
            continue
        open_end = _find_tag_end(text, i)
        if i < last_end:
            pos = max(open_end, i + 1)
            continue
        while ci < len(closes) and closes[ci] < open_end:
            ci += 1
        if ci >= len(closes):
            return
        close_start = closes[ci]
        close_end = close_start + len("</script>")
        yield i, open_end, close_start, close_end
        last_end = close_end
        pos = open_end

def _script_tag_is_jsonld(open_tag: str) -> bool:
    """True iff the opening tag has a top-level type="application/ld+json".

    Single-pass and quote-aware: quoted attribute values are skipped as a unit,
    so only an actual top-level attribute named 'type' with the JSON-LD value
    is matched. Linear in the tag length.
    """
    i, n = 0, len(open_tag)
    if i < n and open_tag[i] == "<":
        i += 1
    while i < n and open_tag[i] not in _HTML_SPACE + "/>":  # tag name
        i += 1
    while i < n:
        while i < n and open_tag[i] in _HTML_SPACE:
            i += 1
        if i >= n or open_tag[i] == ">":
            return False
        name_start = i
        while i < n and open_tag[i] not in "=" + _HTML_SPACE + "/>":
            i += 1
        name = open_tag[name_start:i]
        while i < n and open_tag[i] in _HTML_SPACE:
            i += 1
        value = ""
        if i < n and open_tag[i] == "=":
            i += 1
            while i < n and open_tag[i] in _HTML_SPACE:
                i += 1
            if i < n and open_tag[i] in "\"'":
                quote = open_tag[i]
                i += 1
                value_start = i
                while i < n and open_tag[i] != quote:
                    i += 1
                value = open_tag[value_start:i]
                i += 1  # skip closing quote
            else:
                value_start = i
                while i < n and open_tag[i] not in _HTML_SPACE + ">":
                    i += 1
                value = open_tag[value_start:i]
        if name.lower() == "type" and value.lower() == "application/ld+json":
            return True
    return False

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

    def _block_is_ai(open_tag: str, blob: str) -> bool:
        if not _script_tag_is_jsonld(open_tag):
            return False
        return AI_META_NAME_RE.search(blob) or re.search(
            r"DigitalSourceType|trainedAlgorithmicMedia|SoftwareAgent", blob, re.I
        )

    kept = []
    last = 0
    n = 0
    for os_, oe, _cs, ce in _iter_script_blocks(out):
        blob = out[os_:ce]
        if not _block_is_ai(out[os_:oe], blob):
            continue
        kept.append(out[last:os_])
        last = ce
        n += 1
    if n:
        kept.append(out[last:])
        out = "".join(kept)
        actions.extend(["drop json-ld provenance-like script"] * n)
    out2, n = re.subn(r"\sdata-ai[\w-]*\s*=\s*[\"'][^\"']*[\"']", "", out, flags=re.I)
    if n:
        actions.append(f"drop data-ai* attributes x{n}")
        out = out2

    if not actions:
        actions.append("no HTML AI meta removed")
    return out, actions
