# grugnotes CLI — Agent Skill File

Use the `grugnotes` CLI to read, create, edit, and sync notes in Grug Notes.

## Authentication

Set the API key via environment variable (preferred for agents):

```bash
export GRUGNOTES_API_KEY="gn_..."
```

Or authenticate interactively once: `grugnotes auth`

Verify with: `grugnotes status --json`

Keys are fixed to one space. A key may expose all prompts in that space or only
a prompt allowlist. Restricted keys cannot create notes in prompts outside that
allowlist. Permissions are set at creation; create a new key for different access.

## Data Model

Understanding these concepts is essential for maintaining compatibility with Grug Notes.

### Prompts

A **prompt** is a category or template that organizes content. Examples: "Daily Notes",
"Ideas", "Meeting Notes", "People". Each prompt has:

- **slug**: URL-safe identifier derived from the name (e.g. "Daily Notes" → `daily-notes`)
- **mode**: determines how blocks under this prompt behave:
  - `journal` (default) — date-centric entries, one per day (e.g. daily notes, logs)
  - `document` — persistent, title-focused entries (e.g. wiki pages, reference docs)
  - `list` — item-oriented entries (e.g. tasks, bookmarks)
  - `datatype` — structured data with attributes (e.g. people, companies)

### Blocks (Notes)

A **block** is a single content entry. The CLI calls them "notes" but the underlying
model is `GrugBlock`. Every block belongs to exactly one prompt and always has a date.

- **Date-based blocks**: no title, identified by date. Typical for journal-mode prompts.
  URL: `/{space}/{prompt_slug}/{YYYY-MM-DD}/`
- **Title-based blocks**: have a title, identified by slugified title. Typical for
  document/list/datatype prompts, but any block in any mode can have a title.
  URL: `/{space}/{prompt_slug}/{title_slug}/`

A block has both a date AND optionally a title. If a title is present, the title is
used for identification; otherwise the date is used.

### Sub-blocks (Children)

Blocks can have **child blocks** nested underneath them. A child block has a `parent_id`
pointing to its parent. The CLI and API only support **one level of nesting** — root
blocks plus direct children. Deeper nesting is not supported.

- Child blocks **must have a title** (date-only children are rejected)
- Children inherit the parent's prompt and date
- Child URL: `/{space}/{prompt_slug}/{parent_slug}/{child_slug}/`

### Spaces

A **space** is the top-level container. API keys are scoped to one space. Each space
has its own slug used in URL paths.

## Synced File Layout

When you run `sync init`, the CLI creates a local directory of markdown files that
mirrors the remote state. Understanding this layout is critical for file-level
compatibility.

### Multi-prompt sync (default)

```
sync_dir/
├── .grugnotes.json          # sync state (block IDs, hashes, scope)
├── .grugnotes/              # internal (shadows, trash)
├── .gitignore               # auto-generated
├── daily-notes/             # ← prompt slug = directory name
│   ├── .prompt.json         # prompt metadata (slug, name, emoji, mode)
│   ├── 2026-03-04.md        # date-based block (no title)
│   ├── 2026-03-05.md
│   └── project-plan/        # ← child directory (parent slug)
│       └── next-steps.md    #   child block
├── ideas/
│   ├── .prompt.json
│   ├── cool-project.md      # title-based block
│   └── cool-project/        # children of "cool-project"
│       └── phase-2.md
```

### Scoped sync (`--prompt daily-notes`)

When syncing a single prompt, the prompt directory is flattened to the root:

```
sync_dir/
├── .grugnotes.json          # scope: {"prompt": "daily-notes"}
├── .grugnotes/
├── .gitignore
├── .prompt.json             # prompt metadata at root (not in subdirectory)
├── 2026-03-04.md            # no prompt-slug prefix
├── 2026-03-05.md
└── project-plan/            # child directory directly at root
    └── next-steps.md
```

### File naming rules

- **Date-based blocks** → `YYYY-MM-DD.md` (e.g. `2026-03-04.md`)
- **Title-based blocks** → `slugified-title.md` (e.g. "Cool Project" → `cool-project.md`)
- **Slugification**: lowercased, accents removed, emoji converted to text, spaces become
  hyphens (underscores are preserved). Untransliterable content (e.g. CJK-only titles)
  falls back to `untitled` or `untitled-<hex>`
- **Child blocks** live in a directory named after the parent's stem
  (e.g. parent `cool-project.md` → children in `cool-project/`).
  Child filenames must be title-based — date filenames are rejected for children
- **Conflict files**: `*.conflict.md` — created on content conflicts, path collisions,
  or pre-existing local file mismatches during pull/reset

### Metadata files

- **`.grugnotes.json`**: sync state tracking `block_id`, `save_count`, and `synced_hash`
  (xxhash64) per file, plus last sync time, scope, base URL, and optionally a saved API key.
  `save_count` is critical — it's used for optimistic concurrency on push
- **`.prompt.json`**: prompt metadata (`slug`, `name`, `emoji`, `mode`). Located in
  each prompt directory (multi-prompt) or at root (scoped)
- **`.grugnotes/shadows/`**: shadow copies of last-synced content for conflict detection
- **`.grugnotes/trash/`**: timestamped backups of deleted files

## Rules

- **PREFER** `--json` for machine-readable output whenever the command supports it.
- **ALWAYS** use `--dry-run` before mutating sync operations (`sync push`, `sync reset`).
- **ALWAYS** confirm with the user before `sync push` or `edit` commands.
- **NEVER** run `sync watch` — it is a long-running text/log command not suited for agents.
- **NEVER** rename, move, or delete synced `.md` files directly. Tracked files are identified
  by their path in `.grugnotes.json` — renaming creates an orphan + a duplicate; deleting
  leaves a `missing_tracked` entry. Only edit file *contents* in place.
- **ALWAYS** verify prompt slugs exist before `create`. Use `grugnotes prompts --json` or
  `grugnotes prompts-search <name> --json` first. The API auto-creates prompts for
  unrestricted keys, so a typo'd slug will silently create a new prompt.
- Check exit codes: `0` = success, `1` = error, `2` = sync conflicts exist.

## Response Format

All `--json` output follows this envelope:

```json
{
  "ok": true,
  "data": { ... },
  "error": null,
  "meta": { "total_count": 42, "page": 1, "limit": 20 }
}
```

Access data with `jq '.data'`. Check errors with `jq '.ok'`.

## Commands

### List notes

```bash
grugnotes notes --json
grugnotes notes daily-notes --json
grugnotes notes --from 2026-03-01 --to 2026-03-03 --json
grugnotes notes --limit 50 --json
```

Data: array of note objects with `id`, `date`, `prompt`, `prompt_slug`, `title`, `notes`, `save_count`.

### Read a note

```bash
grugnotes read 42 --json                        # by ID
grugnotes read 2026-03-03 --json                # all notes on a date
grugnotes read daily-notes --json               # all notes for a prompt
grugnotes read daily-notes/2026-03-03 --json    # specific prompt + date
```

`read` looks up notes by ID, date, or prompt — not by title. To find a titled note,
list notes for its prompt (`grugnotes read <prompt> --json`) and filter by `title`.

### Create a note

```bash
grugnotes create daily-notes "Note content" --json
grugnotes create daily-notes "More content" --date 2026-03-03 --json
grugnotes create daily-notes "Appended text" --append --json
```

- `--append`: adds to existing note for that prompt+date instead of failing with 409.
- Omitting `--date` uses today's date.
- `create` is **date-oriented only** — there is no `--title` flag. It creates untitled,
  date-based blocks. Titled blocks (document/list/datatype entries) are created through
  the web UI or sync workflow, not the `create` command.

### Edit a note

```bash
# Full replacement
grugnotes edit 42 --notes "New full content" --json

# String replacement (like find-and-replace)
grugnotes edit 42 --old "typo" --new "fixed" --json
```

Use `--old`/`--new` for surgical edits. The API rejects ambiguous replacements (text appears multiple times).

### List prompts

```bash
grugnotes prompts --json
grugnotes prompt daily-notes --json
```

### Search prompts (offline)

Searches synced prompt metadata locally — no API call. Matches by exact name,
then startswith, then contains.

```bash
grugnotes prompts-search daily --json
```

### Sync — initialize

```bash
grugnotes sync init --json                           # all prompts
grugnotes sync init --prompt daily-notes --json      # scoped to one prompt
grugnotes sync init --from 2026-01-01 --json         # date-filtered
```

Creates a `./grugnotes/` directory with markdown files mirroring remote notes.

### Sync — check status

```bash
grugnotes sync status --json
```

Returns: `modified_locally`, `modified_remotely`, `conflicts`, `new_local`, `missing_locally`, `remote_deleted`.

### Sync — pull remote changes

```bash
grugnotes sync pull --dry-run --json    # preview first
grugnotes sync pull --json              # then pull
```

### Sync — push local changes

```bash
grugnotes sync push --dry-run --json    # preview first
grugnotes sync push --json              # then push (after user confirms)
```

If `sync push` returns a prompt-specific 404 for a restricted key, treat it as a
permission error first, not as evidence that the prompt slug is globally missing.

### Sync — reset state from remote

```bash
grugnotes sync reset --dry-run --json   # preview first
grugnotes sync reset --json             # then reset
```

## Common Workflows

### Read today's notes

```bash
grugnotes read "$(date +%Y-%m-%d)" --json | jq '.data[]'
```

### Append to today's daily notes

```bash
grugnotes create daily-notes "Meeting notes: discussed Q2 planning" --append --json
```

### Find and fix a typo

```bash
grugnotes read 42 --json | jq -r '.data.notes'    # read current content
grugnotes edit 42 --old "teh" --new "the" --json   # fix it
```

### Sync lifecycle

```bash
grugnotes sync init --json              # first time: clone notes locally
grugnotes sync status --json            # check what changed
grugnotes sync pull --json              # pull remote changes
# ... edit local markdown files ...
grugnotes sync push --dry-run --json    # preview what will push
grugnotes sync push --json              # push after confirmation
```

## Error Handling

- HTTP errors surface as `click.ClickException` (exit code 1) with a message on stderr.
- Sync conflicts produce exit code 2. Resolve `*.conflict.md` files before retrying.
- 409 responses mean a note already exists (use `--append`) or a save conflict (re-read and retry).
