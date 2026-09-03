"""Ghost RU→EN sync: published on source → translated draft on target."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import struct
import sys
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import deepl
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from starlette.responses import Response

_WM_DIR = Path(__file__).resolve().parent / "vendor" / "wm"
sys.path.insert(0, str(_WM_DIR))
from text_unicode import clean_text as _wm_clean_text  # noqa: E402
from wm_html import clean_html as _wm_clean_html  # noqa: E402
from wm_image import clean_image_bytes as _wm_clean_image_bytes  # noqa: E402
sys.path.pop(0)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ghost-sync")

SOURCE_URL = os.getenv("SOURCE_GHOST_URL", "").rstrip("/")
SOURCE_KEY = os.getenv("SOURCE_GHOST_ADMIN_API_KEY", "")
TARGET_URL = os.getenv("TARGET_GHOST_URL", "").rstrip("/")
TARGET_KEY = os.getenv("TARGET_GHOST_ADMIN_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
MAP_PATH = Path(os.getenv("MAP_FILE", "map.json"))

http = httpx.Client(timeout=60.0)
_deepl_client: deepl.DeepLClient | None = None

app = FastAPI()


def _get_deepl() -> deepl.DeepLClient:
    global _deepl_client
    if _deepl_client is None:
        _deepl_client = deepl.DeepLClient(os.environ["DEEPL_API_KEY"])
    return _deepl_client


def _ghost_token(admin_key: str) -> str:
    key_id, secret = admin_key.split(":", 1)
    now = int(time.time())
    return jwt.encode(
        {"iat": now, "exp": now + 300, "aud": "/admin/"},
        bytes.fromhex(secret),
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT", "kid": key_id},
    )


def _ghost(base: str, key: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    response = http.request(
        method,
        f"{base}/ghost/api/admin/{path}",
        headers={
            "Authorization": f"Ghost {_ghost_token(key)}",
            "Accept-Version": "v5.0",
            "Content-Type": "application/json",
        },
        **kwargs,
    )
    if response.is_error:
        log.error("ghost %s %s → %s %s", method, path, response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()


def _cover_filename(url: str, fmt: str) -> str:
    name = Path(unquote(urlparse(url).path)).name or "cover"
    stem = Path(name).stem or "cover"
    if fmt == "png":
        return f"{stem}.png"[:200]
    if fmt == "jpeg":
        return f"{stem}.jpg"[:200]
    return name[:200] or "cover"


def _cover_content_type(fmt: str, fallback: str | None) -> str:
    if fmt == "png":
        return "image/png"
    if fmt == "jpeg":
        return "image/jpeg"
    if fallback and fallback.startswith("image/"):
        return fallback.split(";")[0].strip()
    return "application/octet-stream"


def _ghost_upload_image(
    base: str,
    key: str,
    data: bytes,
    filename: str,
    content_type: str,
) -> str:
    response = http.post(
        f"{base}/ghost/api/admin/images/upload/",
        headers={
            "Authorization": f"Ghost {_ghost_token(key)}",
            "Accept-Version": "v5.0",
        },
        files={"file": (filename, data, content_type)},
        data={"purpose": "image", "ref": filename},
    )
    if response.is_error:
        log.error("ghost upload image %s → %s %s", base, response.status_code, response.text[:500])
    response.raise_for_status()
    images = response.json().get("images") or []
    if not images or not images[0].get("url"):
        raise RuntimeError("Ghost image upload returned no url")
    return images[0]["url"]


def _cover_was_scrubbed(actions: list[str]) -> bool:
    return any(a.startswith("drop") for a in actions)


def _apply_cover_url(post: dict[str, Any], old_url: str, new_url: str) -> None:
    post["feature_image"] = new_url
    post["og_image"] = new_url
    post["twitter_image"] = new_url
    html = post.get("html")
    if isinstance(html, str) and old_url in html:
        post["html"] = html.replace(old_url, new_url)


def _update_source_cover(
    post: dict[str, Any],
    old_url: str,
    new_url: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "updated_at": post["updated_at"],
        "feature_image": new_url,
        "og_image": new_url,
        "twitter_image": new_url,
    }
    params: dict[str, str] = {}
    html = post.get("html")
    if isinstance(html, str) and old_url in html:
        payload["html"] = html.replace(old_url, new_url)
        params["source"] = "html"
    saved = _ghost(
        SOURCE_URL,
        SOURCE_KEY,
        "PUT",
        f"posts/{post['id']}/",
        params=params,
        json={"posts": [payload]},
    )["posts"][0]
    return saved


def _scrub_and_relocate_cover(post: dict[str, Any]) -> str | None:
    """Strip cover C2PA; replace on RU when dirty; upload clean copy to EN. Returns EN URL."""
    old_url = post.get("feature_image")
    if not old_url:
        return None
    fetch_url = f"{SOURCE_URL}{old_url}" if old_url.startswith("/") else old_url
    response = http.get(fetch_url)
    if response.is_error:
        log.error("cover download %s → %s", fetch_url, response.status_code)
    response.raise_for_status()
    cleaned, actions, fmt = _wm_clean_image_bytes(response.content)
    log.info("cover scrub %s (%s): %s", fetch_url, fmt, "; ".join(actions[:6]))
    filename = _cover_filename(fetch_url, fmt)
    content_type = _cover_content_type(fmt, response.headers.get("content-type"))

    # Only rewrite RU when metadata was actually dropped — avoids webhook edit loops
    if _cover_was_scrubbed(actions):
        source_url = _ghost_upload_image(
            SOURCE_URL, SOURCE_KEY, cleaned, filename, content_type
        )
        saved = _update_source_cover(post, old_url, source_url)
        _apply_cover_url(post, old_url, source_url)
        post["updated_at"] = saved["updated_at"]
        log.info("source cover replaced everywhere %s → %s", old_url, source_url)

    return _ghost_upload_image(TARGET_URL, TARGET_KEY, cleaned, filename, content_type)


# ponytail: DeepL request body cap is 128 KiB; stay under with margin for JSON overhead
_DEEPL_MAX_BYTES = 100 * 1024

def _tr(text: str, *, html: bool = False) -> str:
    text = text.strip()
    if not text:
        return text
    kwargs: dict[str, Any] = {
        "source_lang": "RU",
        "target_lang": "EN-US",
        "model_type": deepl.ModelType.PREFER_QUALITY_OPTIMIZED,
    }
    if html:
        kwargs["tag_handling"] = "html"
        kwargs["tag_handling_version"] = "v2"
        kwargs["split_sentences"] = "nonewlines"
    return _get_deepl().translate_text(text, **kwargs).text


_BLOCK_END = re.compile(
    r"(</p>|</h1>|</h2>|</h3>|</h4>|</h5>|</h6>|</li>|</figure>|</blockquote>|</tr>)"
)
# Ghost source=html turns /tag/ links into post tags; strip them from synced HTML
_TAG_BLOCK = re.compile(
    r'<p>(?:\s*<a\b[^>]*\bhref="[^"]*/tag/[^"]*"[^>]*>[^<]*</a>\s*)+</p>\s*',
    re.I,
)
# #[^<]+ already spans "#a #b"; nested (?:\s*#…)+ was ReDoS-prone
_HASH_TAG_BLOCK = re.compile(r"<p>\s*#[^<]+</p>\s*", re.I)


def _strip_tag_links(html: str) -> str:
    html = _TAG_BLOCK.sub("", html)
    return _HASH_TAG_BLOCK.sub("", html).strip()


_WM_TEXT_FIELDS = (
    "title",
    "custom_excerpt",
    "excerpt",
    "meta_title",
    "meta_description",
    "og_title",
    "og_description",
    "twitter_title",
    "twitter_description",
    "feature_image_alt",
)


def _strip_watermarks_text(text: str) -> str:
    if not text:
        return text
    cleaned, _ = _wm_clean_text(text)
    return cleaned


def _strip_watermarks_html(html: str) -> str:
    if not html:
        return html
    cleaned, _ = _wm_clean_html(html)
    cleaned, _ = _wm_clean_text(cleaned)
    return cleaned


def _strip_source_watermarks(post: dict[str, Any]) -> dict[str, Any]:
    out = dict(post)
    for key in _WM_TEXT_FIELDS:
        if out.get(key):
            out[key] = _strip_watermarks_text(out[key])
    if out.get("html"):
        out["html"] = _strip_watermarks_html(out["html"])
    return out


_PRESERVE_MARKERS = ("kg-callout-card", "kg-cta-card")


def _split_preserve_callouts(html: str) -> list[tuple[bool, str]]:
    chunks: list[tuple[bool, str]] = []
    pos = 0
    while pos < len(html):
        hits = [(html.find(marker, pos), marker) for marker in _PRESERVE_MARKERS]
        hits = [(idx, marker) for idx, marker in hits if idx != -1]
        if not hits:
            chunks.append((True, html[pos:]))
            break
        idx, marker = min(hits, key=lambda hit: hit[0])
        div_start = html.rfind("<div", pos, idx)
        if div_start == -1:
            chunks.append((True, html[pos : idx + len(marker)]))
            pos = idx + len(marker)
            continue
        if div_start > pos:
            chunks.append((True, html[pos:div_start]))
        depth = 0
        end = div_start
        i = div_start
        while i < len(html):
            if html[i : i + 4].lower() == "<div":
                depth += 1
            elif html[i : i + 6].lower() == "</div>":
                depth -= 1
                if depth == 0:
                    end = i + 6
                    break
            i += 1
        else:
            chunks.append((True, html[div_start:]))
            break
        chunks.append((False, html[div_start:end]))
        pos = end
    return chunks


def _split_html_blocks(html: str) -> list[str]:
    pieces = _BLOCK_END.split(html)
    blocks: list[str] = []
    for i in range(0, len(pieces) - 1, 2):
        blocks.append(pieces[i] + pieces[i + 1])
    if len(pieces) % 2 == 1 and pieces[-1]:
        blocks.append(pieces[-1])
    return blocks


def _tr_html_chunk(html: str) -> str:
    html = html.strip()
    if not html:
        return html
    if len(html.encode("utf-8")) <= _DEEPL_MAX_BYTES:
        return _tr(html, html=True)

    translated: list[str] = []
    buf = ""
    for block in _split_html_blocks(html):
        candidate = buf + block
        if buf and len(candidate.encode("utf-8")) > _DEEPL_MAX_BYTES:
            translated.append(_tr(buf, html=True))
            buf = block
        else:
            buf = candidate
    if buf:
        translated.append(_tr(buf, html=True))
    return "".join(translated)


def _tr_html(html: str) -> str:
    html = html.strip()
    if not html:
        return html
    parts: list[str] = []
    for translate, chunk in _split_preserve_callouts(html):
        parts.append(_tr_html_chunk(chunk) if translate else chunk)
    return "".join(parts)


def _load_map() -> dict[str, str]:
    if not MAP_PATH.exists():
        return {}
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def _save_map(mapping: dict[str, str]) -> None:
    MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAP_PATH.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _tr(title).lower()).strip("-")
    return slug[:240] or "post"


# Ghost Admin API maxLength (422 if DeepL expands past these)
_GHOST_MAX = {
    "title": 255,
    "custom_excerpt": 300,
    "meta_title": 300,
    "meta_description": 500,
    "og_title": 300,
    "og_description": 500,
    "twitter_title": 300,
    "twitter_description": 500,
    "feature_image_alt": 125,
}


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def _build_draft(post: dict[str, Any]) -> dict[str, Any]:
    title = _clip(_tr(post.get("title", "")), _GHOST_MAX["title"])
    draft: dict[str, Any] = {
        "title": title,
        "slug": _slug(post.get("title", "post")),
        "status": "draft",
        "html": _tr_html(_strip_tag_links(post.get("html") or "<p></p>")),
        "tags": [],
    }

    excerpt = post.get("custom_excerpt") or post.get("excerpt")
    if excerpt:
        draft["custom_excerpt"] = _clip(_tr(excerpt), _GHOST_MAX["custom_excerpt"])

    meta_title_src = post.get("meta_title") or post.get("og_title")
    if meta_title_src:
        draft["meta_title"] = _clip(_tr(meta_title_src), _GHOST_MAX["meta_title"])

    meta_desc_src = post.get("meta_description") or post.get("og_description")
    if meta_desc_src:
        draft["meta_description"] = _clip(_tr(meta_desc_src), _GHOST_MAX["meta_description"])

    # Facebook card = og_*; Ghost often leaves these null when UI reuses excerpt/meta
    og_title_src = post.get("og_title") or post.get("meta_title") or post.get("title")
    if og_title_src:
        draft["og_title"] = _clip(_tr(og_title_src), _GHOST_MAX["og_title"])

    og_desc_src = (
        post.get("og_description")
        or post.get("meta_description")
        or post.get("custom_excerpt")
        or post.get("excerpt")
    )
    if og_desc_src:
        draft["og_description"] = _clip(_tr(og_desc_src), _GHOST_MAX["og_description"])

    twitter_title_src = post.get("twitter_title") or post.get("og_title") or post.get("meta_title") or post.get("title")
    if twitter_title_src:
        draft["twitter_title"] = _clip(_tr(twitter_title_src), _GHOST_MAX["twitter_title"])

    twitter_desc_src = (
        post.get("twitter_description")
        or post.get("og_description")
        or post.get("meta_description")
        or post.get("custom_excerpt")
        or post.get("excerpt")
    )
    if twitter_desc_src:
        draft["twitter_description"] = _clip(
            _tr(twitter_desc_src), _GHOST_MAX["twitter_description"]
        )

    if post.get("feature_image"):
        cover = _scrub_and_relocate_cover(post)
        if cover:
            draft["feature_image"] = cover
            draft["og_image"] = cover
            draft["twitter_image"] = cover
    if post.get("feature_image_alt"):
        draft["feature_image_alt"] = _clip(
            _tr(post["feature_image_alt"]), _GHOST_MAX["feature_image_alt"]
        )

    return draft


def _preserve_target_state(payload: dict[str, Any], target_post: dict[str, Any]) -> dict[str, Any]:
    # source edit must not unpublish an already-live EN post
    payload["updated_at"] = target_post["updated_at"]
    payload["status"] = target_post.get("status") or "draft"
    return payload


def sync_post(source_id: str) -> dict[str, Any]:
    for name, value in {
        "SOURCE_GHOST_URL": SOURCE_URL,
        "TARGET_GHOST_URL": TARGET_URL,
        "SOURCE_GHOST_ADMIN_API_KEY": SOURCE_KEY,
        "TARGET_GHOST_ADMIN_API_KEY": TARGET_KEY,
        "DEEPL_API_KEY": os.getenv("DEEPL_API_KEY"),
    }.items():
        if not value:
            raise RuntimeError(f"Missing {name}")

    post = _ghost(
        SOURCE_URL,
        SOURCE_KEY,
        "GET",
        f"posts/{source_id}/",
        params={"formats": "html"},
    )["posts"][0]

    if post.get("status") != "published":
        return {"skipped": True, "reason": "not published"}

    post = _strip_source_watermarks(post)
    draft = _build_draft(post)
    mapping = _load_map()
    target_id = mapping.get(source_id)

    if not target_id:
        by_slug = _ghost(
            TARGET_URL,
            TARGET_KEY,
            "GET",
            "posts/",
            params={"filter": f"slug:{draft['slug']}", "limit": 1},
        )["posts"]
        if by_slug:
            target_id = by_slug[0]["id"]
            mapping[source_id] = target_id
            _save_map(mapping)

    if target_id:
        target_post = _ghost(
            TARGET_URL,
            TARGET_KEY,
            "GET",
            f"posts/{target_id}/",
        )["posts"][0]
        saved = _ghost(
            TARGET_URL,
            TARGET_KEY,
            "PUT",
            f"posts/{target_id}/",
            params={"source": "html"},
            json={"posts": [_preserve_target_state(draft, target_post)]},
        )["posts"][0]
        return {"action": "updated", "target_post_id": saved["id"], "slug": saved.get("slug")}

    saved = _ghost(
        TARGET_URL,
        TARGET_KEY,
        "POST",
        "posts/",
        params={"source": "html"},
        json={"posts": [draft]},
    )["posts"][0]
    mapping[source_id] = saved["id"]
    _save_map(mapping)
    return {"action": "created", "target_post_id": saved["id"], "slug": saved.get("slug")}


def _parse_since(raw: str) -> str:
    if "T" not in raw:
        return f"{raw}T00:00:00.000Z"
    return raw


def _reconcile_since() -> str:
    # ponytail: rolling window; cron every 6h still catches gaps within a day
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return since.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _list_published_since(since: str) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    page = 1
    while True:
        data = _ghost(
            SOURCE_URL,
            SOURCE_KEY,
            "GET",
            "posts/",
            params={
                "filter": f"status:published+published_at:>'{since}'",
                "fields": "id,title,slug,published_at",
                "order": "published_at desc",
                "limit": 50,
                "page": page,
            },
        )
        posts.extend(data["posts"])
        pagination = data.get("meta", {}).get("pagination", {})
        if page >= pagination.get("pages", 1):
            break
        page += 1
    return posts


def reconcile() -> dict[str, Any]:
    since = _reconcile_since()
    mapping = _load_map()
    missed = [p for p in _list_published_since(since) if p["id"] not in mapping]
    results: list[dict[str, Any]] = []
    for post in missed:
        source_id = post["id"]
        try:
            result = sync_post(source_id)
            results.append({"source_id": source_id, "title": post.get("title"), **result})
        except Exception as exc:
            log.exception("reconcile sync %s failed", source_id)
            results.append({"source_id": source_id, "title": post.get("title"), "error": str(exc)})
    return {"since": since, "missed": len(missed), "results": results}


def _require_sync_secret(x_sync_secret: Optional[str]) -> None:
    if WEBHOOK_SECRET and x_sync_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid X-Sync-Secret")


def _verify_signature(body: bytes, signature_header: Optional[str]) -> None:
    if not WEBHOOK_SECRET:
        return
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Ghost-Signature")

    parts: dict[str, str] = {}
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key and value:
            parts[key] = value

    received = parts.get("sha256")
    timestamp = parts.get("t")
    if not received or not timestamp:
        raise HTTPException(status_code=401, detail="Invalid X-Ghost-Signature")

    digest = hmac.new(
        WEBHOOK_SECRET.encode(),
        (body.decode("utf-8") + timestamp).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(digest, received):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.head("/health", include_in_schema=False)
def health_head() -> Response:
    return Response(status_code=200)


@app.get("/status")
def status() -> dict[str, bool]:
    """Проверка, что переменные окружения заданы (без раскрытия значений)."""
    return {
        "source_configured": bool(SOURCE_URL and SOURCE_KEY),
        "target_configured": bool(TARGET_URL and TARGET_KEY),
        "deepl_configured": bool(os.getenv("DEEPL_API_KEY")),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
    }


@app.post("/webhook/ghost")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_ghost_signature: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    _verify_signature(body, x_ghost_signature)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    post = (payload.get("post") or {}).get("current") or payload.get("post")
    if not post:
        return {"ok": True, "skipped": True, "reason": "no post in payload"}

    post_id = post["id"]
    log.info("queued sync %s (%s)", post_id, post.get("title", "?"))
    background.add_task(_run_sync, post_id)
    return {"ok": True, "queued": True, "source_post_id": post_id}


@app.post("/sync/{post_id}")
def manual_sync(
    post_id: str,
    background: BackgroundTasks,
    x_sync_secret: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Повторная синхронизация, если webhook не дошёл (например, сервис спал)."""
    _require_sync_secret(x_sync_secret)
    log.info("manual sync queued %s", post_id)
    background.add_task(_run_sync, post_id)
    return {"ok": True, "queued": True, "source_post_id": post_id}


@app.post("/reconcile")
def reconcile_endpoint(
    background: BackgroundTasks,
    x_sync_secret: Optional[str] = Header(default=None),
) -> dict[str, Any]:
    """Сверка постов за последние 24 часа и перевод пропущенных."""
    _require_sync_secret(x_sync_secret)
    log.info("reconcile queued (since %s)", _reconcile_since())
    background.add_task(_run_reconcile)
    return {"ok": True, "queued": True, "since": _reconcile_since()}


def _run_sync(post_id: str) -> None:
    try:
        result = sync_post(post_id)
        log.info("sync %s done: %s", post_id, result)
    except Exception:
        log.exception("sync %s failed", post_id)


def _run_reconcile() -> None:
    try:
        result = reconcile()
        log.info("reconcile done: missed=%s", result["missed"])
        for item in result["results"]:
            log.info("reconcile %s: %s", item.get("source_id"), item)
    except Exception:
        log.exception("reconcile failed")


if __name__ == "__main__":
    assert (
        _preserve_target_state({"status": "draft"}, {"updated_at": "t", "status": "published"})[
            "status"
        ]
        == "published"
    )
    assert _clip("short", 300) == "short"
    assert len(_clip("x" * 400, 300)) == 300
    assert _clip("x" * 400, 300).endswith("…")
    since = datetime.fromisoformat(_reconcile_since().replace("Z", "+00:00"))
    assert timedelta(hours=23, minutes=59) < datetime.now(timezone.utc) - since < timedelta(hours=24, minutes=1)
    assert _parse_since("2025-06-01") == "2025-06-01T00:00:00.000Z"
    assert _strip_tag_links('<p><a href="/tag/android/">#android</a></p><p>keep</p>') == "<p>keep</p>"
    assert _strip_tag_links("<p>#android #Quick Cursor: One-Hand Aid</p><p>keep</p>") == "<p>keep</p>"
    zwsp = "hello\u200bworld"
    assert _strip_watermarks_text(zwsp) == "helloworld"
    ai_html = '<p>ok</p><meta name="generator" content="Claude">'
    assert "Claude" not in _strip_watermarks_html(ai_html)
    assert "<p>ok</p>" in _strip_watermarks_html(ai_html)

    def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(ctype)
        crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00")
    dirty_png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"tEXt", b"Software\x00Claude")
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )
    cleaned_png, png_actions, png_fmt = _wm_clean_image_bytes(dirty_png)
    assert png_fmt == "png"
    assert b"Claude" not in cleaned_png
    assert any("tEXt" in a for a in png_actions)
    assert _cover_was_scrubbed(png_actions)
    assert not _cover_was_scrubbed(["no PNG metadata chunks removed (already clean or none matched)"])
    sample = {
        "feature_image": "https://ru.example/old.png",
        "og_image": "https://ru.example/other.png",
        "twitter_image": None,
        "html": '<p><img src="https://ru.example/old.png"></p>',
    }
    _apply_cover_url(sample, "https://ru.example/old.png", "https://ru.example/clean.png")
    assert sample["feature_image"] == sample["og_image"] == sample["twitter_image"] == "https://ru.example/clean.png"
    assert "old.png" not in sample["html"]
    assert _cover_filename("https://cdn.example/x/photo.webp", "jpeg") == "photo.jpg"
    callout = (
        '<div class="kg-card kg-callout-card kg-callout-card-accent">'
        '<div class="kg-callout-emoji">💡</div>'
        '<div class="kg-callout-text"><p>Не переводить</p></div></div>'
    )
    mixed = f"<p>Перевести</p>{callout}<p>Тоже перевести</p>"
    assert _split_preserve_callouts(mixed) == [
        (True, "<p>Перевести</p>"),
        (False, callout),
        (True, "<p>Тоже перевести</p>"),
    ]
    cta = (
        '<div class="kg-card kg-cta-card kg-cta-bg-grey kg-cta-minimal kg-cta-pos-left">'
        '<div class="kg-cta-content"><p>Не переводить CTA</p>'
        '<a class="kg-cta-button" href="https://example.com">Подписаться</a></div></div>'
    )
    mixed_cta = f"<p>Перевести</p>{cta}<p>Тоже перевести</p>"
    assert _split_preserve_callouts(mixed_cta) == [
        (True, "<p>Перевести</p>"),
        (False, cta),
        (True, "<p>Тоже перевести</p>"),
    ]
    big = "<p>x</p>" * 60_000
    assert len(big.encode("utf-8")) > _DEEPL_MAX_BYTES
    buf, parts = "", []
    for block in _split_html_blocks(big):
        candidate = buf + block
        if buf and len(candidate.encode("utf-8")) > _DEEPL_MAX_BYTES:
            parts.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    assert len(parts) > 1
    assert all(len(p.encode("utf-8")) <= _DEEPL_MAX_BYTES for p in parts)
