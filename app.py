"""Ghost RU→EN sync: published on source → translated draft on target."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import deepl
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from starlette.responses import Response

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
_HASH_TAG_BLOCK = re.compile(r"<p>(?:\s*#[^<]+)+\s*</p>\s*", re.I)


def _strip_tag_links(html: str) -> str:
    html = _TAG_BLOCK.sub("", html)
    return _HASH_TAG_BLOCK.sub("", html).strip()


def _split_html_blocks(html: str) -> list[str]:
    pieces = _BLOCK_END.split(html)
    blocks: list[str] = []
    for i in range(0, len(pieces) - 1, 2):
        blocks.append(pieces[i] + pieces[i + 1])
    if len(pieces) % 2 == 1 and pieces[-1]:
        blocks.append(pieces[-1])
    return blocks


def _tr_html(html: str) -> str:
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


def _build_draft(post: dict[str, Any]) -> dict[str, Any]:
    title = _tr(post.get("title", ""))
    draft: dict[str, Any] = {
        "title": title,
        "slug": _slug(post.get("title", "post")),
        "status": "draft",
        "html": _tr_html(_strip_tag_links(post.get("html") or "<p></p>")),
        "tags": [],
    }

    excerpt = post.get("custom_excerpt") or post.get("excerpt")
    if excerpt:
        draft["custom_excerpt"] = _tr(excerpt)

    meta_title_src = post.get("meta_title") or post.get("og_title")
    if meta_title_src:
        draft["meta_title"] = _tr(meta_title_src)

    meta_desc_src = post.get("meta_description") or post.get("og_description")
    if meta_desc_src:
        draft["meta_description"] = _tr(meta_desc_src)

    # Facebook card = og_*; Ghost often leaves these null when UI reuses excerpt/meta
    og_title_src = post.get("og_title") or post.get("meta_title") or post.get("title")
    if og_title_src:
        draft["og_title"] = _tr(og_title_src)

    og_desc_src = (
        post.get("og_description")
        or post.get("meta_description")
        or post.get("custom_excerpt")
        or post.get("excerpt")
    )
    if og_desc_src:
        draft["og_description"] = _tr(og_desc_src)

    twitter_title_src = post.get("twitter_title") or post.get("og_title") or post.get("meta_title") or post.get("title")
    if twitter_title_src:
        draft["twitter_title"] = _tr(twitter_title_src)

    twitter_desc_src = (
        post.get("twitter_description")
        or post.get("og_description")
        or post.get("meta_description")
        or post.get("custom_excerpt")
        or post.get("excerpt")
    )
    if twitter_desc_src:
        draft["twitter_description"] = _tr(twitter_desc_src)

    if post.get("feature_image"):
        draft["feature_image"] = post["feature_image"]
    if post.get("feature_image_alt"):
        draft["feature_image_alt"] = _tr(post["feature_image_alt"])
    if post.get("og_image") or post.get("feature_image"):
        draft["og_image"] = post.get("og_image") or post["feature_image"]
    if post.get("twitter_image"):
        draft["twitter_image"] = post["twitter_image"]

    return draft


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
        draft["updated_at"] = target_post["updated_at"]
        saved = _ghost(
            TARGET_URL,
            TARGET_KEY,
            "PUT",
            f"posts/{target_id}/",
            params={"source": "html"},
            json={"posts": [draft]},
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
    since = datetime.fromisoformat(_reconcile_since().replace("Z", "+00:00"))
    assert timedelta(hours=23, minutes=59) < datetime.now(timezone.utc) - since < timedelta(hours=24, minutes=1)
    assert _parse_since("2025-06-01") == "2025-06-01T00:00:00.000Z"
    assert _strip_tag_links('<p><a href="/tag/android/">#android</a></p><p>keep</p>') == "<p>keep</p>"
    assert _strip_tag_links("<p>#android #Quick Cursor: One-Hand Aid</p><p>keep</p>") == "<p>keep</p>"
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
