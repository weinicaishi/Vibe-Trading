"""CLI for the side-effecting, privacy-safe real OIDC staging exercise."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from src.market_morning.oidc_staging_probe import (
    OidcStagingProbeError,
    run_oidc_staging_probe,
)
from src.market_morning.release_candidate import (
    ReleaseCandidateError,
    parse_release_candidate_manifest,
)


_MAX_TOKEN_FILE_BYTES = 8193
_MAX_CANDIDATE_BYTES = 256 * 1024
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

OidcProbeClientFactory = Callable[[str], httpx.Client]


class OidcStagingProbeCliError(ValueError):
    """Raised before a live probe when a local input is unsafe."""


def read_oidc_token_file(path: Path) -> str:
    """Read one bearer from an owner-only regular file without following links."""

    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise OidcStagingProbeCliError("oidc_probe_token_file_unreadable") from error
    if not stat.S_ISREG(path_metadata.st_mode) or stat.S_ISLNK(path_metadata.st_mode):
        raise OidcStagingProbeCliError("oidc_probe_token_file_not_regular")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise OidcStagingProbeCliError("oidc_probe_token_file_unreadable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise OidcStagingProbeCliError("oidc_probe_token_file_not_regular")
        if metadata.st_nlink != 1:
            raise OidcStagingProbeCliError("oidc_probe_token_file_links_invalid")
        if metadata.st_mode & 0o077:
            raise OidcStagingProbeCliError("oidc_probe_token_file_permissions_invalid")
        if not 1 <= metadata.st_size <= _MAX_TOKEN_FILE_BYTES:
            raise OidcStagingProbeCliError("oidc_probe_token_file_size_invalid")
        raw_bytes = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1)
        if len(raw_bytes) != metadata.st_size:
            raise OidcStagingProbeCliError("oidc_probe_token_file_size_invalid")
        raw = raw_bytes.decode("utf-8")
    except UnicodeError as error:
        raise OidcStagingProbeCliError("oidc_probe_token_file_unreadable") from error
    finally:
        os.close(descriptor)
    if raw.endswith("\r\n"):
        raw = raw[:-2]
    elif raw.endswith("\n"):
        raw = raw[:-1]
    if not raw or "\n" in raw or "\r" in raw:
        raise OidcStagingProbeCliError("oidc_probe_token_file_invalid")
    return raw


def _read_candidate(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OidcStagingProbeCliError("oidc_probe_release_candidate_not_regular")
        if not 1 <= metadata.st_size <= _MAX_CANDIDATE_BYTES:
            raise OidcStagingProbeCliError("oidc_probe_release_candidate_size_invalid")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OidcStagingProbeCliError:
        raise
    except Exception as error:
        raise OidcStagingProbeCliError("oidc_probe_release_candidate_unreadable") from error
    if not isinstance(payload, dict):
        raise OidcStagingProbeCliError("oidc_probe_release_candidate_invalid")
    return payload


def _https_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise OidcStagingProbeCliError("oidc_probe_api_base_url_invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise OidcStagingProbeCliError("oidc_probe_api_base_url_invalid")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise OidcStagingProbeCliError("oidc_probe_api_base_url_not_staging")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_link_local or address.is_unspecified):
        raise OidcStagingProbeCliError("oidc_probe_api_base_url_not_staging")
    authority = hostname
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


def _api_audience(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise OidcStagingProbeCliError("oidc_probe_api_audience_invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or value != value.strip()
    ):
        raise OidcStagingProbeCliError("oidc_probe_api_audience_invalid")
    return value


def _edition_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise OidcStagingProbeCliError("oidc_probe_edition_date_invalid") from error
    if parsed.isoformat() != value:
        raise OidcStagingProbeCliError("oidc_probe_edition_date_invalid")
    return parsed


def _issuer_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise OidcStagingProbeCliError("oidc_probe_issuer_id_invalid") from error
    if str(parsed) != value or parsed.version != 4:
        raise OidcStagingProbeCliError("oidc_probe_issuer_id_invalid")
    return value


def _run_id(value: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise OidcStagingProbeCliError("oidc_probe_run_id_invalid")
    return value


def _write_report(destination: Path, payload: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def _absolute_path(path: Path) -> Path:
    """Make a path absolute without resolving a symlink safety boundary."""

    return Path(os.path.abspath(path))


def _default_client_factory(base_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        follow_redirects=False,
        timeout=httpx.Timeout(20.0),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Run the real Market Morning Auth0 staging exercise and emit only hash-only evidence.")
    )
    parser.add_argument("--release-candidate", required=True, type=Path)
    parser.add_argument("--api-base-url", required=True)
    parser.add_argument("--api-audience", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--edition-date", required=True)
    parser.add_argument("--issuer-id", required=True)
    parser.add_argument("--product-token-file", required=True, type=Path)
    parser.add_argument("--product-secondary-token-file", required=True, type=Path)
    parser.add_argument("--operator-token-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--confirm-staging-side-effects",
        action="store_true",
        help=(
            "Acknowledge that the probe creates one invite/user/watchlist record and revokes two supplied token instances."
        ),
    )
    return parser


def run_cli(
    argv: list[str] | None = None,
    *,
    client_factory: OidcProbeClientFactory | None = None,
    clock: Callable[[], datetime] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.confirm_staging_side_effects:
        parser.error("explicit staging side-effect confirmation is required")
    candidate_path = _absolute_path(args.release_candidate)
    token_paths = (
        _absolute_path(args.product_token_file),
        _absolute_path(args.product_secondary_token_file),
        _absolute_path(args.operator_token_file),
    )
    output = _absolute_path(args.output)
    inputs = (candidate_path, *token_paths)
    if len(set(inputs)) != len(inputs):
        parser.error("release candidate and token files must be distinct")
    if output in inputs:
        parser.error("--output must not overwrite an input")
    try:
        release_candidate = parse_release_candidate_manifest(_read_candidate(candidate_path))
        if release_candidate.status != "passed" or not release_candidate.release_revision:
            raise OidcStagingProbeCliError("oidc_probe_release_candidate_not_passed")
        base_url = _https_base_url(args.api_base_url)
        audience = _api_audience(args.api_audience)
        run_id = _run_id(args.run_id)
        edition = _edition_date(args.edition_date)
        issuer_id = _issuer_id(args.issuer_id)
        product_token = read_oidc_token_file(token_paths[0])
        product_secondary_token = read_oidc_token_file(token_paths[1])
        operator_token = read_oidc_token_file(token_paths[2])
        factory = client_factory or _default_client_factory
        with factory(base_url) as client:
            probe_kwargs: dict[str, Any] = {}
            if clock is not None:
                probe_kwargs["clock"] = clock
            payload = run_oidc_staging_probe(
                client=client,
                release_revision=release_candidate.release_revision,
                run_id=run_id,
                edition_date=edition,
                api_audience=audience,
                issuer_id=issuer_id,
                product_access_token=product_token,
                product_secondary_access_token=product_secondary_token,
                operator_access_token=operator_token,
                **probe_kwargs,
            )
    except (
        OidcStagingProbeCliError,
        OidcStagingProbeError,
        ReleaseCandidateError,
    ) as error:
        parser.error(str(error))
    _write_report(output, payload)
    return 0 if payload["status"] == "passed" else 1


def main() -> None:
    raise SystemExit(run_cli())


__all__ = [
    "OidcStagingProbeCliError",
    "OidcProbeClientFactory",
    "read_oidc_token_file",
    "run_cli",
]


if __name__ == "__main__":
    main()
