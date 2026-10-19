"""Unified backend runtime configuration loader (K01).

Goals:

- A SINGLE, explicit configuration entry point for both the API and the
  queue worker (and any supervised child they spawn). The entry point is
  ``load_runtime_config()``; the API, the worker, the CLI ``serve`` command
  and the comparison child process all consume the same resolved object.
- Resolution priority is unambiguous: explicit process environment > values
  read from an explicit ``--env-file`` > built-in defaults. The file is
  parsed with a STRICT, MINIMAL syntax (``KEY=VALUE`` lines, blank lines
  and ``#`` comments), never as Python / shell / dotenv-ish execution.
- The path passed to ``--env-file`` is resolved to an ABSOLUTE path at
  load time so a subsequent ``chdir`` cannot change which file is read.
- Public API exposes only ``configured`` / ``config_source`` / the list of
  MISSING variable names. No secret values are ever returned through
  ``__repr__`` / ``to_public_dict()`` / ``as_log_dict()``.

This module deliberately stays small: it knows the SET of variables the
backend needs (the ``BACKEND_ENV_VARS`` tuple) and a single file syntax. It
does NOT replace the workflow TOML config (``bio_pdf.config``) — that one
describes per-job policies and stays unchanged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, MutableMapping


#: The set of variables the backend resolves from the runtime environment.
#: Keep this list EXPLICIT — anything not listed here cannot be loaded from
#: a file by this module. Adding a new key here is the way to introduce a
#: new tunable: the corresponding dataclass field on :class:`RuntimeConfig`
#: must exist and the ``_public_status()`` masking must list it.
BACKEND_ENV_VARS: tuple[str, ...] = (
    # MinerU cloud adapter
    "MINERU_TOKEN",
    "MINERU_BASE_URL",
    "MINERU_MODEL_VERSION",
    # Comparison runtime
    "BIO_PDF_COMPARISON_RUN_MODE",
    # API plumbing
    "BIO_PDF_DATA_DIR",
    "BIO_PDF_SAMPLES_DIR",
    "BIO_PDF_WEB_DIST",
    "BIO_PDF_CORS_ORIGINS",
    "BIO_PDF_MAX_UPLOAD_BYTES",
    "BIO_PDF_COMPARISON_SWEEP_SECONDS",
)

#: Variables whose presence counts as "configured" but the value is a
#: SECRET and must NEVER leak through public status, repr, or log dumps.
SECRET_ENV_VARS: frozenset[str] = frozenset({"MINERU_TOKEN"})

#: Minimal ``KEY=VALUE`` line — keys are identifiers (``[A-Za-z_][A0-Z0-9_]*``)
#: and values may be quoted with double quotes (escaped ``\\`` and ``\"``),
#: or left unquoted with leading/trailing whitespace stripped. No shell
#: expansion, no Python interpolation, no ``export`` keyword.
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


@dataclass(frozen=True)
class RuntimeConfigSource:
    """How the configuration was assembled (K01 transparency)."""

    #: Path that was actually read (absolute, after ``--env-file`` resolution).
    #: ``None`` when no file was requested.
    env_file: str | None = None
    #: Names of variables that were LOADED FROM FILE (not process env).
    loaded_from_file: tuple[str, ...] = field(default_factory=tuple)
    #: Names of variables that were OVERRIDDEN BY PROCESS ENV (priority rule).
    overridden_by_env: tuple[str, ...] = field(default_factory=tuple)
    #: Names of variables that had a process env value AND a file value, and
    #: the process env value won. Same as ``overridden_by_env`` but kept
    #: as an explicit label so the audit trail is unambiguous.
    env_priority_hits: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved backend configuration.

    Public-facing consumers (``/api/v1/capabilities`` payload, structured
    logs) read ``to_public_dict()`` / ``status()`` — never the raw fields,
    so a token never leaks even if a future change drops a ``__repr__``.
    """

    #: MinerU cloud token (NEVER printed). Empty string = not configured.
    mineru_token: str = ""
    mineru_base_url: str = "https://mineru.net/api/v4"
    mineru_model_version: str = "vlm"
    comparison_run_mode: str = "process"
    data_dir: str = "data/demo"
    samples_dir: str = "pdf/samples"
    web_dist: str = ""
    cors_origins: tuple[str, ...] = field(default_factory=tuple)
    max_upload_bytes: int = 100 * 1024 * 1024
    comparison_sweep_seconds: float = 15.0
    #: Provenance / audit fields.
    source: RuntimeConfigSource = field(default_factory=RuntimeConfigSource)

    # ---- introspection --------------------------------------------------

    def mineru_configured(self) -> bool:
        """True iff the MinerU token is set AND non-blank.

        ``configured`` is intentionally NOT ``authenticated``: a token that
        parses as non-empty is configured, but the cloud side has not been
        contacted yet. ``/api/v1/capabilities`` reports this and nothing
        else — capability reads never ping the cloud.
        """

        return bool(self.mineru_token and self.mineru_token.strip())

    def status(self) -> dict[str, Any]:
        """Public status payload — never includes secret VALUES.

        Lists WHICH variables were seen and which are MISSING (so a
        misconfiguration is actionable), but only echoes BACKEND_SECRET
        variables as ``"configured": bool``.
        """

        missing: list[str] = []
        present_public: dict[str, str] = {}
        for var in BACKEND_ENV_VARS:
            value = self._raw_value(var)
            if var in SECRET_ENV_VARS:
                continue
            if value == "":
                missing.append(var)
            else:
                present_public[var] = value
        return {
            "config_source": self._config_source_dict(),
            "configured": {
                var: bool(self._raw_value(var).strip())
                for var in SECRET_ENV_VARS
            },
            "present": present_public,
            "missing": missing,
            "note": (
                "configured = non-empty value present in process env (or env file); "
                "no remote validation has been performed"
            ),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """The /api/v1/capabilities-style public payload — secrets masked."""

        payload: dict[str, Any] = {
            "mineru_token_configured": self.mineru_configured(),
            "mineru_base_url": self.mineru_base_url,
            "mineru_model_version": self.mineru_model_version,
            "comparison_run_mode": self.comparison_run_mode,
            "max_upload_bytes": self.max_upload_bytes,
            "comparison_sweep_seconds": self.comparison_sweep_seconds,
            "config_source": self._config_source_dict(),
        }
        # Strip keys that accidentally contain SECRET_ENV_VARS — defense in
        # depth in case a future field name overlaps.
        for secret in SECRET_ENV_VARS:
            payload.pop(secret, None)
            payload.pop(f"{secret.lower()}_configured", None) if (
                f"{secret.lower()}_configured" in payload and secret != "MINERU_TOKEN"
            ) else None
        return payload

    def as_log_dict(self) -> dict[str, Any]:
        """Structured-log view. Same masking rules as ``to_public_dict``."""

        return self.to_public_dict()

    # ---- repr safety ----------------------------------------------------

    def __repr__(self) -> str:  # noqa: D401 — explicit repr for safety
        return (
            f"RuntimeConfig(mineru_token={'<set>' if self.mineru_configured() else '<unset>'}, "
            f"source={self._config_source_dict()!r})"
        )

    # ---- internals ------------------------------------------------------

    def _raw_value(self, name: str) -> str:
        return str(getattr(self, _attr_for(name), "") or "")

    def _config_source_dict(self) -> dict[str, Any]:
        return {
            "env_file": self.source.env_file,
            "loaded_from_file": list(self.source.loaded_from_file),
            "overridden_by_env": list(self.source.overridden_by_env),
        }


def _attr_for(env_name: str) -> str:
    """Map a BACKEND_ENV_VARS entry to the matching RuntimeConfig field."""

    return {
        "MINERU_TOKEN": "mineru_token",
        "MINERU_BASE_URL": "mineru_base_url",
        "MINERU_MODEL_VERSION": "mineru_model_version",
        "BIO_PDF_COMPARISON_RUN_MODE": "comparison_run_mode",
        "BIO_PDF_DATA_DIR": "data_dir",
        "BIO_PDF_SAMPLES_DIR": "samples_dir",
        "BIO_PDF_WEB_DIST": "web_dist",
        "BIO_PDF_CORS_ORIGINS": "cors_origins",
        "BIO_PDF_MAX_UPLOAD_BYTES": "max_upload_bytes",
        "BIO_PDF_COMPARISON_SWEEP_SECONDS": "comparison_sweep_seconds",
    }.get(env_name, env_name.lower())


def _coerce(name: str, raw: str) -> Any:
    """Coerce a file/process value to the typed field expected by the dataclass."""

    if name == "BIO_PDF_MAX_UPLOAD_BYTES":
        try:
            return int(raw.strip())
        except ValueError:
            return 100 * 1024 * 1024
    if name == "BIO_PDF_COMPARISON_SWEEP_SECONDS":
        try:
            return float(raw.strip())
        except ValueError:
            return 15.0
    if name == "BIO_PDF_CORS_ORIGINS":
        return tuple(p.strip() for p in raw.split(",") if p.strip())
    return raw


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file (minimal, deterministic syntax).

    Rules:
    - blank lines and lines whose first non-whitespace char is ``#`` are
      ignored;
    - optional leading ``export`` keyword (POSIX habit) is stripped;
    - values may be wrapped in double quotes with ``\\`` and ``\\"``
      escapes; unquoted values keep everything after the first ``=`` and
      are right-trimmed;
    - backslash at end of line continues to the next line;
    - any KEY outside ``BACKEND_ENV_VARS`` is REJECTED — typos cannot
      silently turn into nothing;
    - duplicate keys raise (a file is allowed to set a value EXACTLY once).
    """

    if not path.is_file():
        raise FileNotFoundError(str(path))
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    pending_key: str | None = None
    pending_value: str = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        if pending_key is not None:
            continuation = line.rstrip()
            if continuation.endswith("\\"):
                pending_value += " " + continuation[:-1]
                continue
            pending_value += " " + continuation
            line_to_parse = f"{pending_key}={pending_value.lstrip()}"
            pending_key = None
            pending_value = ""
        else:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            line_to_parse = line
        match = _LINE_RE.match(line_to_parse)
        if match is None:
            raise ValueError(
                f"env file {path}:{lineno}: invalid line: {line_to_parse!r}"
            )
        key, raw_value = match.group(1), match.group(2)
        if key not in BACKEND_ENV_VARS:
            raise ValueError(
                f"env file {path}:{lineno}: unknown variable {key!r}; "
                f"allowed: {sorted(BACKEND_ENV_VARS)}"
            )
        if pending_key is None:
            tail = raw_value.rstrip()
            if tail.endswith("\\"):
                pending_key = key
                pending_value = tail[:-1]
                continue
            value = tail
        else:  # pragma: no cover — branch handled above
            value = pending_value
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            try:
                value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"env file {path}:{lineno}: bad escape in {key!r}: {exc}"
                ) from exc
        if key in out:
            raise ValueError(
                f"env file {path}:{lineno}: duplicate key {key!r}"
            )
        out[key] = value
    if pending_key is not None:  # pragma: no cover — covered by continuation tests
        raise ValueError(f"env file {path}: trailing continuation for {pending_key!r}")
    return out


def load_runtime_config(
    *,
    env_file: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Resolve the runtime configuration for one process invocation.

    Priority (documented):
        1. explicit process environment (``environ`` / ``os.environ``);
        2. values from the file at ``env_file`` (resolved to absolute);
        3. dataclass defaults.

    The returned object is the SINGLE source of effective configuration
    for the calling process. Children spawned after this call inherit
    the resolved environment because the caller passes ``os.environ``
    through ``spawn`` (Python's ``multiprocessing`` does that already —
    see ``ComparisonService._execute_governed``).
    """

    env_path: Path | None = None
    if env_file is not None:
        env_path = Path(env_file).resolve()
        if not env_path.is_file():
            raise FileNotFoundError(f"--env-file not found: {env_path}")

    process_env: dict[str, str] = (
        dict(environ) if environ is not None else {k: v for k, v in os.environ.items()}
    )

    file_values: dict[str, str] = {}
    if env_path is not None:
        file_values = parse_env_file(env_path)

    # Priority: process env wins; the file fills in keys the process did
    # NOT set. ``overridden_by_env`` records the keys that BOTH sources
    # had and where process env won — it is part of the audit trail so a
    # user can see at a glance that their file value did not take effect.
    effective: dict[str, Any] = {}
    loaded_from_file: list[str] = []
    overridden_by_env: list[str] = []
    for var in BACKEND_ENV_VARS:
        attr = _attr_for(var)
        file_value = file_values.get(var, "")
        env_value = process_env.get(var, "")
        if env_value:
            effective[attr] = _coerce(var, env_value)
            if file_value:
                overridden_by_env.append(var)
        elif file_value:
            effective[attr] = _coerce(var, file_value)
            loaded_from_file.append(var)
    source = RuntimeConfigSource(
        env_file=str(env_path) if env_path else None,
        loaded_from_file=tuple(loaded_from_file),
        overridden_by_env=tuple(overridden_by_env),
        env_priority_hits=tuple(overridden_by_env),
    )
    return set_runtime_config(RuntimeConfig(source=source, **effective))


def apply_to_environ(
    config: RuntimeConfig,
    *,
    environ: MutableMapping[str, str] | None = None,
    only_missing: bool = True,
) -> list[str]:
    """Mirror the resolved config onto ``os.environ`` for child inheritance.

    ``only_missing`` is decided against the LIVE process environment, not
    against the ``environ`` argument — the argument only names WHERE to
    write. That way a test fixture that builds a fresh dict still gets
    the "process env wins" rule applied to the real parent process and
    a supervised child can never see a different value than the parent
    did.

    Returns the names of keys that were written.
    """

    target: MutableMapping[str, str]
    if environ is None:
        target = os.environ
    else:
        target = environ
    written: list[str] = []
    for var in BACKEND_ENV_VARS:
        if only_missing and os.environ.get(var):
            continue  # process env wins — never overwrite
        attr = _attr_for(var)
        value = getattr(config, attr, None)
        if value is None or value == "":
            continue
        if isinstance(value, tuple):
            target[var] = ",".join(value)
        else:
            target[var] = str(value)
        written.append(var)
    return written


__all__ = [
    "BACKEND_ENV_VARS",
    "SECRET_ENV_VARS",
    "RuntimeConfig",
    "RuntimeConfigSource",
    "apply_to_environ",
    "get_runtime_config",
    "load_runtime_config",
    "parse_env_file",
    "set_runtime_config",
]


#: Module-level cache of the LAST loaded config. Populated by
 #: ``load_runtime_config`` / explicitly via ``set_runtime_config`` so
 #: deep code paths (comparison child, queue worker subroutines) read
 #: from a single, audit-trail-bearing object instead of poking at
 #: ``os.environ`` ad-hoc. Empty by default — tests that don't care about
 #: configuration still work because ``get_runtime_config`` falls back to
 #: ``os.environ``-based defaults on cache miss.
_RUNTIME_CONFIG_CACHE: RuntimeConfig | None = None


def set_runtime_config(config: RuntimeConfig) -> RuntimeConfig:
    """Install the resolved config as the process-wide singleton."""

    global _RUNTIME_CONFIG_CACHE
    _RUNTIME_CONFIG_CACHE = config
    return config


def get_runtime_config() -> RuntimeConfig:
    """Return the installed runtime config, or a fresh os.environ-based one."""

    if _RUNTIME_CONFIG_CACHE is not None:
        return _RUNTIME_CONFIG_CACHE
    # Lazy fallback: build a minimal config from the current process env
    # without persisting it (test isolation). The audit trail is empty
    # because no file was requested.
    return load_runtime_config()
