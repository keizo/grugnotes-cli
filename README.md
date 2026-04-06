# grugnotes-cli

Command-line client for the Grug Notes `/api/v1/` API.

## Setup

```bash
pip install grugnotes-cli
grugnotes auth          # opens settings page, paste your API key
grugnotes status        # verify connection
```

API keys are fixed to one space. A key may be broad within that space or limited
to a selected prompt allowlist. Permissions are set at creation; create a new key
if you need different access.

## Security defaults

- `grugnotes auth` stores the API key in plain text at `~/.grugnotes`. The CLI sets that file to owner-only permissions (`0600`) and refuses symlinked config paths.
- `sync init --save-key` and `sync reset --save-key` save the active API key into `.grugnotes.json` so each sync directory remembers its own key. This is useful when you have multiple API keys for different spaces. Keys from `--api-key` or `GRUGNOTES_API_KEY` are ephemeral by default and require `--save-key` to persist. Keys from `~/.grugnotes` (global config) are always saved automatically. Precedence: `--api-key` > `GRUGNOTES_API_KEY` > `.grugnotes.json` (local) > `~/.grugnotes` (global).
- For CI, shared machines, or ephemeral shells, prefer `GRUGNOTES_API_KEY` instead of saving a key locally.
- The CLI refuses plain `http://` base URLs unless the host is loopback (`localhost`, `127.0.0.1`, or `::1`). To deliberately use insecure HTTP against a non-localhost host, pass `--allow-insecure-http` or set `GRUGNOTES_ALLOW_INSECURE_HTTP=1`.
- Avoid `--api-key` when possible; shell history and process lists can leak it. Prefer `grugnotes auth` or `GRUGNOTES_API_KEY`.
- Saved API keys are bound to the base URL they were stored with. If you switch `--base-url` or `GRUGNOTES_BASE_URL`, the CLI refuses to reuse the saved key unless you provide an explicit key for that host.
- Sync commands refuse to operate on symlinked sync directories or directories that contain symlinked sync paths or prompt metadata directories.
- `sync init` and `sync reset` write a local `.gitignore` covering `.grugnotes/`, `.grugnotes.json`, `.prompt.json`, and `*.conflict.md`.

## Commands

### notes — list notes

```bash
grugnotes notes                              # all notes
grugnotes notes daily-notes                  # filter by prompt
grugnotes notes --from 2026-03-01 --to 2026-03-03
grugnotes notes --page 2 --limit 50           # pagination
```

### read — read a note

```bash
grugnotes read 42                            # by id
grugnotes read 2026-03-03                    # list notes from a date
grugnotes read daily-notes                   # list notes from a prompt
grugnotes read daily-notes/2026-03-03        # single note (prompt + date)
```

### create — create a note

```bash
grugnotes create daily-notes "Hello world"
grugnotes create daily-notes "More text" --date 2026-03-03
grugnotes create daily-notes "Append this" --append
```

### edit — edit a note

```bash
grugnotes edit 42 --notes "Full replacement"
grugnotes edit 42 --old "typo" --new "fixed"
```

### prompts / prompt — list or show prompts

```bash
grugnotes prompts                             # list all
grugnotes prompts --page 2 --limit 50         # pagination
grugnotes prompt daily-notes                  # show one prompt
```

### prompts-search — offline prompt search

Searches synced prompt metadata locally (no API call). Matches by exact name,
then startswith, then contains.

```bash
grugnotes prompts-search daily
```

### sync — initialize, inspect, and reconcile

```bash
grugnotes sync init
grugnotes sync init --prompt daily-notes
grugnotes sync init --from 2026-03-01 --to 2026-03-03
grugnotes sync init --save-key                # persist API key in this directory
grugnotes sync status
grugnotes sync pull
grugnotes sync push
grugnotes sync reset
grugnotes sync reset --save-key               # persist API key on reset
grugnotes sync watch                          # continuous sync
grugnotes sync watch --interval 60            # poll every 60s (default 30)
```

All sync subcommands except `watch` support `--dry-run` to preview changes
without writing files. Sync commands exit with code 2 on unresolved conflicts.
`sync watch` polls at the configured interval while there is recent sync activity,
then slows remote checks to 60s after 5 minutes of quiet time. If the remote hash
signal is temporarily unavailable, watch mode tolerates 3 unavailable poll cycles
before falling back to a direct pull.
Poll sleeps add up to 25% jitter to avoid synchronized bursts across many clients.

### How sync works

The CLI tracks three versions of each note: the **remote** copy on the server,
the **local** `.md` file, and a **shadow** (the last content both sides agreed on,
stored in `.grugnotes/shadows/`).

On **pull**, the CLI fetches notes changed since the last sync. If only the remote
changed, the local file is updated. If both sides changed the same note, a
`.conflict.md` file is written and further syncs are blocked until you resolve it
by deleting the conflict file.

On **push**, the CLI diffs local files against their shadows and sends a 3-way
merge patch to the server. The server rejects the push if someone else saved
in between (you'll need to pull first).

**watch** runs both continuously: a file watcher pushes local edits (debounced 3s),
while a lightweight poll checks for remote changes every 30–60s.

### Scripting pattern: pull → edit → push

When a script modifies synced files (e.g. appending changelog entries), always
pull before editing to avoid overwriting changes made on the server:

```bash
grugnotes sync pull "$DIR"    # get latest from server
echo "new content" >> "$DIR/file.md"  # edit local files
grugnotes sync push "$DIR"   # push changes back
```

Pulls are incremental — only notes modified since the last sync are fetched.

## JSON output

Most commands accept `--json` to output machine-readable data instead of
human-formatted text. `sync watch` is the main exception because it is a
long-running log stream. Useful for scripting:

```bash
grugnotes read daily-notes --json | jq '.data[].date'
grugnotes notes --json | jq '.data | length'
```

## Global flags

```bash
grugnotes --base-url https://grugnotes.com status
```

Or use env vars:

```bash
export GRUGNOTES_API_KEY="gn_..."
export GRUGNOTES_BASE_URL="https://grugnotes.com"
```

## AI agent integration

The package includes an `AGENTS.md` skill file with structured instructions
for AI coding agents. After install, find it with:

```bash
python -c "import importlib.resources; print(importlib.resources.files('grugnotes_cli').parent / 'AGENTS.md')"
```
