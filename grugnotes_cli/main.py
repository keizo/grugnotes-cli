from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
import webbrowser
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import click
from diff_match_patch import diff_match_patch

from .client import CLIError, GrugNotesClient
from .config import CLIConfig, ConfigError, load_config, save_config
from .fs import PRIVATE_FILE_MODE, ensure_permissions, ensure_private_dir, write_private_text
from .sync_state import (
    DATE_FILENAME_RE,
    PROMPT_META_FILENAME,
    STATE_FILENAME,
    absolute_path_no_resolve,
    assert_no_symlink_components,
    ParsedSyncPath,
    SyncState,
    block_to_rel_path,
    content_hash,
    matches_synced_hash,
    parse_sync_path,
    prompt_meta_dict,
    title_from_slug_filename,
    validate_note_rel_path,
)

DEFAULT_SYNC_DIR = "./grugnotes"
CONFLICT_EXIT_CODE = 2
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYNC_GITIGNORE_LINES = (
    ".grugnotes/",
    ".grugnotes.json",
    ".prompt.json",
    "*.conflict.md",
)
logger = logging.getLogger(__name__)

SYNC_WATCH_IDLE_AFTER_SECONDS = 300.0
SYNC_WATCH_IDLE_INTERVAL_SECONDS = 60.0
SYNC_WATCH_HASH_MISS_TOLERANCE = 3
OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ESC_ESCAPE_RE = re.compile(r"\x1b[@-_]")


@dataclass
class SyncFetchResult:
    notes: list[dict[str, Any]]
    server_time: str | None


@dataclass
class SyncPullSummary:
    pulled: int = 0
    new_remote: int = 0
    renamed: int = 0
    remote_deleted: int = 0
    deleted_local: int = 0
    trashed_local: int = 0
    conflicts: int = 0
    pulled_files: list[str] = field(default_factory=list)
    conflict_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    trashed_files: list[str] = field(default_factory=list)
    written_files: dict[Path, str] = field(default_factory=dict)


@dataclass
class SyncPushSummary:
    pushed: int = 0
    remote_deleted: int = 0
    deleted_local: int = 0
    trashed_local: int = 0
    skipped_post_conflicts: int = 0
    skipped_invalid_layout: int = 0
    patch_conflicts: int = 0
    missing_tracked: int = 0
    changed_files: list[str] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    trashed_files: list[str] = field(default_factory=list)
    patch_conflict_files: list[str] = field(default_factory=list)
    invalid_layout_files: list[str] = field(default_factory=list)
    written_files: dict[Path, str] = field(default_factory=dict)


@dataclass
class SyncResetSummary:
    tracked_remote: int = 0
    wrote_files: int = 0
    conflicts: int = 0
    conflict_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SyncWatchHashPlan:
    should_pull: bool
    current_hash: str | None = None
    unavailable_cycles: int = 0


@dataclass
class RemoteDeleteReconcileResult:
    action: str
    rel_path: str
    trash_rel_path: str | None = None


@dataclass(frozen=True)
class ResolvedSyncNote:
    note: dict[str, Any]
    rel_path: str


@dataclass(frozen=True)
class ParsedNewLocalFile:
    prompt_slug: str
    note_date: str | None
    title: str | None
    parent_id: int | None
    is_child: bool


def _json_option(func):
    return click.option("--json", "json_output", is_flag=True, help="Output raw JSON.")(func)


def _echo_json(payload: dict) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _safe_terminal_text(value: Any, *, preserve_newlines: bool = False) -> str:
    text = "" if value is None else str(value)
    text = OSC_ESCAPE_RE.sub("", text)
    text = CSI_ESCAPE_RE.sub("", text)
    text = ESC_ESCAPE_RE.sub("", text)

    allowed_controls = {"\t"}
    if preserve_newlines:
        allowed_controls.add("\n")

    return "".join(
        ch
        for ch in text
        if ch in allowed_controls or unicodedata.category(ch)[0] != "C"
    )


def _summary_to_dict(summary) -> dict:
    import dataclasses

    d = dataclasses.asdict(summary)
    if "written_files" in d:
        d["written_files"] = {str(k): v for k, v in summary.written_files.items()}
    return d


def _echo_notes_table(rows: list[dict]) -> None:
    for row in rows:
        note_id = _safe_terminal_text(row.get("id"))
        note_date = _safe_terminal_text(row.get("date"))
        note_prompt = _safe_terminal_text(row.get("prompt"))
        note_title = _safe_terminal_text(row.get("title")).strip()
        first_line = (row.get("notes") or "").splitlines()
        preview = _safe_terminal_text(first_line[0] if first_line else "")
        title_prefix = f"[{note_title}] " if note_title else ""
        click.echo(f"{note_id}  {note_date}  {note_prompt}  {title_prefix}{preview}")

        for child in row.get("children") or []:
            child_id = _safe_terminal_text(child.get("id"))
            child_title = _safe_terminal_text(child.get("title")).strip() or _safe_terminal_text(
                child.get("date") or ""
            )
            child_lines = (child.get("notes") or "").splitlines()
            child_preview = _safe_terminal_text(child_lines[0] if child_lines else "")
            click.echo(f"  -> {child_id}  {child_title}  {child_preview}")


def _echo_note_detail(row: dict) -> None:
    click.echo(f"id: {_safe_terminal_text(row.get('id'))}")
    click.echo(f"date: {_safe_terminal_text(row.get('date'))}")
    click.echo(f"prompt: {_safe_terminal_text(row.get('prompt'))}")
    if row.get("title"):
        click.echo(f"title: {_safe_terminal_text(row.get('title'))}")
    if row.get("parent_id") is not None:
        click.echo(f"parent_id: {_safe_terminal_text(row.get('parent_id'))}")
    click.echo("")
    click.echo(_safe_terminal_text(row.get("notes", ""), preserve_newlines=True))

    children = row.get("children") or []
    for child in children:
        click.echo("")
        child_title = _safe_terminal_text(child.get("title")).strip() or _safe_terminal_text(
            child.get("date") or ""
        )
        click.echo(f"child {_safe_terminal_text(child.get('id'))}: {child_title}")
        click.echo(_safe_terminal_text(child.get("notes", ""), preserve_newlines=True))


def _client_from_config(config: CLIConfig) -> GrugNotesClient:
    if not config.api_key:
        if config.ignored_stored_api_key_reason:
            raise click.ClickException(config.ignored_stored_api_key_reason)
        raise click.ClickException(
            "No API key configured. Run `grugnotes auth` or set GRUGNOTES_API_KEY."
        )
    return GrugNotesClient(base_url=config.base_url, api_key=config.api_key)


def _format_cli_error(exc: CLIError) -> str:
    message = exc.message
    if exc.error_code:
        message = f"{message} [{exc.error_code}]"
    if exc.status_code:
        message = f"{message} (HTTP {exc.status_code})"
    return message


def _format_binary_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("bytes", "KiB", "MiB", "GiB", "TiB")
    unit_index = 0
    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} {units[unit_index]}"
    return f"{size:.1f} {units[unit_index]}"


def _extract_note_limits(data: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(data, dict):
        return None

    note_limits = data.get("note_limits")
    if not isinstance(note_limits, dict):
        return None

    required_keys = ("active_note_bytes_used", "max_active_note_bytes", "max_note_bytes")
    try:
        return {key: int(note_limits[key]) for key in required_keys}
    except (KeyError, TypeError, ValueError):
        return None


def _echo_note_limits(note_limits: dict[str, int]) -> None:
    used = int(note_limits["active_note_bytes_used"])
    max_active = int(note_limits["max_active_note_bytes"])
    max_note = int(note_limits["max_note_bytes"])
    remaining = max_active - used

    click.echo(
        "Note quota: "
        f"{_format_binary_bytes(used)} used / {_format_binary_bytes(max_active)} max"
    )
    if remaining >= 0:
        click.echo(f"Note headroom: {_format_binary_bytes(remaining)} free")
    else:
        click.echo(f"Note headroom: over by {_format_binary_bytes(abs(remaining))}")
    click.echo(f"Max note size: {_format_binary_bytes(max_note)}")


def _api_request(ctx: click.Context, method: str, path: str, *, params=None, body=None) -> dict:
    config: CLIConfig = ctx.obj["config"]
    client = _client_from_config(config)

    try:
        return client.request(method, path, params=params, json_body=body)
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc


@click.group(invoke_without_command=True)
@click.option("--base-url", help="Override API base URL.")
@click.option(
    "--api-key",
    help="Override API key. Unsafe for shell history and process lists; prefer `grugnotes auth`.",
)
@click.option(
    "--allow-insecure-http",
    is_flag=True,
    help="Allow non-HTTPS base URLs for non-localhost hosts for this run only.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    base_url: str | None,
    api_key: str | None,
    allow_insecure_http: bool,
):
    """Grug Notes API CLI.

    All commands accept --json to output raw API responses for scripting."""
    ctx.ensure_object(dict)
    try:
        config = load_config(
            base_url_override=base_url,
            api_key_override=api_key,
            allow_insecure_http_override=True if allow_insecure_http else None,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj["config"] = config

    if ctx.invoked_subcommand is None:
        if config.api_key:
            click.echo(ctx.get_help())
        else:
            ctx.invoke(auth)


@cli.command("auth")
@_json_option
@click.pass_context
def auth(ctx: click.Context, json_output: bool):
    """Set up API key (opens settings page)."""
    config: CLIConfig = ctx.obj["config"]
    settings_url = f"{config.base_url}/-/settings/"

    webbrowser.open(settings_url)
    click.echo(f"Opened {settings_url}")

    api_key = click.prompt("Paste your API key", hide_input=True).strip()
    if not api_key.startswith("gn_"):
        raise click.ClickException("Invalid API key format. Keys must start with `gn_`.")

    try:
        path = save_config(
            api_key=api_key,
            base_url=config.base_url,
            allow_insecure_http=config.allow_insecure_http,
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        _echo_json({"ok": True, "data": {"config_path": str(path), "base_url": config.base_url}, "error": None, "meta": {}})
        return

    click.echo(f"Saved API key to {path} (plain text, mode 600)")


@cli.command("status")
@_json_option
@click.pass_context
def status(ctx: click.Context, json_output: bool):
    """Show current authenticated user and key scope."""
    payload = _api_request(ctx, "GET", "/api/v1/me/")

    if json_output:
        _echo_json(payload)
        return

    data = payload.get("data", {})
    click.echo(
        f"User: {_safe_terminal_text(data.get('username'))} "
        f"({_safe_terminal_text(data.get('email'))})"
    )
    space = data.get("api_key_space") or {}
    allowed_prompts = data.get("api_key_allowed_prompts") or []
    note_limits = _extract_note_limits(data)
    click.echo(f"Space: {_safe_terminal_text(space.get('slug', 'unknown'))}")
    click.echo(f"Scope: {_safe_terminal_text(data.get('api_key_scope', 'unknown'))}")
    click.echo(f"Access: {_safe_terminal_text(data.get('api_key_resource_scope', 'unknown'))}")
    if data.get("api_key_resource_scope") == "prompt_allowlist":
        prompt_slugs = ", ".join(
            _safe_terminal_text(prompt.get("slug"))
            for prompt in allowed_prompts
            if isinstance(prompt, dict) and prompt.get("slug")
        )
        click.echo(f"Allowed prompts: {prompt_slugs or 'none'}")
    if note_limits is not None:
        _echo_note_limits(note_limits)


@cli.command("notes")
@click.argument("prompt", required=False)
@click.option("--from", "from_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--to", "to_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--include-children", is_flag=True, help="Inline direct child blocks on root notes.")
@click.option("--page", default=1, show_default=True, type=int)
@click.option("--limit", default=20, show_default=True, type=int)
@_json_option
@click.pass_context
def notes_list(
    ctx: click.Context,
    prompt: str | None,
    from_date,
    to_date,
    include_children: bool,
    page: int,
    limit: int,
    json_output: bool,
):
    """List notes (optionally filtered by prompt/date window)."""
    params = {"page": page, "limit": limit}
    if prompt:
        params["prompt"] = prompt
    if from_date:
        params["date_from"] = from_date.date().isoformat()
    if to_date:
        params["date_to"] = to_date.date().isoformat()
    if include_children:
        params["include_children"] = "inline"

    payload = _api_request(ctx, "GET", "/api/v1/notes/", params=params)
    rows = list(payload.get("data", []))

    if json_output:
        _echo_json(payload)
        return

    if not rows:
        click.echo("No notes found.")
        return

    _echo_notes_table(rows)


@cli.command("read")
@click.argument("reference")
@click.option("--include-children", is_flag=True, help="Inline direct child blocks for root note reads.")
@_json_option
@click.pass_context
def read_note(ctx: click.Context, reference: str, include_children: bool, json_output: bool):
    """Read a note by id, date, prompt slug, or prompt/date.

    \b
    Examples:
      grugnotes read 42                       # by id
      grugnotes read 2026-03-03               # list notes from date
      grugnotes read daily-notes              # list notes from prompt
      grugnotes read daily-notes/2026-03-03   # single note (prompt + date)
    """
    # --- numeric id -> single note detail ---
    if reference.isdigit():
        params = {"include_children": "inline"} if include_children else None
        payload = _api_request(ctx, "GET", f"/api/v1/notes/{int(reference)}/", params=params)

    # --- prompt/date -> single note detail ---
    elif "/" in reference:
        prompt, raw_date = reference.split("/", 1)
        list_payload = _api_request(
            ctx,
            "GET",
            "/api/v1/notes/",
            params={"prompt": prompt, "date": raw_date, "limit": 1, "page": 1},
        )
        rows = list_payload.get("data", [])
        if not rows:
            raise click.ClickException("No matching note found.")
        params = {"include_children": "inline"} if include_children else None
        payload = _api_request(ctx, "GET", f"/api/v1/notes/{rows[0]['id']}/", params=params)

    # --- YYYY-MM-DD -> list notes for that date ---
    elif DATE_RE.match(reference):
        params = {"date": reference}
        if include_children:
            params["include_children"] = "inline"
        list_payload = _api_request(
            ctx, "GET", "/api/v1/notes/", params=params
        )
        rows = list(list_payload.get("data", []))
        if json_output:
            _echo_json(list_payload)
            return
        if not rows:
            click.echo("No notes found.")
            return
        _echo_notes_table(rows)
        return

    # --- anything else -> treat as prompt slug ---
    else:
        params = {"prompt": reference}
        if include_children:
            params["include_children"] = "inline"
        list_payload = _api_request(
            ctx, "GET", "/api/v1/notes/", params=params
        )
        rows = list(list_payload.get("data", []))
        if json_output:
            _echo_json(list_payload)
            return
        if not rows:
            click.echo("No notes found.")
            return
        _echo_notes_table(rows)
        return

    # --- single-note detail output (id or prompt/date) ---
    if json_output:
        _echo_json(payload)
        return

    row = payload.get("data", {})
    _echo_note_detail(row)


@cli.command("create")
@click.argument("prompt")
@click.argument("notes", nargs=-1, required=True)
@click.option("--date", "note_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--append", is_flag=True, help="Append to existing untitled note for prompt/date.")
@_json_option
@click.pass_context
def create_note(
    ctx: click.Context,
    prompt: str,
    notes: tuple[str, ...],
    note_date,
    append: bool,
    json_output: bool,
):
    """Create a note for a prompt."""
    payload = {
        "prompt": prompt,
        "notes": " ".join(notes).strip(),
        "append": append,
    }
    if note_date:
        payload["date"] = note_date.date().isoformat()

    response = _api_request(ctx, "POST", "/api/v1/notes/", body=payload)

    if json_output:
        _echo_json(response)
        return

    row = response.get("data", {})
    click.echo(f"Saved note {row.get('id')} ({row.get('date')})")


@cli.command("edit")
@click.argument("note_id", type=int)
@click.option("--old", "old_string")
@click.option("--new", "new_string")
@click.option("--notes", "full_notes")
@_json_option
@click.pass_context
def edit_note(
    ctx: click.Context,
    note_id: int,
    old_string: str | None,
    new_string: str | None,
    full_notes: str | None,
    json_output: bool,
):
    """Edit a note by full replacement or single string replacement."""
    if full_notes is not None and (old_string is not None or new_string is not None):
        raise click.ClickException("Use either --notes or --old/--new, not both.")

    if full_notes is not None:
        payload = {"notes": full_notes}
    else:
        if old_string is None or new_string is None:
            raise click.ClickException("Provide --notes or both --old and --new.")
        payload = {"old_string": old_string, "new_string": new_string}

    response = _api_request(ctx, "PATCH", f"/api/v1/notes/{note_id}/", body=payload)

    if json_output:
        _echo_json(response)
        return

    row = response.get("data", {})
    click.echo(f"Updated note {row.get('id')}")


@cli.command("prompts")
@click.option("--page", default=1, show_default=True, type=int)
@click.option("--limit", default=100, show_default=True, type=int)
@_json_option
@click.pass_context
def prompts_list(ctx: click.Context, page: int, limit: int, json_output: bool):
    """List prompts."""
    payload = _api_request(
        ctx,
        "GET",
        "/api/v1/prompts/",
        params={"page": page, "limit": limit},
    )

    if json_output:
        _echo_json(payload)
        return

    rows = payload.get("data", [])
    if not rows:
        click.echo("No prompts found.")
        return

    for row in rows:
        click.echo(f"{_safe_terminal_text(row.get('slug'))}  {_safe_terminal_text(row.get('prompt'))}")


@cli.command("prompt")
@click.argument("slug")
@_json_option
@click.pass_context
def prompt_detail(ctx: click.Context, slug: str, json_output: bool):
    """Show details for a single prompt."""
    payload = _api_request(ctx, "GET", f"/api/v1/prompts/{slug}/")

    if json_output:
        _echo_json(payload)
        return

    data = payload.get("data", {})
    click.echo(f"slug: {_safe_terminal_text(data.get('slug'))}")
    click.echo(f"prompt: {_safe_terminal_text(data.get('prompt'))}")
    click.echo(f"mode: {_safe_terminal_text(data.get('prompt_mode'))}")
    click.echo(f"template: {_safe_terminal_text(data.get('notes'), preserve_newlines=True)}")


@cli.command("prompts-search")
@click.argument("query")
@click.option("--directory", "-d", default=DEFAULT_SYNC_DIR)
@_json_option
@click.pass_context
def prompts_search(ctx: click.Context, query: str, directory: str, json_output: bool):
    """Search synced prompts locally by name (offline)."""
    sync_dir = _sync_dir_from_arg(directory)
    query_normalized = (query or "").strip().casefold()
    entries = _load_prompt_metadata_entries(sync_dir)

    def _sort_key(entry: dict[str, str]) -> tuple[str, str]:
        return (entry["name"].casefold(), entry["slug"])

    exact_matches = [
        entry for entry in entries if entry["name"].casefold() == query_normalized
    ]
    startswith_matches = [
        entry
        for entry in entries
        if entry["name"].casefold().startswith(query_normalized)
        and entry["name"].casefold() != query_normalized
    ]
    contains_matches = [
        entry
        for entry in entries
        if query_normalized in entry["name"].casefold()
        and not entry["name"].casefold().startswith(query_normalized)
    ]

    grouped_matches = {
        "exact_matches": sorted(exact_matches, key=_sort_key),
        "startswith_matches": sorted(startswith_matches, key=_sort_key),
        "contains_matches": sorted(contains_matches, key=_sort_key),
    }

    if json_output:
        _echo_json(
            {
                "ok": True,
                "data": grouped_matches,
                "error": None,
                "meta": {},
            }
        )
        return

    if not query_normalized or not entries:
        click.echo("No matching prompts found.")
        return

    groups = [
        ("Exact matches", grouped_matches["exact_matches"]),
        ("Starts with", grouped_matches["startswith_matches"]),
        ("Contains", grouped_matches["contains_matches"]),
    ]

    found = False
    for heading, matches in groups:
        if not matches:
            continue
        found = True
        click.echo(f"{heading}:")
        for entry in matches:
            prefix = f"{_safe_terminal_text(entry['emoji'])} " if entry["emoji"] else ""
            click.echo(
                f"{prefix}{_safe_terminal_text(entry['name'])}  "
                f"{_safe_terminal_text(entry['slug'])}  {_safe_terminal_text(entry['mode'])}"
            )

    if not found:
        click.echo("No matching prompts found.")


# ---------------------------
# Sync helpers
# ---------------------------

def _sync_dir_from_arg(directory: str | None) -> Path:
    sync_dir = absolute_path_no_resolve(directory or DEFAULT_SYNC_DIR)
    try:
        assert_no_symlink_components(sync_dir, label="Sync directory")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    return sync_dir


def _apply_local_api_key(config: CLIConfig, state: SyncState) -> CLIConfig:
    """Use the API key from .grugnotes.json when no explicit override is active.

    Precedence: --api-key > GRUGNOTES_API_KEY > .grugnotes.json (local) > ~/.grugnotes (global).
    The local key is also bound to its base_url — if the effective base URL differs,
    the local key is ignored (matching the host-binding behavior of ~/.grugnotes).
    """
    # Explicit overrides (--api-key or env var) always win.
    if config.api_key_source in ("override", "env"):
        return config
    if not state.api_key:
        return config
    # Refuse cross-host reuse: local key is bound to the base_url it was saved with.
    if state.base_url and state.base_url != config.base_url:
        return config
    return CLIConfig(
        base_url=config.base_url,
        api_key=state.api_key,
        path=config.path,
        allow_insecure_http=config.allow_insecure_http,
        stored_base_url=config.stored_base_url,
        ignored_stored_api_key_reason=None,
        api_key_source="sync_state",
    )


def _load_state_or_fail(sync_dir: Path) -> SyncState:
    state_path = sync_dir / STATE_FILENAME
    if not state_path.exists():
        raise click.ClickException(
            f"Sync state file not found at {state_path}. Run `grugnotes sync init {sync_dir}` first."
        )
    if state_path.is_symlink():
        raise click.ClickException("Sync state file cannot be a symlink.")
    try:
        state = SyncState.load(sync_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Invalid sync state: {exc}") from exc
    return state


def _request_with_client(
    client: GrugNotesClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    allow_statuses: set[int] | None = None,
) -> tuple[dict[str, Any] | None, CLIError | None]:
    try:
        return client.request(method, path, params=params, json_body=body), None
    except CLIError as exc:
        if allow_statuses and exc.status_code in allow_statuses:
            return None, exc
        raise


def _get_cached_me_payload(client: GrugNotesClient) -> dict[str, Any] | None:
    cached = getattr(client, "_cached_me_payload", None)
    if cached is not None:
        return cached

    try:
        payload = client.request("GET", "/api/v1/me/")
    except CLIError:
        return None

    setattr(client, "_cached_me_payload", payload)
    return payload


def _extract_sync_hash_url(me_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(me_payload, dict):
        return None

    data = me_payload.get("data")
    if not isinstance(data, dict):
        return None

    sync_hash_url = data.get("sync_hash_url")
    if not isinstance(sync_hash_url, str):
        return None

    normalized = sync_hash_url.strip()
    return normalized or None


def _get_sync_hash_url(client: GrugNotesClient, *, refresh: bool = False) -> str | None:
    if refresh and hasattr(client, "_cached_me_payload"):
        delattr(client, "_cached_me_payload")
    return _extract_sync_hash_url(_get_cached_me_payload(client))


def _sync_watch_poll_interval(
    *,
    active_interval: int,
    last_activity_at: float | None,
    now: float | None = None,
) -> float:
    interval = float(active_interval)
    if last_activity_at is None or interval >= SYNC_WATCH_IDLE_INTERVAL_SECONDS:
        return interval
    if now is None:
        now = time.monotonic()
    if now - last_activity_at >= SYNC_WATCH_IDLE_AFTER_SECONDS:
        return SYNC_WATCH_IDLE_INTERVAL_SECONDS
    return interval


def _sync_watch_pull_has_activity(summary: SyncPullSummary) -> bool:
    return any(
        (
            summary.pulled,
            summary.renamed,
            summary.deleted_local,
            summary.trashed_local,
            summary.conflicts,
        )
    )


def _plan_sync_watch_pull(
    client: GrugNotesClient,
    *,
    sync_hash_url: str | None,
    last_known_hash: str | None,
    unavailable_cycles: int,
) -> SyncWatchHashPlan:
    if not sync_hash_url:
        next_unavailable_cycles = unavailable_cycles + 1
        return SyncWatchHashPlan(
            should_pull=next_unavailable_cycles > SYNC_WATCH_HASH_MISS_TOLERANCE,
            unavailable_cycles=next_unavailable_cycles,
        )

    current_hash = client.fetch_sync_hash(sync_hash_url)
    if current_hash is None:
        next_unavailable_cycles = unavailable_cycles + 1
        return SyncWatchHashPlan(
            should_pull=next_unavailable_cycles > SYNC_WATCH_HASH_MISS_TOLERANCE,
            unavailable_cycles=next_unavailable_cycles,
        )
    if last_known_hash is not None and current_hash == last_known_hash:
        return SyncWatchHashPlan(
            should_pull=False,
            current_hash=current_hash,
            unavailable_cycles=0,
        )

    return SyncWatchHashPlan(
        should_pull=True,
        current_hash=current_hash,
        unavailable_cycles=0,
    )


def _updated_sync_watch_hash(
    last_known_hash: str | None,
    *,
    current_hash: str | None,
    summary: SyncPullSummary | None,
) -> str | None:
    if current_hash is None or summary is None or summary.conflicts > 0:
        return last_known_hash
    return current_hash


def _prompt_restriction_sync_error(client: GrugNotesClient, prompt_slug: str) -> click.ClickException | None:
    me_payload = _get_cached_me_payload(client)
    if me_payload is None:
        return None

    data = me_payload.get("data", {}) if isinstance(me_payload, dict) else {}
    if data.get("api_key_resource_scope") != "prompt_allowlist":
        return None

    allowed_prompt_slugs = [
        str(prompt.get("slug"))
        for prompt in data.get("api_key_allowed_prompts", [])
        if isinstance(prompt, dict) and prompt.get("slug")
    ]
    if allowed_prompt_slugs:
        allowed_display = ", ".join(allowed_prompt_slugs)
        message = (
            f"Prompt `{prompt_slug}` is not allowed for this API key. "
            f"Allowed prompts: {allowed_display}."
        )
    else:
        message = (
            f"Prompt `{prompt_slug}` is not allowed for this API key. "
            "This key currently has no allowed prompts."
        )
    return click.ClickException(message)


def _iter_sync_tree(sync_dir: Path):
    if not sync_dir.exists():
        return

    stack = [sync_dir]
    while stack:
        current = stack.pop()
        for child in sorted(current.iterdir(), key=lambda entry: entry.name, reverse=True):
            yield child
            if child.is_dir() and not child.is_symlink():
                stack.append(child)


def _rel_display(path: Path, sync_dir: Path) -> str:
    try:
        return path.relative_to(sync_dir).as_posix()
    except ValueError:
        return str(path)


def _has_symlink_component(path: Path, sync_dir: Path) -> bool:
    try:
        relative_parts = path.relative_to(sync_dir).parts
    except ValueError:
        return True

    current = sync_dir
    if current.is_symlink():
        return True
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _assert_safe_sync_path(path: Path, sync_dir: Path, *, label: str) -> None:
    resolved_sync_dir = sync_dir.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_sync_dir):
        raise click.ClickException(f"{label} escapes sync directory: {_rel_display(path, sync_dir)}")
    if _has_symlink_component(path, sync_dir):
        raise click.ClickException(f"{label} cannot use symlinked paths: {_rel_display(path, sync_dir)}")


def _validate_prompt_metadata_path(path: Path, *, sync_dir: Path, scoped_prompt: str | None) -> None:
    rel_path = _rel_display(path, sync_dir)
    parts = path.relative_to(sync_dir).parts
    if scoped_prompt:
        if rel_path != PROMPT_META_FILENAME:
            raise click.ClickException(f"Unexpected prompt metadata path in scoped sync: {rel_path}")
        return
    if rel_path == PROMPT_META_FILENAME:
        return
    if len(parts) == 2 and parts[1] == PROMPT_META_FILENAME and not parts[0].startswith("."):
        return
    raise click.ClickException(f"Unsupported prompt metadata path: {rel_path}")


def _validate_sync_environment(
    sync_dir: Path,
    *,
    state: SyncState | None = None,
    scoped_prompt: str | None = None,
) -> None:
    effective_scope_prompt = scoped_prompt if scoped_prompt is not None else (
        state.scope_prompt if state is not None else None
    )

    issues: list[str] = []

    def add_issue(message: str) -> None:
        issues.append(message)

    state_path = sync_dir / STATE_FILENAME
    if state_path.is_symlink():
        add_issue(f"{STATE_FILENAME}: state file cannot be a symlink")

    if state is not None:
        for rel_path in sorted(state.files):
            try:
                validate_note_rel_path(rel_path, scoped_prompt=effective_scope_prompt)
            except ValueError as exc:
                add_issue(f"{rel_path}: {exc}")
                continue
            tracked_path = sync_dir / rel_path
            if tracked_path.exists() or tracked_path.is_symlink():
                try:
                    _assert_safe_sync_path(tracked_path, sync_dir, label="Tracked file")
                except click.ClickException as exc:
                    add_issue(exc.message)

    if sync_dir.exists():
        for entry in _iter_sync_tree(sync_dir):
            rel_path = _rel_display(entry, sync_dir)
            if entry.is_symlink():
                add_issue(f"{rel_path}: symlinked paths are not supported")
                continue
            if any(part.startswith(".") for part in entry.relative_to(sync_dir).parts):
                continue
            if entry.name == PROMPT_META_FILENAME:
                try:
                    _validate_prompt_metadata_path(
                        entry,
                        sync_dir=sync_dir,
                        scoped_prompt=effective_scope_prompt,
                    )
                except click.ClickException as exc:
                    add_issue(exc.message)
                continue
            if not entry.is_file():
                continue
            if entry.name.endswith(".conflict.md") or not entry.name.endswith(".md"):
                continue
            try:
                validate_note_rel_path(rel_path, scoped_prompt=effective_scope_prompt)
            except ValueError as exc:
                add_issue(f"{rel_path}: {exc}")

    if issues:
        preview = "; ".join(issues[:5])
        suffix = "; ..." if len(issues) > 5 else ""
        raise click.ClickException(f"Unsafe sync directory layout: {preview}{suffix}")


def _read_text(path: Path, *, sync_dir: Path) -> str:
    _assert_safe_sync_path(path, sync_dir, label="Sync file")
    # Preserve raw newlines so synced_hash reflects the exact file content.
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _assert_within_sync_dir(path: Path, sync_dir: Path) -> None:
    _assert_safe_sync_path(path, sync_dir, label="Sync path")


def _write_text(path: Path, text: str, *, dry_run: bool, sync_dir: Path) -> None:
    if dry_run:
        return
    _assert_safe_sync_path(path, sync_dir, label="Sync file")
    try:
        write_private_text(path, text, stop_at=sync_dir, newline="")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _render_prompt_metadata(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, indent=2, sort_keys=True) + "\n"


def _write_prompt_metadata_file(
    path: Path,
    metadata: dict[str, Any],
    *,
    dry_run: bool,
    sync_dir: Path,
) -> None:
    _assert_safe_sync_path(path, sync_dir, label="Prompt metadata")
    rendered = _render_prompt_metadata(metadata)
    if path.exists() and _read_text(path, sync_dir=sync_dir) == rendered:
        return
    _write_text(path, rendered, dry_run=dry_run, sync_dir=sync_dir)


def _expected_prompt_metadata_paths(sync_dir: Path) -> set[Path]:
    paths = {sync_dir / PROMPT_META_FILENAME}
    if not sync_dir.exists():
        return paths

    for child in sync_dir.iterdir():
        if child.is_dir() and not child.is_symlink() and not child.name.startswith("."):
            paths.add(child / PROMPT_META_FILENAME)
    return paths


def _remove_prompt_metadata_file(path: Path, *, dry_run: bool, sync_dir: Path) -> None:
    if dry_run or not path.exists():
        return
    _assert_safe_sync_path(path, sync_dir, label="Prompt metadata")
    path.unlink(missing_ok=True)


def _fetch_all_prompt_metadata(client: GrugNotesClient) -> dict[str, dict[str, Any]]:
    prompt_meta_by_slug: dict[str, dict[str, Any]] = {}
    page = 1
    total_count: int | None = None

    while total_count is None or len(prompt_meta_by_slug) < total_count:
        payload = client.request(
            "GET",
            "/api/v1/prompts/",
            params={"page": page, "limit": 100},
        )
        rows = payload.get("data", [])
        meta = payload.get("meta", {})
        if total_count is None:
            total_count = int(meta.get("total_count") or 0)

        batch_count = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = str(row.get("slug") or "").strip()
            if not slug:
                continue
            prompt_meta_by_slug[slug] = prompt_meta_dict(row)
            batch_count += 1

        if batch_count == 0:
            break
        page += 1

    return prompt_meta_by_slug


def _write_prompt_metadata(
    client: GrugNotesClient,
    state: SyncState,
    sync_dir: Path,
    *,
    dry_run: bool,
) -> None:
    expected_paths = _expected_prompt_metadata_paths(sync_dir)

    if state.scope_prompt:
        payload = client.request("GET", f"/api/v1/prompts/{state.scope_prompt}/")
        data = payload.get("data", {})
        metadata = prompt_meta_dict(data if isinstance(data, dict) else {})
        _write_prompt_metadata_file(
            sync_dir / PROMPT_META_FILENAME,
            metadata,
            dry_run=dry_run,
            sync_dir=sync_dir,
        )

        for path in expected_paths:
            if path == sync_dir / PROMPT_META_FILENAME:
                continue
            _remove_prompt_metadata_file(path, dry_run=dry_run, sync_dir=sync_dir)
        return

    prompt_meta_by_slug = _fetch_all_prompt_metadata(client)
    valid_slugs = set(prompt_meta_by_slug)

    for slug, metadata in prompt_meta_by_slug.items():
        prompt_dir = sync_dir / slug
        if not prompt_dir.is_dir():
            continue
        _write_prompt_metadata_file(
            prompt_dir / PROMPT_META_FILENAME,
            metadata,
            dry_run=dry_run,
            sync_dir=sync_dir,
        )

    for path in expected_paths:
        if path == sync_dir / PROMPT_META_FILENAME:
            _remove_prompt_metadata_file(path, dry_run=dry_run, sync_dir=sync_dir)
            continue
        if path.parent.name not in valid_slugs:
            _remove_prompt_metadata_file(path, dry_run=dry_run, sync_dir=sync_dir)


def _load_prompt_metadata_entries(sync_dir: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    paths: set[Path] = set()

    scope_prompt: str | None = None
    state_path = sync_dir / STATE_FILENAME
    if state_path.exists():
        try:
            scope_prompt = SyncState.load(sync_dir).scope_prompt
        except Exception:
            scope_prompt = None

    root_meta_path = sync_dir / PROMPT_META_FILENAME
    if root_meta_path.exists():
        paths.add(root_meta_path)

    if sync_dir.exists() and scope_prompt is None:
        for child in sync_dir.iterdir():
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
            meta_path = child / PROMPT_META_FILENAME
            if meta_path.exists():
                paths.add(meta_path)

    for path in sorted(paths):
        try:
            payload = json.loads(_read_text(path, sync_dir=sync_dir))
        except (click.ClickException, OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        slug = str(payload.get("slug") or "").strip()
        name = str(payload.get("name") or "").strip()
        if not slug or not name:
            continue
        entries.append(
            {
                "slug": slug,
                "name": name,
                "emoji": str(payload.get("emoji") or "").strip(),
                "mode": str(payload.get("mode") or "journal").strip() or "journal",
            }
        )

    return entries


def _write_sync_gitignore(sync_dir: Path, *, dry_run: bool) -> None:
    gitignore_path = sync_dir / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        _assert_safe_sync_path(gitignore_path, sync_dir, label=".gitignore")
        existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()

    existing_entries = {line.strip() for line in existing_lines if line.strip()}
    missing_entries = [entry for entry in SYNC_GITIGNORE_LINES if entry not in existing_entries]
    if not missing_entries and gitignore_path.exists():
        return

    rendered_lines = [line.rstrip() for line in existing_lines]
    if rendered_lines and rendered_lines[-1] != "":
        rendered_lines.append("")
    rendered_lines.extend(missing_entries)
    rendered = "\n".join(rendered_lines).rstrip("\n") + "\n"
    _write_text(gitignore_path, rendered, dry_run=dry_run, sync_dir=sync_dir)


def _prune_empty_dirs_for_rel_path(
    sync_dir: Path,
    rel_path: str,
    *,
    scoped_prompt: str | None,
) -> None:
    try:
        parsed = parse_sync_path(rel_path, scoped_prompt=scoped_prompt)
    except ValueError:
        return

    if not parsed.is_child:
        return

    keep_dir = sync_dir if scoped_prompt else sync_dir / parsed.prompt_slug
    current = (sync_dir / rel_path).parent
    while current != keep_dir and current != sync_dir:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _reconcile_remote_deleted(
    *,
    state: SyncState,
    sync_dir: Path,
    rel_path: str,
    entry,
    dry_run: bool,
    local_text: str | None = None,
) -> RemoteDeleteReconcileResult:
    local_path = sync_dir / rel_path
    _assert_within_sync_dir(local_path, sync_dir)

    if not local_path.exists():
        if not dry_run:
            state.remove_shadow(rel_path)
            state.remove_file(rel_path)
            _prune_empty_dirs_for_rel_path(sync_dir, rel_path, scoped_prompt=state.scope_prompt)
        return RemoteDeleteReconcileResult(action="untracked", rel_path=rel_path)

    current_text = local_text if local_text is not None else _read_text(local_path, sync_dir=sync_dir)
    local_matches_synced, local_hash, migrated_hash = matches_synced_hash(current_text, entry.synced_hash)
    if migrated_hash and not dry_run:
        entry.synced_hash = local_hash

    if local_matches_synced:
        if not dry_run:
            local_path.unlink(missing_ok=True)
            state.remove_shadow(rel_path)
            state.remove_file(rel_path)
            _prune_empty_dirs_for_rel_path(sync_dir, rel_path, scoped_prompt=state.scope_prompt)
        return RemoteDeleteReconcileResult(action="deleted", rel_path=rel_path)

    trash_path = state.next_trash_path(rel_path)
    _assert_within_sync_dir(trash_path, sync_dir)
    trash_rel_path = trash_path.relative_to(sync_dir).as_posix()
    if not dry_run:
        ensure_private_dir(trash_path.parent, stop_at=sync_dir)
        local_path.rename(trash_path)
        ensure_permissions(trash_path, PRIVATE_FILE_MODE)
        state.remove_shadow(rel_path)
        state.remove_file(rel_path)
        _prune_empty_dirs_for_rel_path(sync_dir, rel_path, scoped_prompt=state.scope_prompt)
    return RemoteDeleteReconcileResult(action="trashed", rel_path=rel_path, trash_rel_path=trash_rel_path)


def _record_remote_delete_pull(summary: SyncPullSummary, result: RemoteDeleteReconcileResult) -> None:
    summary.remote_deleted += 1
    if result.action == "deleted":
        summary.deleted_local += 1
        summary.deleted_files.append(result.rel_path)
    elif result.action == "trashed":
        summary.trashed_local += 1
        summary.trashed_files.append(result.trash_rel_path or result.rel_path)


def _record_remote_delete_push(summary: SyncPushSummary, result: RemoteDeleteReconcileResult) -> None:
    summary.remote_deleted += 1
    if result.action == "deleted":
        summary.deleted_local += 1
        summary.deleted_files.append(result.rel_path)
    elif result.action == "trashed":
        summary.trashed_local += 1
        summary.trashed_files.append(result.trash_rel_path or result.rel_path)


def _migrate_legacy_deleted_entries(state: SyncState) -> None:
    dirty = False
    if state.version < 2:
        state.version = 2
        dirty = True

    if state.legacy_deleted_paths:
        for rel_path in sorted(state.legacy_deleted_paths):
            entry = state.get_file(rel_path)
            if entry is None:
                continue
            _reconcile_remote_deleted(
                state=state,
                sync_dir=state.sync_dir,
                rel_path=rel_path,
                entry=entry,
                dry_run=False,
            )
            dirty = True
        state.legacy_deleted_paths.clear()
        dirty = True

    if dirty:
        state.save()


def _conflict_path_for(source_path: Path) -> Path:
    return source_path.with_name(f"{source_path.stem}.conflict.md")


def _write_conflict(source_path: Path, remote_text: str, *, dry_run: bool, sync_dir: Path) -> Path:
    conflict_path = _conflict_path_for(source_path)
    _write_text(conflict_path, remote_text, dry_run=dry_run, sync_dir=sync_dir)
    return conflict_path


def _remote_note_hash(note: dict[str, Any], remote_notes: str) -> str:
    value = note.get("notes_hash")
    if isinstance(value, str) and value:
        return value
    return content_hash(remote_notes)


def _build_patch_text(shadow_text: str, current_text: str) -> str:
    dmp = diff_match_patch()
    return dmp.patch_toText(dmp.patch_make(shadow_text, current_text))


def _is_editor_temp_name(name: str) -> bool:
    return (
        name.endswith(".swp")
        or name.endswith(".tmp")
        or name.endswith("~")
        or name.startswith(".#")
        or (name.startswith("#") and name.endswith("#"))
    )


def _is_ignored_watch_path(path: Path, sync_dir: Path) -> bool:
    resolved_sync_dir = sync_dir.resolve()
    resolved_path = path.resolve()

    try:
        rel = resolved_path.relative_to(resolved_sync_dir)
    except ValueError:
        return True

    if any(part.startswith(".") for part in rel.parts):
        return True

    name = rel.name
    if name == STATE_FILENAME:
        return True
    if _is_editor_temp_name(name):
        return True
    if not name.endswith(".md"):
        return True
    if name.endswith(".conflict.md"):
        return True
    return False


def _iter_local_markdown(sync_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    if not sync_dir.exists():
        return files

    for path in _iter_sync_tree(sync_dir):
        if path.is_symlink() or not path.is_file():
            continue
        if _is_ignored_watch_path(path, sync_dir):
            continue
        rel = path.relative_to(sync_dir).as_posix()
        files[rel] = path
    return files


def _is_child_note(note: dict[str, Any]) -> bool:
    return note.get("parent_id") is not None


def _root_directory_rel_path(root_rel_path: str) -> str:
    return Path(root_rel_path).with_suffix("").as_posix()


def _root_rel_path_for_child(parsed: ParsedSyncPath, *, scoped_prompt: str | None) -> str:
    if not parsed.is_child or not parsed.root_dir:
        raise ValueError(f"Path is not a child note path: {parsed}")
    root_filename = f"{parsed.root_dir}.md"
    if scoped_prompt:
        return root_filename
    return str(Path(parsed.prompt_slug) / root_filename)


def _tracked_root_rel_paths(state: SyncState) -> dict[int, str]:
    roots: dict[int, str] = {}
    for rel_path, entry in state.files.items():
        try:
            parsed = parse_sync_path(rel_path, scoped_prompt=state.scope_prompt)
        except ValueError:
            continue
        if parsed.is_child:
            continue
        roots[entry.block_id] = rel_path
    return roots


def _note_rel_path(
    note: dict[str, Any],
    state: SyncState,
    *,
    parent_rel_path: str | None = None,
) -> str:
    prompt_slug = (note.get("prompt_slug") or note.get("prompt") or "notes").strip()
    title = (note.get("title") or "").strip() or None
    date_value = note.get("date")
    return block_to_rel_path(
        prompt_slug,
        date_value,
        title,
        scoped_prompt=state.scope_prompt,
        parent_stem=Path(parent_rel_path).stem if parent_rel_path else None,
    )


def _resolve_sync_notes(
    notes: list[dict[str, Any]],
    state: SyncState,
) -> tuple[list[ResolvedSyncNote], list[tuple[dict[str, Any], str]]]:
    resolved_roots = _tracked_root_rel_paths(state)
    unresolved: list[tuple[dict[str, Any], str]] = []

    for note in notes:
        if _is_child_note(note):
            continue
        try:
            note_id = int(note.get("id"))
            resolved_roots[note_id] = _note_rel_path(note, state)
        except (TypeError, ValueError):
            unresolved.append((note, "could not derive a root rel_path"))

    resolved_notes: list[ResolvedSyncNote] = []
    for note in notes:
        try:
            if _is_child_note(note):
                parent_id = int(note.get("parent_id"))
                parent_rel_path = resolved_roots.get(parent_id)
                if parent_rel_path is None:
                    unresolved.append((note, "could not resolve its parent root"))
                    continue
                rel_path = _note_rel_path(note, state, parent_rel_path=parent_rel_path)
            else:
                rel_path = _note_rel_path(note, state)
        except (TypeError, ValueError):
            unresolved.append((note, "could not derive a rel_path"))
            continue
        resolved_notes.append(ResolvedSyncNote(note=note, rel_path=rel_path))

    return resolved_notes, unresolved


def _echo_unresolved_note_warnings(
    unresolved: list[tuple[dict[str, Any], str]],
    *,
    phase: str,
) -> None:
    for note, reason in unresolved:
        note_id = note.get("id")
        parent_id = note.get("parent_id")
        if parent_id is not None:
            click.echo(
                f"warning: skipping child block {note_id} during {phase}; parent={parent_id} {reason}",
                err=True,
            )
            continue
        click.echo(
            f"warning: skipping block {note_id} during {phase}; {reason}",
            err=True,
        )


def _note_collision_priority(
    note: dict[str, Any],
    rel_path: str,
    state: SyncState,
    index: int,
) -> tuple[int, int, int, int]:
    note_id = int(note.get("id"))
    save_count = int(note.get("save_count") or 0)
    tracked_at_path = state.get_file(rel_path)
    tracked_anywhere = state.get_file_by_block_id(note_id)
    if tracked_at_path and tracked_at_path.block_id == note_id:
        tracked_rank = 0
    elif tracked_anywhere:
        tracked_rank = 1
    else:
        tracked_rank = 2
    return tracked_rank, -save_count, -note_id, index


def _dedupe_notes_by_rel_path(
    notes: list[ResolvedSyncNote],
    state: SyncState,
) -> tuple[list[ResolvedSyncNote], list[tuple[str, int, list[int]]]]:
    selected: dict[str, tuple[tuple[int, int, int, int], int, int, ResolvedSyncNote]] = {}
    seen: dict[str, list[tuple[int, int]]] = {}

    for index, resolved in enumerate(notes):
        note = resolved.note
        rel_path = resolved.rel_path
        note_id = int(note.get("id"))
        priority = _note_collision_priority(note, rel_path, state, index)
        seen.setdefault(rel_path, []).append((note_id, index))
        current = selected.get(rel_path)
        if current is None or priority < current[0]:
            selected[rel_path] = (priority, index, note_id, resolved)

    deduped_with_order = [(item[1], item[3]) for item in selected.values()]
    deduped = [note for _index, note in sorted(deduped_with_order, key=lambda row: row[0])]
    collisions_with_order: list[tuple[int, str, int, list[int]]] = []
    for rel_path, items in seen.items():
        if len(items) < 2:
            continue
        kept_index = selected[rel_path][1]
        kept_id = selected[rel_path][2]
        skipped_ids = [note_id for note_id, index in items if index != kept_index]
        collisions_with_order.append((min(index for _note_id, index in items), rel_path, kept_id, skipped_ids))
    collisions_with_order.sort(key=lambda row: row[0])
    collisions = [(rel_path, kept_id, skipped_ids) for _order, rel_path, kept_id, skipped_ids in collisions_with_order]
    return deduped, collisions


def _echo_path_collision_warnings(collisions: list[tuple[str, int, list[int]]], *, phase: str) -> None:
    for rel_path, kept_id, skipped_ids in collisions:
        skipped_preview = ", ".join(str(note_id) for note_id in skipped_ids[:5])
        suffix = "..." if len(skipped_ids) > 5 else ""
        click.echo(
            f"warning: duplicate remote rel_path during {phase} for {rel_path}; "
            f"keeping block {kept_id}, skipping {len(skipped_ids)} block(s) ({skipped_preview}{suffix})",
            err=True,
        )


def _move_tracked_root_family(
    *,
    state: SyncState,
    sync_dir: Path,
    old_rel_path: str,
    new_rel_path: str,
    dry_run: bool,
) -> None:
    old_path = sync_dir / old_rel_path
    new_path = sync_dir / new_rel_path
    old_prefix = _root_directory_rel_path(old_rel_path)
    new_prefix = _root_directory_rel_path(new_rel_path)
    old_child_dir = sync_dir / old_prefix
    new_child_dir = sync_dir / new_prefix

    if not dry_run and old_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)

    if not dry_run:
        state.move_shadow(old_rel_path, new_rel_path)
    state.move_file(old_rel_path, new_rel_path)

    child_rel_paths = [
        rel_path
        for rel_path in list(state.files)
        if rel_path.startswith(f"{old_prefix}/")
    ]
    child_rel_paths.sort()

    if not dry_run and old_child_dir.exists():
        new_child_dir.parent.mkdir(parents=True, exist_ok=True)
        old_child_dir.rename(new_child_dir)

    for child_rel_path in child_rel_paths:
        new_child_rel_path = f"{new_prefix}{child_rel_path[len(old_prefix):]}"
        if not dry_run:
            state.move_shadow(child_rel_path, new_child_rel_path)
        state.move_file(child_rel_path, new_child_rel_path)
    if not dry_run:
        _prune_empty_dirs_for_rel_path(sync_dir, old_rel_path, scoped_prompt=state.scope_prompt)


def _fetch_notes_for_sync(
    client: GrugNotesClient,
    *,
    prompt_slug: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    updated_since: str | None = None,
    include_deleted: bool = False,
) -> SyncFetchResult:
    all_notes: list[dict[str, Any]] = []
    after_id = 0
    server_time: str | None = None

    while True:
        params: dict[str, Any] = {"limit": 100, "after_id": after_id, "include_children": "flat"}
        if prompt_slug:
            params["prompt"] = prompt_slug
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if updated_since:
            params["updated_since"] = updated_since
        if include_deleted:
            params["include_deleted"] = True

        payload = client.request("GET", "/api/v1/notes/", params=params)
        meta = payload.get("meta", {})
        if server_time is None:
            server_time = meta.get("server_time")

        rows = list(payload.get("data", []))
        if not rows:
            break

        all_notes.extend(rows)

        has_more = bool(meta.get("has_more"))
        if not has_more:
            break

        next_after_id = meta.get("next_after_id")
        if next_after_id is None:
            next_after_id = rows[-1].get("id")
        if next_after_id is None:
            break
        after_id = int(next_after_id)

    return SyncFetchResult(notes=all_notes, server_time=server_time)


def _check_no_unresolved_conflicts(state: SyncState) -> None:
    if not state.has_unresolved_conflicts():
        return
    click.echo("Unresolved .conflict.md files exist. Resolve or run `grugnotes sync reset` first.", err=True)
    raise SystemExit(CONFLICT_EXIT_CODE)


def _parse_new_local_file(rel_path: str, state: SyncState) -> ParsedNewLocalFile:
    parsed = parse_sync_path(rel_path, scoped_prompt=state.scope_prompt)
    if parsed.is_child:
        if DATE_FILENAME_RE.match(parsed.filename):
            raise ValueError("Child files must use titled filenames.")

        root_rel_path = _root_rel_path_for_child(parsed, scoped_prompt=state.scope_prompt)
        root_entry = state.get_file(root_rel_path)
        if root_entry is None:
            raise ValueError("Child files must live under a tracked root note.")

        return ParsedNewLocalFile(
            prompt_slug=parsed.prompt_slug,
            note_date=None,
            title=title_from_slug_filename(parsed.filename),
            parent_id=root_entry.block_id,
            is_child=True,
        )

    if DATE_FILENAME_RE.match(parsed.filename):
        return ParsedNewLocalFile(
            prompt_slug=parsed.prompt_slug,
            note_date=Path(parsed.filename).stem,
            title=None,
            parent_id=None,
            is_child=False,
        )

    return ParsedNewLocalFile(
        prompt_slug=parsed.prompt_slug,
        note_date=date.today().isoformat(),
        title=title_from_slug_filename(parsed.filename),
        parent_id=None,
        is_child=False,
    )


def _sync_pull_once(
    client: GrugNotesClient,
    state: SyncState,
    sync_dir: Path,
    *,
    dry_run: bool,
    echo: bool,
) -> SyncPullSummary:
    _validate_sync_environment(sync_dir, state=state)
    summary = SyncPullSummary()
    fetched = _fetch_notes_for_sync(
        client,
        prompt_slug=state.scope_prompt,
        updated_since=state.last_synced_at,
        include_deleted=True,
    )
    state_dirty = False
    resolved_notes, unresolved = _resolve_sync_notes(fetched.notes, state)
    if unresolved:
        _echo_unresolved_note_warnings(unresolved, phase="sync pull")

    deduped_notes, collisions = _dedupe_notes_by_rel_path(resolved_notes, state)
    if collisions:
        _echo_path_collision_warnings(collisions, phase="sync pull")

    try:
        for resolved in deduped_notes:
            note = resolved.note
            note_id = int(note.get("id"))
            remote_notes = note.get("notes") or ""
            remote_hash = _remote_note_hash(note, remote_notes)
            remote_save_count = int(note.get("save_count") or 0)
            deleted_at = note.get("deleted_at")
            expected_rel = resolved.rel_path
            expected_path = sync_dir / expected_rel
            _assert_within_sync_dir(expected_path, sync_dir)

            by_block = state.get_file_by_block_id(note_id)
            current_rel = by_block[0] if by_block else None
            entry = by_block[1] if by_block else None

            if deleted_at:
                if entry:
                    delete_result = _reconcile_remote_deleted(
                        state=state,
                        sync_dir=sync_dir,
                        rel_path=current_rel or expected_rel,
                        entry=entry,
                        dry_run=dry_run,
                    )
                    _record_remote_delete_pull(summary, delete_result)
                    state_dirty = True
                    if echo:
                        if delete_result.action == "deleted":
                            click.echo(f"deleted: remote removed {delete_result.rel_path}")
                        elif delete_result.action == "trashed":
                            click.echo(
                                "trashed: remote removed note with local edits; "
                                f"moved to {delete_result.trash_rel_path}"
                            )
                continue

            if entry and current_rel and current_rel != expected_rel:
                old_rel = current_rel
                old_path = sync_dir / old_rel
                root_family_conflict = False
                if not _is_child_note(note):
                    old_child_dir = sync_dir / _root_directory_rel_path(old_rel)
                    new_child_dir = sync_dir / _root_directory_rel_path(expected_rel)
                    root_family_conflict = (
                        old_child_dir != new_child_dir and new_child_dir.exists()
                    )

                if expected_path.exists() or root_family_conflict:
                    conflict_path = _write_conflict(
                        expected_path,
                        remote_notes,
                        dry_run=dry_run,
                        sync_dir=sync_dir,
                    )
                    summary.conflicts += 1
                    summary.conflict_files.append(conflict_path.relative_to(sync_dir).as_posix())
                    entry.save_count = remote_save_count
                    state_dirty = True
                    if echo:
                        click.echo(f"conflict: path collision while renaming {old_rel} -> {expected_rel}")
                else:
                    if _is_child_note(note):
                        if not dry_run and old_path.exists():
                            expected_path.parent.mkdir(parents=True, exist_ok=True)
                            old_path.rename(expected_path)
                            state.move_shadow(old_rel, expected_rel)
                        state.move_file(old_rel, expected_rel)
                    else:
                        _move_tracked_root_family(
                            state=state,
                            sync_dir=sync_dir,
                            old_rel_path=old_rel,
                            new_rel_path=expected_rel,
                            dry_run=dry_run,
                        )
                    state_dirty = True
                    by_block = state.get_file_by_block_id(note_id)
                    current_rel = by_block[0] if by_block else expected_rel
                    entry = by_block[1] if by_block else entry
                    summary.renamed += 1
                    if echo:
                        click.echo(f"renamed: {old_rel} -> {expected_rel}")

            if entry is None:
                if expected_path.exists():
                    local_text = _read_text(expected_path, sync_dir=sync_dir)
                    local_hash = content_hash(local_text)
                    if local_hash == remote_hash:
                        if not dry_run:
                            state.set_file(
                                expected_rel,
                                note_id,
                                remote_save_count,
                                remote_hash,
                            )
                            state.write_shadow(expected_rel, remote_notes)
                            state_dirty = True
                        summary.pulled += 1
                        summary.new_remote += 1
                        summary.pulled_files.append(expected_rel)
                        if echo:
                            click.echo(f"tracked: existing local file matches remote at {expected_rel}")
                    else:
                        conflict_path = _write_conflict(
                            expected_path,
                            remote_notes,
                            dry_run=dry_run,
                            sync_dir=sync_dir,
                        )
                        summary.conflicts += 1
                        summary.conflict_files.append(conflict_path.relative_to(sync_dir).as_posix())
                        if echo:
                            click.echo(f"conflict: local file exists at {expected_rel}")
                else:
                    _write_text(expected_path, remote_notes, dry_run=dry_run, sync_dir=sync_dir)
                    if not dry_run:
                        summary.written_files[expected_path.resolve()] = remote_notes
                        state.set_file(
                            expected_rel,
                            note_id,
                            remote_save_count,
                            remote_hash,
                        )
                        state.write_shadow(expected_rel, remote_notes)
                        state_dirty = True
                    summary.pulled += 1
                    summary.new_remote += 1
                    summary.pulled_files.append(expected_rel)
                continue

            local_rel = current_rel or expected_rel
            local_path = sync_dir / local_rel
            local_exists = local_path.exists()
            local_text = _read_text(local_path, sync_dir=sync_dir) if local_exists else ""
            local_hash = None
            local_matches_synced = False
            if local_exists:
                local_matches_synced, local_hash, migrated_hash = matches_synced_hash(
                    local_text,
                    entry.synced_hash,
                )
                if migrated_hash and not dry_run:
                    entry.synced_hash = local_hash
                    state_dirty = True

            if remote_save_count != entry.save_count:
                if local_matches_synced:
                    # Skip writing if remote content matches what we already have locally.
                    # This avoids unnecessary file writes (and watch-mode push→pull loops)
                    # when save_count advances due to server-side processing (auto-tags, etc.)
                    # but the actual note content hasn't changed.
                    local_already_matches_remote = local_exists and content_hash(local_text) == remote_hash
                    if local_already_matches_remote:
                        entry.save_count = remote_save_count
                        entry.synced_hash = remote_hash
                        state_dirty = True
                    else:
                        _write_text(local_path, remote_notes, dry_run=dry_run, sync_dir=sync_dir)
                        if not dry_run:
                            summary.written_files[local_path.resolve()] = remote_notes
                            state.write_shadow(local_rel, remote_notes)
                        entry.save_count = remote_save_count
                        entry.synced_hash = remote_hash
                        state_dirty = True
                        summary.pulled += 1
                        summary.pulled_files.append(local_rel)
                else:
                    conflict_path = _write_conflict(
                        local_path,
                        remote_notes,
                        dry_run=dry_run,
                        sync_dir=sync_dir,
                    )
                    summary.conflicts += 1
                    summary.conflict_files.append(conflict_path.relative_to(sync_dir).as_posix())
                    # Preserve last agreed state so the next push can diff from the
                    # shared base instead of from unmerged remote content.
                    if echo:
                        click.echo(f"conflict: both changed {local_rel}")

        # Do not advance the cursor when conflicts were generated; otherwise future pulls can
        # skip over conflicting remote revisions and keep push in a 409 loop.
        if fetched.server_time and summary.conflicts == 0:
            state.last_synced_at = fetched.server_time
            state_dirty = True
    finally:
        if not dry_run and state_dirty:
            state.save()

    _write_prompt_metadata(client, state, sync_dir, dry_run=dry_run)
    return summary


def _sync_push_once(
    client: GrugNotesClient,
    state: SyncState,
    sync_dir: Path,
    *,
    dry_run: bool,
    echo: bool,
    only_paths: set[Path] | None = None,
    abort_on_patch_conflict: bool,
) -> SyncPushSummary:
    _validate_sync_environment(sync_dir, state=state)
    summary = SyncPushSummary()
    local_files = _iter_local_markdown(sync_dir)
    state_dirty = False

    allowed_rel_paths: set[str] | None = None
    if only_paths is not None:
        allowed_rel_paths = set()
        for raw_path in only_paths:
            try:
                rel = raw_path.resolve().relative_to(sync_dir.resolve()).as_posix()
            except ValueError:
                continue
            if rel in local_files:
                allowed_rel_paths.add(rel)

    tracked_changes: list[tuple[str, Path, Any, str, str]] = []
    for rel_path, entry in state.files.items():
        if allowed_rel_paths is not None and rel_path not in allowed_rel_paths:
            continue
        abs_path = sync_dir / rel_path
        if not abs_path.exists():
            summary.missing_tracked += 1
            continue
        local_text = _read_text(abs_path, sync_dir=sync_dir)
        local_matches_synced, local_hash, migrated_hash = matches_synced_hash(local_text, entry.synced_hash)
        if migrated_hash:
            entry.synced_hash = local_hash
            state_dirty = True
        if not local_matches_synced:
            tracked_changes.append((rel_path, abs_path, entry, local_text, local_hash))
    tracked_changes.sort(key=lambda row: row[0])

    new_local_files: list[tuple[str, Path]] = []
    for rel_path, abs_path in local_files.items():
        if rel_path in state.files:
            continue
        if allowed_rel_paths is not None and rel_path not in allowed_rel_paths:
            continue
        new_local_files.append((rel_path, abs_path))
    new_local_files.sort(key=lambda row: row[0])

    total_items = len(tracked_changes) + len(new_local_files)
    show_progress = echo and total_items > 20
    progress_index = 0

    def echo_progress(rel_path: str) -> None:
        nonlocal progress_index
        if not show_progress:
            return
        progress_index += 1
        click.echo(f"[{progress_index}/{total_items}] {rel_path}")

    try:
        for rel_path, abs_path, entry, local_text, local_hash in tracked_changes:
            echo_progress(rel_path)
            if dry_run:
                summary.pushed += 1
                summary.changed_files.append(rel_path)
                continue

            request_body: dict[str, Any] = {
                "notes": local_text,
                "expected_save_count": entry.save_count,
            }
            shadow_text = state.read_shadow(rel_path)
            if shadow_text is not None:
                request_body["patch"] = _build_patch_text(shadow_text, local_text)
                request_body["shadow_hash"] = content_hash(shadow_text)
                request_body["notes_hash"] = local_hash

            payload, error = _request_with_client(
                client,
                "PATCH",
                f"/api/v1/notes/{entry.block_id}/",
                body=request_body,
                allow_statuses={404, 409, 422},
            )

            if error is not None:
                if error.status_code == 409:
                    summary.patch_conflicts += 1
                    summary.patch_conflict_files.append(rel_path)
                    if abort_on_patch_conflict:
                        raise click.ClickException(
                            f"Push conflict for {rel_path}. Run `grugnotes sync pull` first."
                        )
                    if echo:
                        click.echo(f"conflict: remote changed for {rel_path}; pull first")
                    continue
                if error.status_code == 404:
                    delete_result = _reconcile_remote_deleted(
                        state=state,
                        sync_dir=sync_dir,
                        rel_path=rel_path,
                        entry=entry,
                        dry_run=False,
                        local_text=local_text,
                    )
                    _record_remote_delete_push(summary, delete_result)
                    state_dirty = True
                    if echo:
                        if delete_result.action == "deleted":
                            click.echo(f"deleted: remote removed {delete_result.rel_path}")
                        elif delete_result.action == "trashed":
                            click.echo(
                                "trashed: remote removed note with local edits; "
                                f"moved to {delete_result.trash_rel_path}"
                            )
                    continue
                if error.status_code == 422:
                    raise click.ClickException(
                        f"Sync push failed for {rel_path}: {error.message}"
                    )

            row = (payload or {}).get("data", {})
            merged_notes = row.get("notes")
            if not isinstance(merged_notes, str):
                logger.warning(
                    "PATCH /notes/%s returned non-string `notes`; keeping local file content",
                    entry.block_id,
                )
                merged_notes = local_text
            merged_hash = row.get("notes_hash") if isinstance(row.get("notes_hash"), str) else None
            if not merged_hash:
                merged_hash = content_hash(merged_notes)

            if merged_notes != local_text:
                _write_text(abs_path, merged_notes, dry_run=False, sync_dir=sync_dir)
                summary.written_files[abs_path.resolve()] = merged_notes

            state.write_shadow(rel_path, merged_notes)
            entry.save_count = int(row.get("save_count") or entry.save_count)
            entry.synced_hash = merged_hash
            state_dirty = True
            summary.pushed += 1
            summary.changed_files.append(rel_path)

        for rel_path, abs_path in new_local_files:
            echo_progress(rel_path)
            try:
                spec = _parse_new_local_file(rel_path, state)
            except ValueError as exc:
                summary.skipped_invalid_layout += 1
                summary.invalid_layout_files.append(rel_path)
                if echo:
                    click.echo(f"skip: {rel_path} {exc}")
                continue

            local_text = _read_text(abs_path, sync_dir=sync_dir)

            if dry_run:
                summary.pushed += 1
                summary.changed_files.append(rel_path)
                continue

            if spec.is_child:
                request_body = {
                    "title": spec.title or "",
                    "notes": local_text,
                }
                request_path = f"/api/v1/notes/{spec.parent_id}/children/"
            else:
                request_body = {
                    "prompt": spec.prompt_slug,
                    "date": spec.note_date,
                    "notes": local_text,
                }
                if spec.title:
                    request_body["title"] = spec.title
                request_path = "/api/v1/notes/"

            payload, error = _request_with_client(
                client,
                "POST",
                request_path,
                body=request_body,
                allow_statuses={404, 409, 422},
            )

            if error is not None:
                if error.status_code == 409:
                    summary.skipped_post_conflicts += 1
                    if echo:
                        click.echo(f"skip: remote note already exists for {rel_path}; run pull first")
                    continue
                if error.status_code == 404:
                    prompt_error = _prompt_restriction_sync_error(client, spec.prompt_slug)
                    if prompt_error is not None:
                        raise prompt_error
                    raise click.ClickException(
                        f"Sync push failed for {rel_path}: {error.message}"
                    )
                if error.status_code == 422:
                    raise click.ClickException(
                        f"Sync push failed for {rel_path}: {error.message}"
                    )

            row = (payload or {}).get("data", {})
            block_id = int(row.get("id"))
            save_count = int(row.get("save_count") or 0)
            server_notes = row.get("notes")
            if not isinstance(server_notes, str):
                server_notes = local_text
            server_hash = row.get("notes_hash") if isinstance(row.get("notes_hash"), str) else None
            if not server_hash:
                server_hash = content_hash(server_notes)
            state.set_file(
                rel_path,
                block_id,
                save_count,
                server_hash,
            )
            state.write_shadow(rel_path, server_notes)
            state_dirty = True
            summary.pushed += 1
            summary.changed_files.append(rel_path)
    finally:
        if not dry_run and state_dirty:
            state.save()

    return summary


def _sync_reset_once(
    client: GrugNotesClient,
    sync_dir: Path,
    *,
    scope: dict[str, str] | None,
    dry_run: bool,
    api_key: str | None = None,
    base_url: str | None = None,
) -> SyncResetSummary:
    _validate_sync_environment(
        sync_dir,
        scoped_prompt=(scope or {}).get("prompt"),
    )
    summary = SyncResetSummary()
    new_state = SyncState(sync_dir=sync_dir, scope=scope, api_key=api_key, base_url=base_url)

    fetched = _fetch_notes_for_sync(
        client,
        prompt_slug=(scope or {}).get("prompt"),
    )
    resolved_notes, unresolved = _resolve_sync_notes(fetched.notes, new_state)
    if unresolved:
        _echo_unresolved_note_warnings(unresolved, phase="sync reset")
    deduped_notes, collisions = _dedupe_notes_by_rel_path(resolved_notes, new_state)
    if collisions:
        _echo_path_collision_warnings(collisions, phase="sync reset")
    existing_conflict_files = set(sync_dir.rglob("*.conflict.md")) if (sync_dir.exists() and not dry_run) else set()
    created_conflict_files: set[Path] = set()

    for resolved in deduped_notes:
        note = resolved.note
        rel_path = resolved.rel_path
        abs_path = sync_dir / rel_path
        _assert_within_sync_dir(abs_path, sync_dir)
        remote_notes = note.get("notes") or ""
        remote_hash = _remote_note_hash(note, remote_notes)
        save_count = int(note.get("save_count") or 0)
        block_id = int(note.get("id"))

        if abs_path.exists():
            local_text = _read_text(abs_path, sync_dir=sync_dir)
            local_hash = content_hash(local_text)
            if local_hash == remote_hash:
                new_state.set_file(rel_path, block_id, save_count, remote_hash)
                if not dry_run:
                    new_state.write_shadow(rel_path, remote_notes)
            else:
                conflict_path = _write_conflict(
                    abs_path,
                    remote_notes,
                    dry_run=dry_run,
                    sync_dir=sync_dir,
                )
                if not dry_run:
                    created_conflict_files.add(conflict_path)
                new_state.set_file(rel_path, block_id, save_count, remote_hash)
                if not dry_run:
                    new_state.write_shadow(rel_path, remote_notes)
                summary.conflicts += 1
                summary.conflict_files.append(conflict_path.relative_to(sync_dir).as_posix())
        else:
            _write_text(abs_path, remote_notes, dry_run=dry_run, sync_dir=sync_dir)
            new_state.set_file(rel_path, block_id, save_count, remote_hash)
            if not dry_run:
                new_state.write_shadow(rel_path, remote_notes)
            summary.wrote_files += 1

        summary.tracked_remote += 1

    if fetched.server_time:
        new_state.last_synced_at = fetched.server_time

    if not dry_run:
        new_state.save()
        for conflict_path in existing_conflict_files - created_conflict_files:
            conflict_path.unlink(missing_ok=True)

    return summary


# ---------------------------
# Sync commands
# ---------------------------


@cli.group("sync")
def sync_group() -> None:
    """Synchronize local markdown files with Grug Notes."""


@sync_group.command("init")
@click.option("--prompt", "prompt_slug", help="Scope sync to a single prompt slug.")
@click.option("--from", "from_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--to", "to_date", type=click.DateTime(formats=["%Y-%m-%d"]))
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing files.")
@click.option("--save-key", is_flag=True, help="Save the API key into the sync directory for future use.")
@_json_option
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_init(
    ctx: click.Context,
    prompt_slug: str | None,
    from_date,
    to_date,
    dry_run: bool,
    save_key: bool,
    json_output: bool,
    directory: str,
):
    """Initialize a sync directory and pull notes."""
    sync_dir = _sync_dir_from_arg(directory)
    if from_date and to_date and from_date.date() > to_date.date():
        raise click.ClickException("--from must be earlier than or equal to --to")

    state_path = sync_dir / STATE_FILENAME
    if state_path.exists():
        raise click.ClickException(
            f"Sync directory already initialized at {state_path}. Run `grugnotes sync reset {sync_dir}` instead."
        )

    config: CLIConfig = ctx.obj["config"]
    client = _client_from_config(config)

    normalized_prompt = (prompt_slug or "").strip() or None
    _validate_sync_environment(sync_dir, scoped_prompt=normalized_prompt)
    scope = {"prompt": normalized_prompt} if normalized_prompt else None
    # Only persist the key when --save-key is passed or the key already came
    # from a durable source (global config file).  Keys from --api-key or
    # GRUGNOTES_API_KEY are ephemeral and should not be written to disk
    # without explicit opt-in.
    should_save_key = save_key or config.api_key_source in ("config", "sync_state")
    persist_key = config.api_key if should_save_key else None
    persist_url = config.base_url if persist_key else None
    state = SyncState(sync_dir=sync_dir, scope=scope, api_key=persist_key, base_url=persist_url)

    try:
        fetched = _fetch_notes_for_sync(
            client,
            prompt_slug=normalized_prompt,
            date_from=from_date.date().isoformat() if from_date else None,
            date_to=to_date.date().isoformat() if to_date else None,
        )
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc

    resolved_notes, unresolved = _resolve_sync_notes(fetched.notes, state)
    if unresolved:
        _echo_unresolved_note_warnings(unresolved, phase="sync init")
    deduped_notes, collisions = _dedupe_notes_by_rel_path(resolved_notes, state)
    if collisions:
        _echo_path_collision_warnings(collisions, phase="sync init")

    notes_to_write: list[tuple[ResolvedSyncNote, Path, str]] = []
    existing_paths: list[str] = []
    for resolved in deduped_notes:
        rel_path = resolved.rel_path
        abs_path = sync_dir / rel_path
        _assert_within_sync_dir(abs_path, sync_dir)
        remote_notes = resolved.note.get("notes") or ""
        notes_to_write.append((resolved, abs_path, remote_notes))
        if abs_path.exists():
            existing_paths.append(rel_path)

    if existing_paths:
        preview = ", ".join(existing_paths[:5])
        suffix = "..." if len(existing_paths) > 5 else ""
        raise click.ClickException(
            f"Sync init would overwrite existing files ({len(existing_paths)}): {preview}{suffix}. "
            f"Use `grugnotes sync reset {sync_dir}` instead."
        )

    prompt_count: set[str] = set()
    for resolved, abs_path, remote_notes in notes_to_write:
        note = resolved.note
        rel_path = resolved.rel_path
        _write_text(abs_path, remote_notes, dry_run=dry_run, sync_dir=sync_dir)
        remote_hash = _remote_note_hash(note, remote_notes)
        state.set_file(
            rel_path,
            int(note.get("id")),
            int(note.get("save_count") or 0),
            remote_hash,
        )
        if not dry_run:
            state.write_shadow(rel_path, remote_notes)
        if note.get("prompt_slug"):
            prompt_count.add(str(note["prompt_slug"]))

    if fetched.server_time:
        state.last_synced_at = fetched.server_time

    if not dry_run:
        state.save()
        _write_sync_gitignore(sync_dir, dry_run=False)

    _write_prompt_metadata(client, state, sync_dir, dry_run=dry_run)

    if json_output:
        _echo_json({
            "ok": True,
            "data": {
                "pulled": len(deduped_notes),
                "prompt_count": len(prompt_count),
                "dry_run": dry_run,
            },
            "error": None,
            "meta": {},
        })
        return

    click.echo(f"Pulled {len(deduped_notes)} notes across {len(prompt_count)} prompts")
    if dry_run:
        click.echo("dry-run: no files were written")


@sync_group.command("status")
@_json_option
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_status(ctx: click.Context, json_output: bool, directory: str):
    """Show local vs remote sync status."""
    sync_dir = _sync_dir_from_arg(directory)
    state = _load_state_or_fail(sync_dir)
    _validate_sync_environment(sync_dir, state=state)

    local_files = _iter_local_markdown(sync_dir)
    local_changed_block_ids: set[int] = set()
    modified_local = 0
    missing_local = 0
    state_dirty = False

    for rel_path, entry in state.files.items():
        abs_path = sync_dir / rel_path
        if not abs_path.exists():
            missing_local += 1
            continue
        local_matches_synced, local_hash, migrated_hash = matches_synced_hash(
            _read_text(abs_path, sync_dir=sync_dir),
            entry.synced_hash,
        )
        if migrated_hash:
            entry.synced_hash = local_hash
            state_dirty = True
        if not local_matches_synced:
            modified_local += 1
            local_changed_block_ids.add(entry.block_id)

    if state_dirty:
        state.save()

    new_local = len([rel_path for rel_path in local_files if rel_path not in state.files])

    config = _apply_local_api_key(ctx.obj["config"], state)
    client = _client_from_config(config)
    me_payload = _get_cached_me_payload(client)
    note_limits = _extract_note_limits(
        me_payload.get("data", {}) if isinstance(me_payload, dict) else None
    )

    try:
        fetched = _fetch_notes_for_sync(
            client,
            prompt_slug=state.scope_prompt,
            updated_since=state.last_synced_at,
            include_deleted=True,
        )
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc

    modified_remote = 0
    remote_deleted = 0
    remote_changed_block_ids: set[int] = set()

    for note in fetched.notes:
        block_id = int(note.get("id"))
        found = state.get_file_by_block_id(block_id)
        if not found:
            continue

        _rel_path, entry = found
        if note.get("deleted_at"):
            remote_deleted += 1
            remote_changed_block_ids.add(block_id)
            continue

        remote_save_count = int(note.get("save_count") or 0)
        if remote_save_count != entry.save_count:
            modified_remote += 1
            remote_changed_block_ids.add(block_id)

    conflicts = len(local_changed_block_ids & remote_changed_block_ids)

    if json_output:
        _echo_json({
            "ok": True,
            "data": {
                "modified_locally": modified_local,
                "modified_remotely": modified_remote,
                "conflicts": conflicts,
                "new_local": new_local,
                "missing_locally": missing_local,
                "remote_deleted": remote_deleted,
                "note_limits": note_limits,
            },
            "error": None,
            "meta": {},
        })
        return

    click.echo(f"modified locally: {modified_local} files")
    click.echo(f"modified remotely: {modified_remote} files")
    click.echo(f"conflicts: {conflicts} files")
    click.echo(f"new local: {new_local} files")
    click.echo(f"missing locally: {missing_local} files")
    click.echo(f"remote deleted: {remote_deleted} files")
    if note_limits is not None:
        _echo_note_limits(note_limits)


@sync_group.command("pull")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing files.")
@_json_option
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_pull(ctx: click.Context, dry_run: bool, json_output: bool, directory: str):
    """Pull remote changes into local markdown files."""
    sync_dir = _sync_dir_from_arg(directory)
    state = _load_state_or_fail(sync_dir)
    _migrate_legacy_deleted_entries(state)
    _check_no_unresolved_conflicts(state)

    config = _apply_local_api_key(ctx.obj["config"], state)
    client = _client_from_config(config)

    try:
        summary = _sync_pull_once(client, state, sync_dir, dry_run=dry_run, echo=not json_output)
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc

    if json_output:
        d = _summary_to_dict(summary)
        d.pop("written_files", None)
        d["dry_run"] = dry_run
        _echo_json({"ok": True, "data": d, "error": None, "meta": {}})
        if summary.conflicts > 0:
            raise SystemExit(CONFLICT_EXIT_CODE)
        return

    click.echo(
        f"pull summary: pulled={summary.pulled} new_remote={summary.new_remote} "
        f"renamed={summary.renamed} remote_deleted={summary.remote_deleted} "
        f"deleted_local={summary.deleted_local} trashed_local={summary.trashed_local} "
        f"conflicts={summary.conflicts}"
    )

    if dry_run:
        click.echo("dry-run: no files were written")

    if summary.conflicts > 0:
        raise SystemExit(CONFLICT_EXIT_CODE)


@sync_group.command("push")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing files.")
@_json_option
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_push(ctx: click.Context, dry_run: bool, json_output: bool, directory: str):
    """Push local markdown changes to the server."""
    sync_dir = _sync_dir_from_arg(directory)
    state = _load_state_or_fail(sync_dir)
    _migrate_legacy_deleted_entries(state)
    _check_no_unresolved_conflicts(state)

    config = _apply_local_api_key(ctx.obj["config"], state)
    client = _client_from_config(config)

    try:
        summary = _sync_push_once(
            client,
            state,
            sync_dir,
            dry_run=dry_run,
            echo=not json_output,
            only_paths=None,
            abort_on_patch_conflict=True,
        )
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc

    if json_output:
        d = _summary_to_dict(summary)
        d["dry_run"] = dry_run
        _echo_json({"ok": True, "data": d, "error": None, "meta": {}})
        return

    for rel_path in summary.changed_files:
        click.echo(f"  pushed: {rel_path}")
    click.echo(
        f"push summary: pushed={summary.pushed} remote_deleted={summary.remote_deleted} "
        f"deleted_local={summary.deleted_local} trashed_local={summary.trashed_local} "
        f"post_conflicts={summary.skipped_post_conflicts} "
        f"skipped_invalid_layout={summary.skipped_invalid_layout} missing_tracked={summary.missing_tracked}"
    )
    if dry_run:
        click.echo("dry-run: no API writes were performed")


@sync_group.command("reset")
@click.option("--dry-run", is_flag=True, help="Show what would happen without writing files.")
@click.option("--save-key", is_flag=True, help="Save the API key into the sync directory for future use.")
@_json_option
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_reset(ctx: click.Context, dry_run: bool, save_key: bool, json_output: bool, directory: str):
    """Rebuild sync state from remote without deleting unmatched local files."""
    sync_dir = _sync_dir_from_arg(directory)

    scope: dict[str, str] | None = None
    existing_api_key: str | None = None
    existing_base_url: str | None = None
    existing_state_path = sync_dir / STATE_FILENAME
    if existing_state_path.exists():
        existing_state = _load_state_or_fail(sync_dir)
        scope = existing_state.scope
        existing_api_key = existing_state.api_key
        existing_base_url = existing_state.base_url

    config = _apply_local_api_key(
        ctx.obj["config"],
        SyncState(sync_dir=sync_dir, api_key=existing_api_key, base_url=existing_base_url),
    )
    client = _client_from_config(config)

    # Decide what key to write into the new state file:
    # 1. --save-key or durable source (global config / sync_state) → save the
    #    active key (which may be a new env/override key the user wants stored).
    # 2. Previous state already had a saved key and user did NOT pass --save-key
    #    → preserve the *existing* saved key, don't overwrite it with an
    #    ephemeral env/override value.
    # 3. Otherwise → don't persist any key.
    if save_key or config.api_key_source in ("config", "sync_state"):
        persist_key = config.api_key
        persist_url = config.base_url if persist_key else None
    elif existing_api_key is not None:
        persist_key = existing_api_key
        persist_url = existing_base_url
    else:
        persist_key = None
        persist_url = None

    try:
        summary = _sync_reset_once(client, sync_dir, scope=scope, dry_run=dry_run, api_key=persist_key, base_url=persist_url)
    except CLIError as exc:
        raise click.ClickException(_format_cli_error(exc)) from exc

    metadata_state = SyncState(sync_dir=sync_dir, scope=scope)
    if not dry_run:
        _write_sync_gitignore(sync_dir, dry_run=False)
    _write_prompt_metadata(client, metadata_state, sync_dir, dry_run=dry_run)

    if json_output:
        d = _summary_to_dict(summary)
        d["dry_run"] = dry_run
        _echo_json({"ok": True, "data": d, "error": None, "meta": {}})
        if summary.conflicts > 0:
            raise SystemExit(CONFLICT_EXIT_CODE)
        return

    click.echo(
        f"reset summary: tracked_remote={summary.tracked_remote} wrote_files={summary.wrote_files} "
        f"conflicts={summary.conflicts}"
    )
    if dry_run:
        click.echo("dry-run: no files were changed")

    if summary.conflicts > 0:
        raise SystemExit(CONFLICT_EXIT_CODE)


@sync_group.command("watch")
@click.option("--interval", default=30, show_default=True, type=int)
@click.argument("directory", required=False, default=DEFAULT_SYNC_DIR)
@click.pass_context
def sync_watch(ctx: click.Context, interval: int, directory: str):
    """Watch a directory and continuously pull/push changes."""
    if interval < 10:
        raise click.ClickException("--interval must be at least 10 seconds")

    sync_dir = _sync_dir_from_arg(directory)
    state = _load_state_or_fail(sync_dir)
    _migrate_legacy_deleted_entries(state)
    _check_no_unresolved_conflicts(state)

    config = _apply_local_api_key(ctx.obj["config"], state)
    client = _client_from_config(config)

    try:
        from .sync_watch import SyncWatcher
    except Exception as exc:  # pragma: no cover - dependency guard
        raise click.ClickException(f"watch mode unavailable: {exc}") from exc

    click.echo(f"watching {sync_dir} ({len(state.files)} files tracked)")

    pull_backoff = 1
    push_backoff = 1
    sync_hash_url = _get_sync_hash_url(client)
    last_known_hash: str | None = None
    hash_unavailable_cycles = 0
    sync_hash_url_retry_backoff = float(interval)
    next_sync_hash_url_retry_at = 0.0 if sync_hash_url is None else float("inf")
    sync_lock = asyncio.Lock()

    watcher: SyncWatcher

    def next_poll_interval() -> float:
        return _sync_watch_poll_interval(
            active_interval=interval,
            last_activity_at=watcher.last_activity_at,
        )

    async def on_poll_remote() -> float | None:
        nonlocal pull_backoff, state, sync_hash_url, last_known_hash, hash_unavailable_cycles
        nonlocal sync_hash_url_retry_backoff, next_sync_hash_url_retry_at
        try:
            async with sync_lock:
                if sync_hash_url is None and time.monotonic() >= next_sync_hash_url_retry_at:
                    sync_hash_url = _get_sync_hash_url(client, refresh=True)
                    if sync_hash_url is None:
                        next_sync_hash_url_retry_at = time.monotonic() + sync_hash_url_retry_backoff
                        sync_hash_url_retry_backoff = min(sync_hash_url_retry_backoff * 2.0, 300.0)
                    else:
                        next_sync_hash_url_retry_at = float("inf")
                        sync_hash_url_retry_backoff = float(interval)

                hash_plan = _plan_sync_watch_pull(
                    client,
                    sync_hash_url=sync_hash_url,
                    last_known_hash=last_known_hash,
                    unavailable_cycles=hash_unavailable_cycles,
                )
                hash_unavailable_cycles = hash_plan.unavailable_cycles
                if not hash_plan.should_pull:
                    return next_poll_interval()

                summary = _sync_pull_once(client, state, sync_dir, dry_run=False, echo=False)
                pull_backoff = 1
                if hash_plan.current_hash is None:
                    hash_unavailable_cycles = 0
                last_known_hash = _updated_sync_watch_hash(
                    last_known_hash,
                    current_hash=hash_plan.current_hash,
                    summary=summary,
                )
                if _sync_watch_pull_has_activity(summary):
                    watcher.mark_activity()
                for abs_path, written_text in summary.written_files.items():
                    watcher.mark_just_written(abs_path, written_text)
                for rel_path in summary.pulled_files:
                    click.echo(f"↓ pulled  {rel_path}")
                for rel_path in summary.deleted_files:
                    click.echo(f"deleted  {rel_path}")
                for rel_path in summary.trashed_files:
                    click.echo(f"trashed  {rel_path}")
                for rel_path in summary.conflict_files:
                    click.echo(f"⚡ conflict  {rel_path} (wrote .conflict.md)")
        except CLIError as exc:
            if exc.status_code is None:
                click.echo(f"warning: network pull failure, retrying in {pull_backoff}s")
                await asyncio.sleep(pull_backoff)
                pull_backoff = min(pull_backoff * 2, 60)
                return next_poll_interval()
            click.echo(f"warning: pull error: {_format_cli_error(exc)}")
        except Exception as exc:  # pragma: no cover - watch loop guard
            click.echo(f"warning: pull error: {exc}")
        return next_poll_interval()

    async def on_push_paths(paths: set[Path]) -> None:
        nonlocal push_backoff, state
        try:
            async with sync_lock:
                if state.has_unresolved_conflicts():
                    click.echo("warning: push skipped — resolve .conflict.md files first")
                    watcher.requeue_paths(paths)
                    return
                summary = _sync_push_once(
                    client,
                    state,
                    sync_dir,
                    dry_run=False,
                    echo=False,
                    only_paths=paths,
                    abort_on_patch_conflict=False,
                )
                push_backoff = 1
                watcher.mark_activity()
                for abs_path, written_text in summary.written_files.items():
                    watcher.mark_just_written(abs_path, written_text)
                for rel_path in summary.changed_files:
                    click.echo(f"↑ pushed  {rel_path}")
                for rel_path in summary.deleted_files:
                    click.echo(f"deleted  {rel_path}")
                for rel_path in summary.trashed_files:
                    click.echo(f"trashed  {rel_path}")
                for rel_path in summary.patch_conflict_files:
                    click.echo(f"warning: push conflict for {rel_path}; pull required")
                for rel_path in summary.invalid_layout_files:
                    click.echo(f"warning: skipped invalid layout file {rel_path}")
        except CLIError as exc:
            if exc.status_code is None:
                click.echo(f"warning: network push failure, retrying in {push_backoff}s")
                watcher.requeue_paths(paths)
                await asyncio.sleep(push_backoff)
                push_backoff = min(push_backoff * 2, 60)
                return
            click.echo(f"warning: push error: {_format_cli_error(exc)}")
        except Exception as exc:  # pragma: no cover - watch loop guard
            click.echo(f"warning: push error: {exc}")

    def on_error(exc: Exception) -> None:
        click.echo(f"warning: watcher error: {exc}")

    watcher = SyncWatcher(
        sync_dir=sync_dir,
        interval_seconds=interval,
        on_push_paths=on_push_paths,
        on_poll_remote=on_poll_remote,
        is_ignored=lambda path: _is_ignored_watch_path(path, sync_dir),
        hash_text=content_hash,
        on_error=on_error,
    )

    # Seed pending set with pre-existing local edits so they don't require a touch.
    dirty_paths: set[Path] = set()
    state_dirty = False
    local_files = _iter_local_markdown(sync_dir)
    for rel_path, entry in state.files.items():
        abs_path = sync_dir / rel_path
        if not abs_path.exists():
            continue
        local_matches_synced, local_hash, migrated_hash = matches_synced_hash(
            _read_text(abs_path, sync_dir=sync_dir),
            entry.synced_hash,
        )
        if migrated_hash:
            entry.synced_hash = local_hash
            state_dirty = True
        if not local_matches_synced:
            dirty_paths.add(abs_path.resolve())
    for rel_path in local_files:
        if rel_path not in state.files:
            dirty_paths.add((sync_dir / rel_path).resolve())
    if state_dirty:
        state.save()
    if dirty_paths:
        watcher.requeue_paths(dirty_paths)
        click.echo(f"queued {len(dirty_paths)} locally changed files for push")

    try:
        asyncio.run(watcher.run())
    except KeyboardInterrupt:
        watcher.stop()
        state.save()
        click.echo("watch stopped")


if __name__ == "__main__":
    cli()
