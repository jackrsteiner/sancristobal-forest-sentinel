"""The dispatch-triggered repo sync (Slice 7 bead 7.3, #136)."""

from pathlib import Path
from typing import Any

import pytest

from forest_sentinel.dispatch import (
    GITHUB_REPO_ENV_VAR,
    SYNC_TOKEN_FILE_ENV_VAR,
    request_sync,
    reset_debounce,
)


@pytest.fixture(autouse=True)
def _fresh_debounce() -> None:
    reset_debounce()


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, token: str = "tok-123") -> Path:
    token_file = tmp_path / "dispatch-token"
    token_file.write_text(token + "\n")
    monkeypatch.setenv(SYNC_TOKEN_FILE_ENV_VAR, str(token_file))
    monkeypatch.setenv(GITHUB_REPO_ENV_VAR, "owner/instance-repo")
    return token_file


def test_absent_token_is_a_silent_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(SYNC_TOKEN_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv(GITHUB_REPO_ENV_VAR, "owner/repo")
    calls: list[Any] = []

    def post(url: str, headers: dict[str, str], body: bytes) -> int:
        calls.append(url)
        return 204

    assert request_sync(reason="test", http_post=post) is False
    assert calls == []

    # Configured path but missing file: still off, still silent.
    monkeypatch.setenv(SYNC_TOKEN_FILE_ENV_VAR, str(tmp_path / "missing"))
    assert request_sync(reason="test", http_post=post) is False
    assert calls == []


def test_missing_repo_is_a_silent_no_op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure(monkeypatch, tmp_path)
    monkeypatch.delenv(GITHUB_REPO_ENV_VAR, raising=False)
    assert request_sync(reason="test", http_post=lambda *a: 204) is False


def test_dispatch_payload_targets_the_sync_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}

    def post(url: str, headers: dict[str, str], body: bytes) -> int:
        seen.update(url=url, headers=headers, body=body)
        return 204

    assert request_sync(reason="settings-edit", http_post=post) is True
    assert seen["url"] == (
        "https://api.github.com/repos/owner/instance-repo/actions/workflows/"
        "update-instance.yml/dispatches"
    )
    assert seen["headers"]["Authorization"] == "Bearer tok-123"
    import json

    payload = json.loads(seen["body"])
    assert payload["ref"] == "main"
    assert payload["inputs"] == {
        "sync_upstream": "false",
        "sync_aois": "true",
        "sync_settings": "true",
        "update_vm": "false",
    }


def test_bursts_are_debounced_to_one_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    calls: list[str] = []

    def post(url: str, headers: dict[str, str], body: bytes) -> int:
        calls.append(url)
        return 204

    assert request_sync(reason="edit-1", http_post=post) is True
    assert request_sync(reason="edit-2", http_post=post) is False
    assert request_sync(reason="edit-3", http_post=post) is False
    assert len(calls) == 1


def test_failure_is_swallowed_and_frees_the_debounce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)

    def failing(url: str, headers: dict[str, str], body: bytes) -> int:
        raise OSError("network down")

    assert request_sync(reason="edit", http_post=failing) is False
    # The failed attempt must not consume the debounce window.
    assert request_sync(reason="retry", http_post=lambda *a: 204) is True


def test_non_204_is_reported_as_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configure(monkeypatch, tmp_path)
    assert request_sync(reason="edit", http_post=lambda *a: 401) is False
    assert request_sync(reason="retry", http_post=lambda *a: 204) is True


def test_update_instance_workflow_has_the_sync_settings_contract() -> None:
    """Workflow contract (mirrors the vm_setup contract tests): the dispatch
    target exists, syncs overrides.env, and never pushes in parallel."""
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "update-instance.yml"
    ).read_text()
    assert "sync_settings:" in workflow
    assert "config/overrides.env" in workflow
    # Serialized pushes: settings syncs after the AOI sync, and the VM update
    # waits for both.
    assert "needs: [sync, sync-aois]" in workflow
    assert "needs: [sync, sync-aois, sync-settings]" in workflow


def test_update_vm_dispatch_escalates_and_bypasses_debounce(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bead 7.5 (#139): unit-rendered settings roll the VM, and a recent plain
    sync must not swallow the rollout."""
    import json

    _configure(monkeypatch, tmp_path)
    payloads: list[dict[str, Any]] = []

    def post(url: str, headers: dict[str, str], body: bytes) -> int:
        payloads.append(json.loads(body))
        return 204

    assert request_sync(reason="aoi-upload", http_post=post) is True
    # Plain follow-up is debounced; the rollout is not.
    assert request_sync(reason="settings-edit", http_post=post) is False
    assert request_sync(reason="settings-edit", update_vm=True, http_post=post) is True
    assert payloads[0]["inputs"]["update_vm"] == "false"
    assert payloads[1]["inputs"]["update_vm"] == "true"
