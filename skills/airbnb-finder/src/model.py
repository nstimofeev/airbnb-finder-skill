"""Headless model calls: `claude -p`, no tools, no user settings, strict JSON via --json-schema."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import jsonschema

log = logging.getLogger(__name__)


class ModelError(RuntimeError):
    pass


def call(prompt: str, schema: dict, *, model: str, log_dir: Path, name: str, effort: str | None = None,
         timeout_s: int = 1800, attempts: int = 2) -> dict:
    """One fresh session per call; context passes only through the prompt (files on disk)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}.prompt.md").write_text(prompt)
    exe = shutil.which("claude")  # resolves claude.cmd / claude.exe on Windows
    if not exe:
        raise ModelError("The `claude` command is not on PATH — install Claude Code (https://claude.com/claude-code)")
    cmd = [exe, "-p", "--model", model, "--tools", "", "--setting-sources", "",
           "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(schema)]
    if effort:
        cmd += ["--effort", effort]
    last_err = None
    for attempt in range(1, attempts + 1):
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout_s)
        except subprocess.TimeoutExpired:
            last_err = f"timed out after {timeout_s}s"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        (log_dir / f"{name}.response.json").write_text(p.stdout or p.stderr)
        try:
            env = json.loads(p.stdout)
        except json.JSONDecodeError:
            last_err = f"claude exited {p.returncode}, not JSON: {(p.stderr or p.stdout)[:400]}"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        if env.get("is_error"):
            last_err = f"claude error: {str(env.get('result'))[:400]}"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        out = env.get("structured_output")
        if out is None:
            try:
                out = json.loads(env.get("result") or "")
            except json.JSONDecodeError:
                last_err = f"no structured output; text was: {str(env.get('result'))[:400]}"
                log.warning("model %s attempt %d: %s", name, attempt, last_err)
                continue
        try:
            jsonschema.validate(out, schema)
        except jsonschema.ValidationError as e:
            last_err = f"output does not match schema: {e.message} at {list(e.absolute_path)}"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        log.info("model %s: ok in %.0fs, $%.3f", name, time.monotonic() - t0, env.get("total_cost_usd") or 0)
        return out
    raise ModelError(f"Model step '{name}' failed after {attempts} attempts: {last_err}. "
                     f"Prompt and raw answer: {log_dir}/{name}.*")
