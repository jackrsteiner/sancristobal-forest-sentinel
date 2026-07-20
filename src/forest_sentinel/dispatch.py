"""VM-triggered sync of dashboard changes to the instance repo (bead 7.3, #136).

The VM is deliberately keyless for repo *contents*: a ``contents: write``
credential could push to any branch and, through the deploy workflows,
escalate to the WIF provisioner's GCP permissions. Instead, after a
dashboard-originated change (settings edit, AOI upload, context upload), the
dashboard fires ``workflow_dispatch`` on the Update-instance workflow — whose
sync jobs pull the changed files off the VM over WIF-authenticated SSH and
commit them — so durability arrives in about a minute without a push
credential ever existing on the VM.

``workflow_dispatch`` needs only a fine-grained PAT scoped to the single
instance repo with **Actions: read/write** (``repository_dispatch`` would
require ``contents: write``, defeating the purpose — recorded in the Slice 7
plan). The token lives outside the repo tree in a ``0600`` file named by
``FOREST_SENTINEL_SYNC_TOKEN_FILE``; an absent/empty token means the feature
is off and changes stay VM-local until a manual sync — exactly the pre-bead
behavior, never an error.

Dispatches are best-effort and **after** the local write commits: a failure
logs a warning and the edit stands. A debounce collapses bursts of edits into
one dispatch — the sync jobs copy whole files, so one run syncs everything.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

SYNC_TOKEN_FILE_ENV_VAR = "FOREST_SENTINEL_SYNC_TOKEN_FILE"
GITHUB_REPO_ENV_VAR = "GITHUB_REPO"
WORKFLOW_FILE = "update-instance.yml"
DEBOUNCE_SECONDS = 60.0

# The sync-only input set: pull the VM's uploaded files into the repo without
# merging upstream or touching the VM.
DISPATCH_INPUTS = {
    "sync_upstream": "false",
    "sync_aois": "true",
    "sync_settings": "true",
    "update_vm": "false",
}

_lock = threading.Lock()
_last_dispatch: float | None = None


def request_sync(
    *,
    reason: str,
    update_vm: bool = False,
    http_post: Callable[[str, dict[str, str], bytes], int] | None = None,
) -> bool:
    """Fire the sync workflow (best-effort, debounced); True when dispatched.

    Never raises: the caller just made a successful local write, and sync
    failure must not un-succeed it. All misconfiguration paths (no token file
    configured, unreadable/empty token, no ``GITHUB_REPO``) are silent
    feature-off, not errors.

    ``update_vm`` (bead 7.5) additionally rolls the VM — for settings that are
    rendered into systemd units. Rollout requests bypass the debounce: a plain
    sync moments earlier must not silently swallow the rollout.
    """
    token = _token()
    repo = os.environ.get(GITHUB_REPO_ENV_VAR, "").strip()
    if token is None or not repo or "/" not in repo:
        return False

    global _last_dispatch
    with _lock:
        now = time.monotonic()
        if not update_vm and _last_dispatch is not None and now - _last_dispatch < DEBOUNCE_SECONDS:
            logger.debug("sync dispatch debounced (%s)", reason)
            return False
        # Claim the slot inside the lock: concurrent requests race to one dispatch.
        _last_dispatch = now

    url = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    inputs = dict(DISPATCH_INPUTS, update_vm="true") if update_vm else DISPATCH_INPUTS
    body = json.dumps({"ref": "main", "inputs": inputs}).encode()
    try:
        status = (http_post or _post)(url, headers, body)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        logger.warning("sync dispatch failed (%s): %s — the local change stands", reason, exc)
        with _lock:
            _last_dispatch = None  # let the next edit retry immediately
        return False
    if status != 204:
        logger.warning(
            "sync dispatch returned HTTP %s (%s) — the local change stands", status, reason
        )
        with _lock:
            _last_dispatch = None
        return False
    logger.info("sync workflow dispatched (%s)", reason)
    return True


def reset_debounce() -> None:
    """Testing/ops hook: forget the last dispatch time."""
    global _last_dispatch
    with _lock:
        _last_dispatch = None


def _token() -> str | None:
    token_file = os.environ.get(SYNC_TOKEN_FILE_ENV_VAR, "").strip()
    if not token_file:
        return None
    try:
        token = Path(token_file).read_text().strip()
    except OSError:
        logger.warning("sync token file %s is unreadable; sync stays off", token_file)
        return None
    return token or None


def _post(url: str, headers: dict[str, str], body: bytes) -> int:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed https URL
        return int(response.status)
