"""Ghost RU→EN sync: published on source → translated draft on target."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import deepl
import httpx
import jwt
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

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
    response.raise_for_status()
    return response.json()


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
    draft: dict[str, Any] = {
        "title": _tr(post.get("title", "")),
        "slug": _slug(post.get("title", "post")),
        "status": "draft",
        "html": _tr(post.get("html") or "<p></p>", html=True),
        "tags": [{"name": _tr(tag["name"])} for tag in post.get("tags", []) if tag.get("name")],
    }

    excerpt = post.get("custom_excerpt") or post.get("excerpt")
    if excerpt:
        draft["custom_excerpt"] = _tr(excerpt)

    meta_title = post.get("meta_title") or post.get("og_title")
    if meta_title:
        draft["meta_title"] = draft["og_title"] = _tr(meta_title)

    meta_description = post.get("meta_description") or post.get("og_description")
    if meta_description:
        draft["meta_description"] = draft["og_description"] = _tr(meta_description)

    if post.get("feature_image"):
        draft["feature_image"] = post["feature_image"]
    if post.get("feature_image_alt"):
        draft["feature_image_alt"] = _tr(post["feature_image_alt"])

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
        params={"formats": "html", "include": "tags"},
    )["posts"][0]

    if post.get("status") != "published":
        return {"skipped": True, "reason": "not published"}

    draft = _build_draft(post)
    mapping = _load_map()
    target_id = mapping.get(source_id)

    if target_id:
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
    log.info("queued sync %s", post_id)
    background.add_task(_run_sync, post_id)
    return {"ok": True, "queued": True, "source_post_id": post_id}


def _run_sync(post_id: str) -> None:
    try:
        result = sync_post(post_id)
        log.info("sync %s done: %s", post_id, result)
    except Exception:
        log.exception("sync %s failed", post_id)
