"""Versioned local model profiles loader (T050a, OC-1 section 3, D33).

Reads ``radar_v08/model_profiles.toml`` with the stdlib ``tomllib`` (TECHNOLOGY.md S2)
from a file opened in binary mode, validates every profile and returns
``workflow.worker.InferenceProfile`` values. It fails closed: any problem raises a
typed ``ModelProfileError`` for the whole file, never a silent fallback to a default.

Rules:

* The file is read once, in ``"rb"`` mode, and never written; no socket is opened
  (the endpoint is only checked as text by ``local_inference.loopback_base_url``).
* Top level: exactly ``schema_version`` (1), ``default_profile`` and ``profiles``.
  Each profile has exactly the keys in ``_PROFILE_KEYS``; unknown or missing keys,
  wrong types (``bool`` is never an integer, a float never an integer), duplicate
  ids and an unknown or disabled default are refused.
* ``role`` is a ``workflow.scheduler.Role``. Roles disabled in OC-1 production
  (Challenger, Deep Analyst) may only appear with ``enabled = false``; such a profile
  is validated but cannot be resolved into an ``InferenceProfile``.
* Limits come from OC-1 (``scheduler.OC1_ROLE_PROFILES`` plus the section 3 context
  totals): ``1 <= hard_timeout_seconds <= role limit``, ``1 <= output_cap_tokens <=
  role cap``, ``context_tokens <= role context total`` and ``context_tokens -
  output_cap_tokens >= role max input`` (the role's input budget must still fit).
* ``output_format`` is a closed enum (``json_schema`` only: structured output through
  Ollama ``format``, never free text). ``temperature`` must be 0 and ``think`` false.
* Resources: ``reserve_vram_gib >= 1.5`` and ``reserve_ram_gib >= 4`` (OC-1 section 3).
* ``endpoint`` must pass ``loopback_base_url`` (plain ``http`` to ``localhost`` or
  ``127.0.0.1`` with an explicit port). ``model`` must be a local Ollama name with an
  explicit tag, no registry host, and no cloud tag (``cloud`` or ``*-cloud``), because
  an Ollama cloud model is forwarded to a remote service by the local daemon.

``apply_overrides`` (T050c) applies raw override text (read by the caller) to an enabled
profile through the same rules (model name, role timeout limit, temperature 0, loopback
endpoint) and fails closed the same way; ``radar_v08/config.py`` uses it for
``RADAR_QWEN_*`` and ``RADAR_OLLAMA_URL``.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from ..workflow.scheduler import (
    OC1_ROLE_PROFILES,
    PRODUCTION_DISABLED_ROLES,
    Role,
    RoleProfile,
)
from ..workflow.worker import InferenceProfile, WorkerConfigError
from .local_inference import LocalEndpointRefused, loopback_base_url

DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent.parent / "model_profiles.toml"
SCHEMA_VERSION = 1
MAX_FILE_BYTES = 65_536
MIN_RESERVE_VRAM_GIB = 1.5
MIN_RESERVE_RAM_GIB = 4.0

# OC-1 section 3 table: total context window per role (input budget + output cap).
OC1_ROLE_CONTEXT_TOKENS: Mapping[Role, int] = MappingProxyType(
    {
        Role.SCREENER: 4096,
        Role.CHALLENGER: 8192,
        Role.DEEP_ANALYST: 8192,
    }
)

_TOP_LEVEL_KEYS = frozenset({"schema_version", "default_profile", "profiles"})
_PROFILE_KEYS = frozenset(
    {
        "id",
        "role",
        "enabled",
        "model",
        "endpoint",
        "output_format",
        "hard_timeout_seconds",
        "context_tokens",
        "output_cap_tokens",
        "temperature",
        "think",
        "reserve_vram_gib",
        "reserve_ram_gib",
    }
)
_PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# Optional single namespace without dots (so no registry host), name, explicit tag.
_MODEL_NAME = re.compile(r"(?:[a-z0-9][a-z0-9_-]{0,63}/)?[a-z0-9][a-z0-9._-]{0,127}:[a-z0-9][a-z0-9._-]{0,127}")


class OutputFormat(Enum):
    JSON_SCHEMA = "json_schema"


class ProfileErrorCode(Enum):
    FILE_MISSING = "file_missing"
    FILE_UNREADABLE = "file_unreadable"
    FILE_TOO_LARGE = "file_too_large"
    INVALID_TOML = "invalid_toml"
    UNKNOWN_KEY = "unknown_key"
    MISSING_KEY = "missing_key"
    WRONG_TYPE = "wrong_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    NO_PROFILES = "no_profiles"
    INVALID_ID = "invalid_id"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_ROLE = "unknown_role"
    ROLE_DISABLED_IN_OC1 = "role_disabled_in_oc1"
    UNKNOWN_OUTPUT_FORMAT = "unknown_output_format"
    TIMEOUT_OUT_OF_LIMITS = "timeout_out_of_limits"
    OUTPUT_CAP_OUT_OF_LIMITS = "output_cap_out_of_limits"
    CONTEXT_OUT_OF_LIMITS = "context_out_of_limits"
    TEMPERATURE_NOT_ZERO = "temperature_not_zero"
    THINK_ENABLED = "think_enabled"
    RESOURCE_RESERVE_TOO_LOW = "resource_reserve_too_low"
    ENDPOINT_REFUSED = "endpoint_refused"
    INVALID_MODEL = "invalid_model"
    CLOUD_MODEL = "cloud_model"
    DEFAULT_NOT_FOUND = "default_not_found"
    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_DISABLED = "profile_disabled"
    WORKER_REJECTED = "worker_rejected"


class ModelProfileError(ValueError):
    """The profiles file (or a requested profile) is not usable. Nothing is returned."""

    def __init__(self, code: ProfileErrorCode, where: str, detail: str = "") -> None:
        message = f"model profiles refused: {code.value} at {where}"
        super().__init__(f"{message}: {detail}" if detail else message)
        self.code = code
        self.where = where


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """One validated profile. ``inference`` is ``None`` exactly when ``enabled`` is false."""

    profile_id: str
    role: Role
    enabled: bool
    model: str
    endpoint: str
    output_format: OutputFormat
    hard_timeout_seconds: int
    context_tokens: int
    output_cap_tokens: int
    temperature: float
    think: bool
    reserve_vram_gib: float
    reserve_ram_gib: float
    inference: InferenceProfile | None

    def inference_profile(self) -> InferenceProfile:
        if self.inference is None:
            raise ModelProfileError(ProfileErrorCode.PROFILE_DISABLED, self.profile_id)
        return self.inference


@dataclass(frozen=True, slots=True)
class ModelProfiles:
    schema_version: int
    default_profile_id: str
    profiles: Mapping[str, ModelProfile]

    def get(self, profile_id: str) -> ModelProfile:
        profile = self.profiles.get(profile_id)
        if profile is None:
            raise ModelProfileError(ProfileErrorCode.PROFILE_NOT_FOUND, repr(profile_id))
        return profile

    def resolve(self, profile_id: str | None = None) -> InferenceProfile:
        """The ``InferenceProfile`` for ``profile_id`` (default profile when ``None``)."""
        return self.get(self.default_profile_id if profile_id is None else profile_id).inference_profile()


def _read_toml(path: str | os.PathLike[str]) -> dict[str, object]:
    where = os.fspath(path)
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_FILE_BYTES:
                raise ModelProfileError(ProfileErrorCode.FILE_TOO_LARGE, where, f"over {MAX_FILE_BYTES} bytes")
            return tomllib.load(handle)
    except FileNotFoundError:
        raise ModelProfileError(ProfileErrorCode.FILE_MISSING, where) from None
    except tomllib.TOMLDecodeError as error:
        raise ModelProfileError(ProfileErrorCode.INVALID_TOML, where, str(error)) from None
    except UnicodeDecodeError:
        raise ModelProfileError(ProfileErrorCode.INVALID_TOML, where, "not UTF-8") from None
    except OSError as error:
        raise ModelProfileError(ProfileErrorCode.FILE_UNREADABLE, where, type(error).__name__) from None


def _exact_keys(table: Mapping[str, object], expected: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - expected)
    if unknown:
        raise ModelProfileError(ProfileErrorCode.UNKNOWN_KEY, where, ", ".join(unknown))
    missing = sorted(expected - set(table))
    if missing:
        raise ModelProfileError(ProfileErrorCode.MISSING_KEY, where, ", ".join(missing))


def _str(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a string")
    return value


def _int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected an integer")
    return value


def _bool(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a boolean")
    return value


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a finite number")
    return number


def _role(value: object, where: str) -> Role:
    text = _str(value, where)
    for role in Role:
        if role.value == text:
            return role
    raise ModelProfileError(ProfileErrorCode.UNKNOWN_ROLE, where, repr(text))


def _output_format(value: object, where: str) -> OutputFormat:
    text = _str(value, where)
    for output_format in OutputFormat:
        if output_format.value == text:
            return output_format
    raise ModelProfileError(ProfileErrorCode.UNKNOWN_OUTPUT_FORMAT, where, repr(text))


def _model(value: object, where: str) -> str:
    text = _str(value, where)
    if _MODEL_NAME.fullmatch(text) is None:
        raise ModelProfileError(ProfileErrorCode.INVALID_MODEL, where, repr(text))
    tag = text.rsplit(":", 1)[1]
    if tag == "cloud" or tag.endswith("-cloud"):
        raise ModelProfileError(ProfileErrorCode.CLOUD_MODEL, where, repr(text))
    return text


def _endpoint(value: object, where: str) -> str:
    text = _str(value, where)
    try:
        return loopback_base_url(text)
    except LocalEndpointRefused as error:
        raise ModelProfileError(ProfileErrorCode.ENDPOINT_REFUSED, where, error.code.value) from None


def _check_limits(
    role: Role, limits: RoleProfile, timeout: int, context: int, output_cap: int, where: str
) -> None:
    if not 1 <= timeout <= limits.hard_timeout_seconds:
        raise ModelProfileError(
            ProfileErrorCode.TIMEOUT_OUT_OF_LIMITS, where, f"1..{limits.hard_timeout_seconds} for {role.value}"
        )
    if not 1 <= output_cap <= limits.output_cap_tokens:
        raise ModelProfileError(
            ProfileErrorCode.OUTPUT_CAP_OUT_OF_LIMITS, where, f"1..{limits.output_cap_tokens} for {role.value}"
        )
    context_total = OC1_ROLE_CONTEXT_TOKENS[role]
    if context > context_total or context - output_cap < limits.max_input_tokens:
        raise ModelProfileError(
            ProfileErrorCode.CONTEXT_OUT_OF_LIMITS,
            where,
            f"<= {context_total} and >= output cap + {limits.max_input_tokens} for {role.value}",
        )


def _profile(table: object, index: int) -> ModelProfile:
    where = f"profiles[{index}]"
    if not isinstance(table, dict):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a table")
    _exact_keys(table, _PROFILE_KEYS, where)
    profile_id = _str(table["id"], f"{where}.id")
    if _PROFILE_ID.fullmatch(profile_id) is None:
        raise ModelProfileError(ProfileErrorCode.INVALID_ID, f"{where}.id", repr(profile_id))
    where = f"profile {profile_id!r}"
    role = _role(table["role"], f"{where}.role")
    enabled = _bool(table["enabled"], f"{where}.enabled")
    if enabled and role in PRODUCTION_DISABLED_ROLES:
        raise ModelProfileError(ProfileErrorCode.ROLE_DISABLED_IN_OC1, f"{where}.enabled", role.value)
    model = _model(table["model"], f"{where}.model")
    endpoint = _endpoint(table["endpoint"], f"{where}.endpoint")
    output_format = _output_format(table["output_format"], f"{where}.output_format")
    timeout = _int(table["hard_timeout_seconds"], f"{where}.hard_timeout_seconds")
    context = _int(table["context_tokens"], f"{where}.context_tokens")
    output_cap = _int(table["output_cap_tokens"], f"{where}.output_cap_tokens")
    _check_limits(role, OC1_ROLE_PROFILES[role], timeout, context, output_cap, where)
    temperature = _number(table["temperature"], f"{where}.temperature")
    if temperature != 0.0:
        raise ModelProfileError(ProfileErrorCode.TEMPERATURE_NOT_ZERO, f"{where}.temperature")
    think = _bool(table["think"], f"{where}.think")
    if think:
        raise ModelProfileError(ProfileErrorCode.THINK_ENABLED, f"{where}.think")
    reserve_vram = _number(table["reserve_vram_gib"], f"{where}.reserve_vram_gib")
    if reserve_vram < MIN_RESERVE_VRAM_GIB:
        raise ModelProfileError(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, f"{where}.reserve_vram_gib")
    reserve_ram = _number(table["reserve_ram_gib"], f"{where}.reserve_ram_gib")
    if reserve_ram < MIN_RESERVE_RAM_GIB:
        raise ModelProfileError(ProfileErrorCode.RESOURCE_RESERVE_TOO_LOW, f"{where}.reserve_ram_gib")
    inference: InferenceProfile | None = None
    if enabled:
        try:
            inference = InferenceProfile(
                profile_id=profile_id,
                model=model,
                role=role,
                hard_timeout_seconds=timeout,
                context_tokens=context,
                output_cap_tokens=output_cap,
                think=think,
            )
        except WorkerConfigError as error:
            raise ModelProfileError(ProfileErrorCode.WORKER_REJECTED, where, str(error)) from None
    return ModelProfile(
        profile_id=profile_id,
        role=role,
        enabled=enabled,
        model=model,
        endpoint=endpoint,
        output_format=output_format,
        hard_timeout_seconds=timeout,
        context_tokens=context,
        output_cap_tokens=output_cap,
        temperature=temperature,
        think=think,
        reserve_vram_gib=reserve_vram,
        reserve_ram_gib=reserve_ram,
        inference=inference,
    )


def load_model_profiles(path: str | os.PathLike[str] = DEFAULT_PROFILES_PATH) -> ModelProfiles:
    """Load and validate every profile in ``path``; raise ``ModelProfileError`` on any problem."""
    document = _read_toml(path)
    _exact_keys(document, _TOP_LEVEL_KEYS, "top level")
    version = _int(document["schema_version"], "schema_version")
    if version != SCHEMA_VERSION:
        raise ModelProfileError(ProfileErrorCode.UNSUPPORTED_SCHEMA_VERSION, "schema_version", str(version))
    default_id = _str(document["default_profile"], "default_profile")
    tables = document["profiles"]
    if not isinstance(tables, list):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, "profiles", "expected an array of tables")
    if not tables:
        raise ModelProfileError(ProfileErrorCode.NO_PROFILES, "profiles")
    profiles: dict[str, ModelProfile] = {}
    for index, table in enumerate(tables):
        profile = _profile(table, index)
        if profile.profile_id in profiles:
            raise ModelProfileError(ProfileErrorCode.DUPLICATE_ID, f"profiles[{index}].id", repr(profile.profile_id))
        profiles[profile.profile_id] = profile
    default = profiles.get(default_id)
    if default is None:
        raise ModelProfileError(ProfileErrorCode.DEFAULT_NOT_FOUND, "default_profile", repr(default_id))
    if not default.enabled:
        raise ModelProfileError(ProfileErrorCode.PROFILE_DISABLED, "default_profile", repr(default_id))
    return ModelProfiles(
        schema_version=version,
        default_profile_id=default_id,
        profiles=MappingProxyType(profiles),
    )


def load_inference_profile(
    path: str | os.PathLike[str] = DEFAULT_PROFILES_PATH, profile_id: str | None = None
) -> InferenceProfile:
    """Load ``path`` and return the ``InferenceProfile`` for ``profile_id`` (default when ``None``)."""
    return load_model_profiles(path).resolve(profile_id)


def _override_number(text: str, where: str) -> float:
    try:
        number = float(text)
    except ValueError:
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a number") from None
    if not math.isfinite(number):
        raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected a finite number")
    return number


def apply_overrides(
    profile: ModelProfile,
    *,
    model: str | None = None,
    hard_timeout_seconds: str | None = None,
    temperature: str | None = None,
    endpoint: str | None = None,
) -> ModelProfile:
    """Return ``profile`` with raw text overrides applied, each validated by the file's own rules.

    The overrides are raw text read by the caller (``None`` = not set). They pass exactly the checks a
    value in the file passes: explicit tag and no cloud tag for ``model``, whole seconds inside
    the role's OC-1 limit for ``hard_timeout_seconds``, ``0`` for ``temperature``, loopback
    ``http`` with an explicit port for ``endpoint``. Any failure raises ``ModelProfileError``;
    nothing is clamped or defaulted. The profile must be enabled.
    """
    if profile.inference is None:
        raise ModelProfileError(ProfileErrorCode.PROFILE_DISABLED, profile.profile_id)
    new_model = profile.model if model is None else _model(model, "RADAR_QWEN_MODEL")
    new_timeout = profile.hard_timeout_seconds
    if hard_timeout_seconds is not None:
        where = "RADAR_QWEN_TIMEOUT_SECONDS"
        seconds = _override_number(hard_timeout_seconds, where)
        if not seconds.is_integer():
            raise ModelProfileError(ProfileErrorCode.WRONG_TYPE, where, "expected whole seconds")
        new_timeout = int(seconds)
        _check_limits(
            profile.role,
            OC1_ROLE_PROFILES[profile.role],
            new_timeout,
            profile.context_tokens,
            profile.output_cap_tokens,
            where,
        )
    new_temperature = profile.temperature
    if temperature is not None:
        new_temperature = _override_number(temperature, "RADAR_QWEN_TEMPERATURE")
        if new_temperature != 0.0:
            raise ModelProfileError(ProfileErrorCode.TEMPERATURE_NOT_ZERO, "RADAR_QWEN_TEMPERATURE")
    new_endpoint = profile.endpoint if endpoint is None else _endpoint(endpoint, "RADAR_OLLAMA_URL")
    try:
        inference = replace(profile.inference, model=new_model, hard_timeout_seconds=new_timeout)
    except WorkerConfigError as error:
        raise ModelProfileError(ProfileErrorCode.WORKER_REJECTED, profile.profile_id, str(error)) from None
    return replace(
        profile,
        model=new_model,
        hard_timeout_seconds=new_timeout,
        temperature=new_temperature,
        endpoint=new_endpoint,
        inference=inference,
    )
