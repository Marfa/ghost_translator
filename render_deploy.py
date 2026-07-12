"""Trigger Render deploy for the latest commit and print status."""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

RENDER_API = "https://api.render.com/v1"
POLL_INTERVAL_SEC = 15
DEPLOY_TIMEOUT_SEC = 900

_TERMINAL_OK = frozenset({"live"})
_TERMINAL_FAIL = frozenset(
    {"build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"}
)


def _render_api_key() -> str:
    key = os.getenv("RENDER_API_KEY", "").strip() or os.getenv("render", "").strip()
    if not key:
        raise RuntimeError("Missing Render API key (RENDER_API_KEY or render in .env)")
    return key


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def git_head_commit() -> str:
    out = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT
    )
    return out.strip()


def trigger_deploy(api_key: str, service_id: str, commit_id: str) -> str:
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{RENDER_API}/services/{service_id}/deploys",
            headers={**_headers(api_key), "Content-Type": "application/json"},
            json={"commitId": commit_id},
        )
        resp.raise_for_status()
        data = resp.json()
    deploy_id = data.get("id")
    if not deploy_id:
        raise RuntimeError(f"Render deploy response missing id: {data}")
    return deploy_id


def get_deploy(api_key: str, service_id: str, deploy_id: str) -> dict:
    with httpx.Client(timeout=60) as client:
        resp = client.get(
            f"{RENDER_API}/services/{service_id}/deploys/{deploy_id}",
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        return resp.json()


def wait_for_deploy(api_key: str, service_id: str, deploy_id: str) -> dict:
    deadline = time.monotonic() + DEPLOY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        deploy = get_deploy(api_key, service_id, deploy_id)
        status = deploy.get("status", "")
        logger.info("Deploy %s status: %s", deploy_id, status)
        if status in _TERMINAL_OK | _TERMINAL_FAIL:
            return deploy
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"Deploy {deploy_id} did not finish within {DEPLOY_TIMEOUT_SEC}s")


def _service_id(explicit: str = "") -> str:
    service_id = explicit.strip() or os.getenv("RENDER_SERVICE_ID", "").strip()
    if not service_id:
        raise RuntimeError("Missing RENDER_SERVICE_ID (env or --service-id)")
    return service_id


def _service_dashboard(service_id: str) -> str:
    return f"https://dashboard.render.com/web/{service_id}"


def _short_commit(commit_id: str) -> str:
    return commit_id[:7] if len(commit_id) >= 7 else commit_id


def run_deploy(commit_id: str, *, service_id: str) -> tuple[int, str]:
    api_key = _render_api_key()
    service_id = _service_id(service_id)
    dashboard = _service_dashboard(service_id)
    short = _short_commit(commit_id)

    logger.info("Triggering Render deploy for %s on %s", short, service_id)
    deploy_id = trigger_deploy(api_key, service_id, commit_id)
    deploy = wait_for_deploy(api_key, service_id, deploy_id)
    status = deploy.get("status", "unknown")

    if status in _TERMINAL_OK:
        message = (
            f"✅ Render: деплой успешен\n"
            f"Коммит: {short}\n"
            f"Deploy: {deploy_id}\n"
            f"Service: {dashboard}"
        )
        logger.info("Deploy succeeded: %s", deploy_id)
        return 0, message

    message = (
        f"❌ Render: ошибка деплоя\n"
        f"Коммит: {short}\n"
        f"Статус: {status}\n"
        f"Deploy: {deploy_id}\n"
        f"Service: {dashboard}"
    )
    logger.error("Deploy failed: %s status=%s", deploy_id, status)
    return 1, message


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    load_dotenv()

    parser = argparse.ArgumentParser(description="Deploy latest commit to Render")
    parser.add_argument("--commit", default="", help="Git commit SHA (default: HEAD)")
    parser.add_argument(
        "--service-id",
        default=os.getenv("RENDER_SERVICE_ID", ""),
        help="Render service ID",
    )
    args = parser.parse_args(argv)

    commit_id = args.commit.strip() or git_head_commit()
    try:
        code, message = run_deploy(commit_id, service_id=args.service_id)
        print(message)
        return code
    except Exception as exc:
        short = _short_commit(commit_id)
        message = (
            f"❌ Render: не удалось задеплоить\n"
            f"Коммит: {short}\n"
            f"Ошибка: {exc}"
        )
        logger.exception("Deploy flow failed")
        print(message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
