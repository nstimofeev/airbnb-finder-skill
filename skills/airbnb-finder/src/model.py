"""Headless model calls: `claude -p`, no tools, no MCP servers, no user settings, strict JSON via --json-schema."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import jsonschema

from .fetch import atomic_write

log = logging.getLogger(__name__)
# set on Ctrl+C: worker threads stop instead of starting new model calls
STOP = threading.Event()


class ModelError(RuntimeError):
    pass


def claude_cmd(model: str, schema: dict, effort: str | None = None) -> list[str]:
    exe = shutil.which("claude")  # resolves claude.exe / claude.cmd on Windows
    if not exe:
        raise ModelError("The `claude` command is not on PATH — install Claude Code (https://claude.com/claude-code)")
    # The prompt carries untrusted text (reviews, listing descriptions): the model gets no built-in tools,
    # no MCP servers (claude.ai connectors included), no skills and no user settings.
    cmd = [exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
           "--setting-sources", "", "--no-session-persistence", "--output-format", "json",
           "--json-schema", json.dumps(schema, separators=(",", ":"))]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def _run(cmd: list[str], prompt: str, timeout_s: int) -> subprocess.CompletedProcess:
    """subprocess.run, but on Windows a timeout kills the whole tree (claude.cmd → node keeps running otherwise)."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    try:
        out, err = p.communicate(prompt, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        p.kill()
        p.communicate()
        raise
    except BaseException:  # Ctrl+C: don't leave the model running
        p.kill()
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def call(prompt: str, schema: dict, *, model: str, log_dir: Path, name: str, effort: str | None = None,
         timeout_s: int = 1800, attempts: int = 2) -> dict:
    """One fresh session per call; context passes only through the prompt (files on disk)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(log_dir / f"{name}.prompt.md", prompt)
    cmd = claude_cmd(model, schema, effort)
    last_err = None
    for attempt in range(1, attempts + 1):
        if STOP.is_set():
            raise ModelError("stopped")
        t0 = time.monotonic()
        try:
            p = _run(cmd, prompt, timeout_s)
        except subprocess.TimeoutExpired:
            last_err = f"timed out after {timeout_s}s"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        if p.returncode is not None and (p.returncode < 0 or p.returncode in (130, 0xC000013A)):
            STOP.set()  # killed by Ctrl+C
            raise ModelError("interrupted")
        atomic_write(log_dir / f"{name}.response.json", p.stdout or p.stderr)
        # error texts below never quote the model's free text: it may echo untrusted input
        try:
            env = json.loads(p.stdout)
        except json.JSONDecodeError:
            last_err = f"claude exited {p.returncode}: {(p.stderr or '').strip()[:300] or 'no JSON on stdout'}"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        if env.get("is_error"):
            last_err = f"claude error ({env.get('subtype') or 'error'}, {len(str(env.get('result') or ''))} chars of text)"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        out = env.get("structured_output")
        if out is None:
            try:
                out = json.loads(env.get("result") or "")
            except json.JSONDecodeError:
                last_err = "no structured output"
                log.warning("model %s attempt %d: %s", name, attempt, last_err)
                continue
        try:
            jsonschema.validate(out, schema)
        except jsonschema.ValidationError as e:
            last_err = f"output does not match schema: {e.message[:200]} at {list(e.absolute_path)}"
            log.warning("model %s attempt %d: %s", name, attempt, last_err)
            continue
        log.info("model %s: ok in %.0fs, $%.3f", name, time.monotonic() - t0, env.get("total_cost_usd") or 0)
        return out
    raise ModelError(f"Model step '{name}' failed after {attempts} attempts: {last_err}. "
                     f"Prompt and raw answer: {log_dir}/{name}.*")
