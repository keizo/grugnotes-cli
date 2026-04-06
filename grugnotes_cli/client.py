from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

RATE_LIMIT_RETRY_BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0)


@dataclass
class CLIError(Exception):
    message: str
    status_code: int | None = None
    error_code: str | None = None
    retry_after: float | None = None

    def __str__(self) -> str:
        return self.message


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None

    normalized = value.strip()
    if not normalized:
        return None

    try:
        retry_after_seconds = float(normalized)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        retry_after_seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()

    if retry_after_seconds <= 0:
        return None
    return retry_after_seconds


def _cli_error_from_response(
    response: httpx.Response,
    payload: Any,
    *,
    retry_after: float | None,
) -> CLIError:
    error_obj = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error_obj, dict):
        return CLIError(
            error_obj.get("message", "API request failed."),
            status_code=response.status_code,
            error_code=error_obj.get("code"),
            retry_after=retry_after,
        )
    return CLIError(
        f"API request failed (status {response.status_code}).",
        status_code=response.status_code,
        retry_after=retry_after,
    )


class GrugNotesClient:
    def __init__(self, *, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(
        self,
        *,
        accept: str = "application/json",
        include_content_type: bool = True,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": accept,
        }
        if include_content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _sleep_for_rate_limit(self, *, path: str, retry_index: int, retry_after: float | None) -> None:
        sleep_seconds = retry_after
        if sleep_seconds is None:
            sleep_seconds = RATE_LIMIT_RETRY_BACKOFF_SECONDS[retry_index]

        # Keep machine-readable stdout clean for `--json` callers.
        print(
            f"Rate limited on {path}; retrying in {sleep_seconds:g}s "
            f"({retry_index + 1}/{len(RATE_LIMIT_RETRY_BACKOFF_SECONDS)})...",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(sleep_seconds)

    def _resolve_text_url(self, url: str) -> str:
        split = urlsplit(url)
        if split.scheme and split.netloc:
            return url
        if split.path.startswith("/"):
            base = urlsplit(self.base_url)
            base_path = base.path.rstrip("/")
            path = f"{base_path}{split.path}" if base_path else split.path
            return urlunsplit(
                (
                    base.scheme,
                    base.netloc,
                    path,
                    split.query,
                    split.fragment,
                )
            )
        return urljoin(f"{self.base_url}/", url)

    def _same_origin(self, url: str) -> bool:
        target = urlsplit(url)
        base = urlsplit(self.base_url)

        def effective_port(split) -> int | None:
            if split.port is not None:
                return split.port
            if split.scheme == "https":
                return 443
            if split.scheme == "http":
                return 80
            return None

        return (
            target.scheme == base.scheme
            and (target.hostname or "").lower() == (base.hostname or "").lower()
            and effective_port(target) == effective_port(base)
        )

    def resolve_sync_hash_url(self, url: str) -> str | None:
        normalized = (url or "").strip()
        if not normalized:
            return None

        split = urlsplit(normalized)
        if split.username or split.password:
            return None

        if split.scheme or split.netloc:
            return normalized if self._same_origin(normalized) else None

        resolved = self._resolve_text_url(normalized)
        return resolved if self._same_origin(resolved) else None

    def _cache_bust_url(self, url: str) -> str:
        split = urlsplit(url)
        query_params = parse_qsl(split.query, keep_blank_values=True)
        query_params.append(("t", str(int(time.time() * 1000))))
        return urlunsplit(
            (
                split.scheme,
                split.netloc,
                split.path,
                urlencode(query_params, doseq=True),
                split.fragment,
            )
        )

    def fetch_sync_hash(self, url: str, *, timeout: float = 5.0) -> str | None:
        resolved_url = self.resolve_sync_hash_url(url)
        if resolved_url is None:
            return None

        target_url = self._cache_bust_url(resolved_url)
        try:
            response = httpx.get(
                target_url,
                headers=self._headers(accept="text/plain", include_content_type=False),
                timeout=timeout,
            )
        except httpx.HTTPError:
            return None

        if response.status_code != 200:
            return None
        return response.text.strip()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"

        max_retries = len(RATE_LIMIT_RETRY_BACKOFF_SECONDS)
        for retry_index in range(max_retries + 1):
            try:
                response = httpx.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
            except httpx.HTTPError as exc:
                raise CLIError(f"Network error: {exc}") from exc

            retry_after = _parse_retry_after(response.headers.get("Retry-After"))

            try:
                payload = response.json()
            except ValueError as exc:
                cli_error = CLIError(
                    f"Server returned non-JSON response (status {response.status_code}).",
                    status_code=response.status_code,
                    retry_after=retry_after,
                )
                if response.status_code == 429 and retry_index < max_retries:
                    self._sleep_for_rate_limit(
                        path=path,
                        retry_index=retry_index,
                        retry_after=retry_after,
                    )
                    continue
                raise cli_error from exc

            if response.status_code >= 400:
                cli_error = _cli_error_from_response(
                    response,
                    payload,
                    retry_after=retry_after,
                )
                if response.status_code == 429 and retry_index < max_retries:
                    self._sleep_for_rate_limit(
                        path=path,
                        retry_index=retry_index,
                        retry_after=retry_after,
                    )
                    continue
                raise cli_error

            return payload

        raise AssertionError("429 retry loop exhausted without returning or raising")
