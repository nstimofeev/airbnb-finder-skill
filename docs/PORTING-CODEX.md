# Plan: running the skill under OpenAI Codex (and other Agent Skills hosts)

Status: planned, not implemented. Today the skill runs under Claude Code only.

## Why it is feasible

- **The skill format is shared.** `SKILL.md` with `name`/`description` frontmatter is the open Agent Skills
  format ([agentskills.io](https://agentskills.io)). Codex (CLI, desktop app, IDE extension) reads it from
  `~/.codex/skills/` or `<project>/.agents/skills/`. Gemini CLI, Cursor and GitHub Copilot read it too.
- **Everything except the model calls is agent-neutral.** Search, surroundings, photos, the page and the
  report are plain Python run by `run.py`.
- **Codex has a headless mode with a strict output shape.** `codex exec` takes `--output-schema <file>` and
  `--ephemeral`, and its sandbox can be set to `read-only`. That is enough to replace `claude -p` in
  `src/model.py`.

The ChatGPT web chat is out of scope: its sandbox has no internet, and the skill must reach Airbnb,
OpenStreetMap, OSRM and the FX service.

## Work

### 1. A second model backend (`src/model.py`)
- Config: `models.backend: auto | claude | codex` (`auto` = the first of `claude` / `codex` found on PATH).
  Model aliases per step stay in `models:`. Add a mapping, because Codex model names differ
  (e.g. `narrow: {claude: sonnet, codex: gpt-5-mini}`) or one default per backend.
- `codex_cmd()`: `codex exec --ephemeral --skip-git-repo-check --sandbox read-only --output-schema <tmpfile>
  -m <model> -` with the prompt on stdin, run from a fresh empty temp directory (`cwd`), and
  `-c mcp_servers={}` (or the equivalent) to drop the user's MCP servers. Check each flag against the
  installed `codex exec --help`.
- Parse the final message, which is JSON, then validate it with `jsonschema` as today. Keep the retries,
  the timeout with killing the process tree, Ctrl+C handling and the "no model text in errors" rule.
- Cost line in the log: use the token usage if `--json` events expose it; otherwise print nothing.

### 2. Isolation: weaker than Claude's, document it honestly
- The Claude backend gets no tools at all (`--tools ""`, `--strict-mcp-config`, no settings), verified in
  `system/init`: only `StructuredOutput`.
- Codex always keeps a shell tool. The closest we can get:
  - a `read-only` sandbox, with no network by default;
  - an empty working directory;
  - no MCP servers.
- What remains possible: an instruction planted in a review could make the model *read* local files.
  - It cannot write or reach the network.
  - Its answer must still fit the JSON schema.
  - Code checks still apply to what it returns: quote matching, the translation check, the "not confirmed"
    flag.
- Put this in README "Please read" and in `SKILL.md`. Default `auto` to Claude when both are installed.

### 3. `SKILL.md`: host-neutral wording
- Claude-only bits become conditional:
  - "run in the background with `timeout: 7200000`" → "if your host can run long commands in the
    background, do; otherwise run the steps one at a time (`search`, `context`, …), each is resumable";
  - "publish with the Artifact tool" → "if your host can publish a page, …; otherwise give the zip".
- Keep the frontmatter within the common spec: `name` and `description` only.
- Commands already use `"<skill>/run.py"`; say how each host shows the skill's directory.

### 4. Install docs
- README: Claude Code (plugin, or copy to `~/.claude/skills/`), Codex (`~/.codex/skills/airbnb-finder` or
  `.agents/skills/`), Gemini CLI (its skills directory). One skill folder for all of them, no copies in
  the repo.

### 5. Tests and a live check
- Unit tests:
  - `codex_cmd()` flags (sandbox `read-only`, no MCP, `--output-schema`, empty `cwd`);
  - backend auto-detection;
  - parsing of the final JSON.
- Live: rebuild the demo trip's `page --force` with `models.backend: codex` on the existing data, with no
  new Airbnb requests, and compare the page.
- Then a fresh small trip end to end under Codex.

## Effort

About half a day, plus the live runs.

## Open questions
- Which Codex models to default to per step (cost vs. quality) — decide after the live check.
- Whether `codex exec` can disable the shell tool entirely in some future version. If it can, use that
  and drop the caveat in §2.
