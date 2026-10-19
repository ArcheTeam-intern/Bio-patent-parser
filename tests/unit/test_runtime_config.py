"""K01: unified, persistent backend runtime configuration.

Acceptance surfaces:

- resolution priority is unambiguous: process env wins, file fills
  missing keys, defaults fill everything else;
- the path passed to ``--env-file`` is resolved to an absolute path at
  load time (a subsequent ``chdir`` cannot change which file is read);
- blank/whitespace-only values count as "not configured";
- a missing file raises (no silent default);
- a process env override of a file value is recorded in the audit
  trail;
- a child process spawned after ``apply_to_environ`` sees the resolved
  configuration (the supervised comparison child relies on this);
- ``to_public_dict()`` / ``status()`` / ``repr`` never leak the token,
  even if a future field accidentally shares a SECRET_ENV_VARS prefix.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bio_pdf.runtime_config import (
    BACKEND_ENV_VARS,
    SECRET_ENV_VARS,
    RuntimeConfig,
    apply_to_environ,
    get_runtime_config,
    load_runtime_config,
    parse_env_file,
    set_runtime_config,
)


@pytest.fixture(autouse=True)
def _reset_runtime_cache():
    """Every test starts with a clean module-level cache."""

    set_runtime_config(RuntimeConfig())
    yield
    set_runtime_config(RuntimeConfig())


def _write_env(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_env_file_strips_comments_and_blanks(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "demo.env",
        "\n".join(
            [
                "# leading comment",
                "",
                "MINERU_TOKEN=tok-123  # inline comment? no, value has hash",
                "MINERU_BASE_URL = \"https://mineru.example/api/v4\"",
                "export BIO_PDF_DATA_DIR=data/post-m1-demo",
                "MINERU_MODEL_VERSION =vlm",
            ]
        ),
    )
    parsed = parse_env_file(env_file)
    assert parsed["MINERU_TOKEN"] == "tok-123  # inline comment? no, value has hash"
    assert parsed["MINERU_BASE_URL"] == "https://mineru.example/api/v4"
    assert parsed["BIO_PDF_DATA_DIR"] == "data/post-m1-demo"
    assert parsed["MINERU_MODEL_VERSION"] == "vlm"


def test_parse_env_file_rejects_unknown_keys(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "bad.env", "NOT_A_REAL_KEY=value\n")
    with pytest.raises(ValueError, match="unknown variable"):
        parse_env_file(env_file)


def test_parse_env_file_rejects_duplicates(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "dup.env", "MINERU_TOKEN=first\nMINERU_TOKEN=second\n"
    )
    with pytest.raises(ValueError, match="duplicate key"):
        parse_env_file(env_file)


def test_load_runtime_config_resolves_relative_paths_to_absolute(tmp_path: Path) -> None:
    env_file = tmp_path / "relative.env"
    env_file.write_text("MINERU_TOKEN=from-cwd-relative\n", encoding="utf-8")
    # NOTE: chdir to a different directory to PROVE the path was resolved
    # before reading.
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    cwd_before = Path.cwd()
    try:
        os.chdir(other_dir)
        config = load_runtime_config(env_file=Path("..") / "relative.env")
    finally:
        os.chdir(cwd_before)
    assert config.mineru_token == "from-cwd-relative"
    assert config.source.env_file == str(env_file.resolve())


def test_load_runtime_config_priority_process_env_wins(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "pri.env",
        "MINERU_TOKEN=from-file\nMINERU_BASE_URL=https://file.example/api\n",
    )
    config = load_runtime_config(
        env_file=env_file,
        environ={"MINERU_TOKEN": "from-env", "OTHER": "ignored"},
    )
    assert config.mineru_token == "from-env"
    assert config.mineru_base_url == "https://file.example/api"
    assert "MINERU_TOKEN" in config.source.overridden_by_env
    assert "MINERU_BASE_URL" in config.source.loaded_from_file


def test_blank_value_is_treated_as_unset(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "blank.env",
        "MINERU_TOKEN=   \nBIO_PDF_CORS_ORIGINS=\n",
    )
    config = load_runtime_config(env_file=env_file)
    assert config.mineru_configured() is False
    # ``configured`` is NOT ``authenticated``: no remote check happened.
    status = config.status()
    assert status["configured"] == {"MINERU_TOKEN": False}
    # Secrets are reported as ``configured`` bools only — never as a
    # public ``missing`` entry (the public surface would otherwise leak
    # the fact that we look for a token at all).
    assert "MINERU_TOKEN" not in status["missing"]
    # A blank value in a NON-secret variable counts as "not configured"
    # and shows up in ``missing`` — this is how a user knows the file's
    # ``KEY=`` line left the variable effectively empty.
    assert "BIO_PDF_CORS_ORIGINS" in status["missing"]


def test_missing_file_raises_with_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="--env-file not found"):
        load_runtime_config(env_file=tmp_path / "absent.env")


def test_public_dict_and_repr_never_leak_token(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "secret.env",
        "MINERU_TOKEN=top-secret-do-not-leak\n",
    )
    config = load_runtime_config(env_file=env_file)
    # The string "top-secret" must appear NOWHERE in the public surface.
    public_blob = json.dumps(
        [config.to_public_dict(), config.status(), config.as_log_dict(), repr(config)],
        ensure_ascii=False,
    )
    assert "top-secret" not in public_blob
    # ``configured`` is a boolean — no value leakage even on secrets.
    assert config.to_public_dict()["mineru_token_configured"] is True


def test_apply_to_environ_only_fills_missing_keys(tmp_path: Path, monkeypatch) -> None:
    # ``apply_to_environ`` checks the LIVE os.environ (not the explicit
    # ``environ`` arg) for the missing-ness rule. Simulate the parent
    # process already owning the token via monkeypatch.
    monkeypatch.setenv("MINERU_TOKEN", "process-env")
    config = load_runtime_config(env_file=None)
    target: dict[str, str] = {}
    written = apply_to_environ(config, environ=target)
    # ``MINERU_TOKEN`` is process-env-set → must NOT be written again.
    assert "MINERU_TOKEN" not in written
    # Other defaults that have a non-empty value (e.g. base URL, sweep
    # seconds) are mirrored so a spawned child sees them too.
    assert "MINERU_BASE_URL" in written
    assert target["MINERU_BASE_URL"] == "https://mineru.net/api/v4"


def test_apply_to_environ_mirrors_resolved_values(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "mir.env",
        "MINERU_TOKEN=tok\nMINERU_BASE_URL=https://x/api\n",
    )
    config = load_runtime_config(env_file=env_file)
    target: dict[str, str] = {}
    written = apply_to_environ(config, environ=target)
    assert "MINERU_TOKEN" in written
    assert "MINERU_BASE_URL" in written
    assert target["MINERU_TOKEN"] == "tok"
    assert target["MINERU_BASE_URL"] == "https://x/api"


def test_child_process_inherits_resolved_environment(tmp_path: Path) -> None:
    """The comparison child is spawned AFTER ``apply_to_environ`` runs in
    the parent — so it must see the same resolved values, not whatever
    existed in the terminal that launched the API.
    """

    env_file = _write_env(
        tmp_path / "child.env",
        "MINERU_TOKEN=inherited-tok\n",
    )
    config = load_runtime_config(env_file=env_file)
    target: dict[str, str] = {}
    apply_to_environ(config, environ=target)
    # Spawn a child that prints the env value; the parent must NOT have
    # set it directly. This is the supervised-child contract K01 fixes.
    script = (
        "import os, sys\n"
        "sys.stdout.write(os.environ.get('MINERU_TOKEN', ''))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, **target},
        check=True,
    )
    assert result.stdout == "inherited-tok"


def test_get_runtime_config_returns_cached_or_default(tmp_path: Path) -> None:
    sentinel = load_runtime_config(env_file=None)
    # Cache reflects the most recent load_runtime_config call.
    assert get_runtime_config().mineru_base_url == sentinel.mineru_base_url


def test_secret_env_vars_are_listed_in_status_but_only_as_bool(tmp_path: Path) -> None:
    config = load_runtime_config(
        env_file=None, environ={"MINERU_TOKEN": "X"}
    )
    status = config.status()
    assert set(status["configured"].keys()) == set(SECRET_ENV_VARS)
    for var in SECRET_ENV_VARS:
        assert var not in status["present"]
        assert var not in status["missing"]


def test_unknown_variable_is_rejected_at_load_time(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "typo.env",
        "MINERU_TOKE=oops\n",  # missing trailing N — must be rejected
    )
    with pytest.raises(ValueError, match="unknown variable"):
        load_runtime_config(env_file=env_file)


def test_default_values_fill_everything_when_nothing_is_set(tmp_path: Path) -> None:
    config = load_runtime_config(env_file=None, environ={})
    assert config.mineru_base_url == "https://mineru.net/api/v4"
    assert config.mineru_model_version == "vlm"
    assert config.comparison_run_mode == "process"
    assert config.max_upload_bytes == 100 * 1024 * 1024
    assert config.comparison_sweep_seconds == 15.0


def test_env_file_with_quoted_value_handles_escapes(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path / "quoted.env",
        'MINERU_BASE_URL="https://example.com/api?key=val&x=\\"q\\""\n',
    )
    parsed = parse_env_file(env_file)
    # Backslash-quote inside the quoted value escapes to a single quote
    # in the resolved string (POSIX-ish env-file semantics).
    assert parsed["MINERU_BASE_URL"].startswith("https://example.com/api?key=val&x=")
    assert '"q"' in parsed["MINERU_BASE_URL"]


def test_corner_case_only_whitespace_in_value_is_unset(tmp_path: Path) -> None:
    """Whitespace-only values must NOT count as configured (K01 contract)."""

    env_file = _write_env(
        tmp_path / "ws.env",
        "MINERU_TOKEN=\t  \n",
    )
    config = load_runtime_config(env_file=env_file)
    assert config.mineru_configured() is False
