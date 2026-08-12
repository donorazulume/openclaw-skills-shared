#!/usr/bin/env python3
"""SPEC-SYSADMIN-002 — weekly ecosystem repo review (gateway LLM + GitHub issues)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from prompt_template import (
    STRICT_RETRY,
    SYNTHESIS_SYSTEM_TEMPLATE,
    SYNTHESIS_USER_TEMPLATE,
    SYSTEM_LANE_TEMPLATE,
    USER_LANE_TEMPLATE,
)

log = logging.getLogger("ecosystem_review")

SPEC_ID = "SPEC-SYSADMIN-002"
OPENCLAW_VERSION_FALLBACK = "2026.4.10"

ECOSYSTEM_REPOS_FULL = frozenset(
    {
        "donorazulume/openclaw-docker",
        "donorazulume/openclaw-roho",
        "donorazulume/openclaw-amara",
        "donorazulume/openclaw-rob",
        "donorazulume/openclaw-skills-shared",
    }
)

DOC_SECTIONS = [
    "automation/cron-jobs",
    "gateway/sandboxing",
    "gateway/security",
    "automation/taskflow",
    "schema/openclaw.json",
]

# Upstream may 404 `schema/openclaw.json` on the static site; try configuration reference as fallback (Issue #305).
DOC_FETCH_ALIASES: dict[str, tuple[str, ...]] = {
    "schema/openclaw.json": (
        "schema/openclaw.json",
        "gateway/configuration-reference",
    ),
}

DEFAULT_ALERTS_CID = os.environ.get("MATTERMOST_ALERTS_CHANNEL_ID", "j7ayoqd3ztf7iexsbi8x7rgiua")
DEFAULT_AGENT_ROHO_CID = os.environ.get("MATTERMOST_AGENT_ROHO_CHANNEL_ID", "1ch9knt6w3bc3gkw4a7w33k6qh")

LABELS_DEFAULT = (
    ("roho-review", "0e8a16", "Roho periodic review findings"),
    ("sysadmin-002", "5319e7", "SPEC-SYSADMIN-002 ecosystem review"),
    ("enhancement", "a2eeef", "New feature or request"),
    ("severity:critical", "b60205", "Critical severity"),
    ("severity:high", "d93f0b", "High severity"),
    ("severity:medium", "fbca04", "Medium severity"),
    ("severity:low", "cccccc", "Low severity"),
)

TOKEN_IN_CAP = int(os.environ.get("ECOSYSTEM_REVIEW_INPUT_TOKEN_CAP", "800000"))
TOKEN_OUT_CAP = int(os.environ.get("ECOSYSTEM_REVIEW_OUTPUT_TOKEN_CAP", "200000"))
TOTAL_TOKEN_WARN = int(os.environ.get("ECOSYSTEM_REVIEW_SOFT_TOKEN_WARN", "800000"))  # combined

# OpenClaw gateway /v1/chat/completions requires routing-style model ids (`openclaw` or `openclaw/<agentId>`),
# not provider slugs like `openai-compatible/deepseek-reasoner` (Issues #304, #305).
ECOSYSTEM_GATEWAY_CHAT_MODEL = (
    os.environ.get("ECOSYSTEM_REVIEW_GATEWAY_CHAT_MODEL", "openclaw").strip() or "openclaw"
)

RE_SECRET = re.compile(
    r"|".join(
        [
            r"OPENCLAW_GATEWAY_TOKEN\s*=\s*\S+",
            r"DOPPLER_[A-Z0-9_]+\s*=\s*\S+",
            r"xoxb-\S+",
            r"ghp_[A-Za-z0-9_]+",
            r"gho_[A-Za-z0-9_]+",
            r"Bearer\s+[A-Za-z0-9\-_.]+",
        ]
    ),
    re.I,
)

def _skill_dir() -> Path:
    return Path(__file__).resolve().parent


def github_manager_py() -> Path:
    return _skill_dir().parent / "github-manager" / "manager.py"


def mattermost_bridge_py() -> Path:
    return _skill_dir().parent / "mattermost-bridge" / "bridge.py"


def _cache_root() -> Path:
    return Path(os.environ.get("OPENCLAW_HOME", "/home/node/.openclaw")) / "cache"


def docs_cache_root(gwv: str) -> Path:
    p = _cache_root() / "openclaw-docs" / gwv
    p.mkdir(parents=True, mode=0o700, exist_ok=True)
    return p


def ecosystem_review_cache() -> Path:
    p = _cache_root() / "ecosystem-review"
    p.mkdir(parents=True, mode=0o700, exist_ok=True)
    return p


def detect_gateway_version(verbose: bool) -> str:
    def try_openclaw(path: str) -> str | None:
        r = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        m = re.search(r"(\d{4})\.(\d{1,3})\.(\d{1,3})", r.stdout + r.stderr)
        if not m:
            return r.stdout.strip().split()[0] if r.stdout.strip() else None
        return ".".join(m.groups())

    def docker_exec_vc() -> str:
        cp = subprocess.run(
            ["docker", "exec", "openclaw", "timeout", "10", "/usr/local/bin/openclaw", "--version"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if cp.returncode != 0:
            raise RuntimeError(cp.stderr.strip()[:200])
        m = re.search(r"(\d{4})\.(\d{1,3})\.(\d{1,3})", cp.stdout + cp.stderr)
        if not m:
            return cp.stdout.strip()
        return ".".join(m.groups())

    if shutil.which("docker"):
        try:
            ver = docker_exec_vc()
            if verbose:
                log.info("gateway version via docker exec: %s", ver)
            return ver.strip()
        except Exception as exc:
            if verbose:
                log.warning("docker exec version failed: %s", exc)

    for pth in ("/usr/local/bin/openclaw", "openclaw"):
        if shutil.which(pth.strip("/usr/local/bin/")) if pth == "openclaw" else os.path.isfile(pth):
            ex = shutil.which(pth) if pth == "openclaw" else pth
            if not ex:
                continue
            v = try_openclaw(ex)
            if v:
                return v.strip()

    log.warning("gateway version detection failed — using OPENCLAW_VERSION_FALLBACK %s", OPENCLAW_VERSION_FALLBACK)
    return OPENCLAW_VERSION_FALLBACK


def resolve_gateway_base(verbose: bool) -> str:
    base = (
        os.environ.get("OPENCLAW_GATEWAY_URL")
        or os.environ.get("OPENCLAW_GATEWAY_CHAT_COMPLETIONS_URL")
        or "http://openclaw:18789"
    ).rstrip("/")
    # normalise …/chat/completions → gateway root
    if base.endswith("/v1/chat/completions"):
        base = base[: -len("/v1/chat/completions")]
    if verbose:
        log.info("gateway base URL: %s", base)
    return base


def resolve_gateway_token() -> str:
    tok = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if tok:
        return tok
    cfg_path = Path(os.environ.get("OPENCLAW_HOME", "/home/node/.openclaw")) / "openclaw.json"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            tok = (data.get("gateway") or {}).get("auth", {}).get("token", "").strip()
            if tok:
                return tok
        except Exception:
            pass
    return ""


def _html_to_md(html: str) -> str:
    try:
        import html2text as h2t  # noqa

        return h2t.HTMLToText(bodywidth=120).handle(html or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "").strip()


def _atomic_write(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as wf:
            wf.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def fetch_docs_action(verbose: bool) -> tuple[str, list[str]]:
    ttl_days = float(os.environ.get("OPENCLAW_DOCS_CACHE_TTL_DAYS", "7"))
    ttl_sec = ttl_days * 86400

    gw = detect_gateway_version(verbose)
    root = docs_cache_root(gw)
    ua = {"User-Agent": "roho-system-admin/1.0"}
    gaps: list[str] = []
    parts_out: list[str] = []

    for section in DOC_SECTIONS:
        slug = section.replace("/", "__")
        path = root / f"{slug}.md"
        use_cache = False
        if path.is_file():
            age = time.time() - path.stat().st_mtime
            if age < ttl_sec:
                use_cache = True
                if verbose:
                    log.info("cache HIT section=%s age_s=%.0f", section, age)
        if use_cache:
            parts_out.append(f"<!-- {section} cached -->\n{path.read_text(encoding='utf-8', errors='replace')}")
            continue
        if verbose:
            log.info("cache MISS fetching %s", section)
        rel_paths = DOC_FETCH_ALIASES.get(section, (section,))
        r: requests.Response | None = None
        url_used = ""
        try:
            for sp in rel_paths:
                url = f"https://docs.openclaw.ai/{sp.lstrip('/')}"
                r = requests.get(url, headers=ua, timeout=20)
                url_used = url
                if r.status_code < 400:
                    break
                if verbose:
                    log.info("doc fetch HTTP %s for %s — trying next alias", r.status_code, url)
            if r is None or r.status_code >= 400:
                gaps.append(f"{section}: HTTP {(r.status_code if r else 'no-response')} tried={','.join(rel_paths)}")
                continue
            body = _html_to_md(r.text if "text/html" in r.headers.get("Content-Type", "") else r.text)
            _atomic_write(path, f"# Doc: {section}\nSource URL: {url_used}\n\n{body}\n")
            parts_out.append(f"<!-- fetched {section} -->\n{path.read_text(encoding='utf-8', errors='replace')}")
        except Exception as exc:
            gaps.append(f"{section}: {exc}")
            if verbose:
                log.warning("doc fetch failure %s — %s", section, exc)

    return ("\n\n---\n\n".join(parts_out), gaps)


def redact(text: str) -> str:
    return RE_SECRET.sub(lambda m: m.group(0).split("=", 1)[0] + "=[REDACTED]" if "=" in m.group(0) else "[REDACTED]", text)


def _repo_slug(full: str) -> str:
    return full.split("/", 1)[1]


_SKIP_PARTS = frozenset({"__pycache__", ".git", "node_modules"})


def _walk_skippable(rel: Path) -> bool:
    return any(p in _SKIP_PARTS for p in rel.parts)


_SUFFIX_OK = frozenset({".py", ".md", ".sh", ".yml", ".yaml", ".json", ".txt"})


def _repo_path_allowed(repo_root: Path, slug: str, p: Path) -> bool:
    """Whether p is scanned for SPEC-SYSADMIN-002 §6.2 style review."""
    if not p.is_file():
        return False
    try:
        rel = p.relative_to(repo_root)
    except ValueError:
        return False
    if _walk_skippable(rel):
        return False
    rs = rel.as_posix()

    ok = False
    if slug == "openclaw-docker":
        if (
            rs.startswith(("sdds/", "tests/", "data/", "node_modules/", ".git/", ".claude/", ".pytest_cache/", "skills-shared/"))
            or "__pycache__" in rs
            or rs.endswith(".lock")
        ):
            ok = False
        elif rs.startswith(("scripts/", "config/", "docker-compose/", "docs/", "skills/system-admin/", "skills/github-manager/")):
            ext = p.suffix.lstrip(".").lower()
            ok = ext in {"py", "sh", "md", "json", "txt", "yml", "yaml"} or "Dockerfile" in p.name
        elif rs.startswith("services/openclaw-mcp-"):
            ok = p.name.endswith(".py") or rs.endswith(".md") or p.name.endswith("SKILL.md")
        elif len(rel.parts) == 1 and p.name.startswith("Dockerfile"):
            ok = True
    elif slug in {"openclaw-roho", "openclaw-amara", "openclaw-skills-shared"}:
        if rs.startswith(("tests/", ".git/", "skills-shared/.git")) or "__pycache__" in rs or rs.endswith(".lock"):
            ok = False
        elif p.suffix.lower() in _SUFFIX_OK and not rs.startswith("tests/"):
            ok = True
        elif p.name in {"SOUL.md", "MEMORY.md", "AGENTS.md", "Dockerfile", "pyproject.toml", "requirements.txt"}:
            ok = True
    elif slug == "openclaw-rob":
        if rs.startswith(("tests/", ".git/", "__pycache__")) or rs.endswith(".lock"):
            ok = False
        elif rs.startswith(("src/", "skills/", "config/")) and p.suffix.lower() in _SUFFIX_OK:
            ok = True
        elif p.name in {"SOUL.md", "MEMORY.md", "AGENTS.md", "Dockerfile"}:
            ok = True

    return ok


def _collect_reviewable_paths(repo_root: Path, slug: str) -> list[Path]:
    return [p for p in repo_root.rglob("*") if _repo_path_allowed(repo_root, slug, p)]


def iter_repo_files(repo_root: Path, slug: str, max_files: int, max_bytes: int, verbose: bool) -> Iterator[tuple[str, str]]:
    """Yield (relative_posix_path, capped_content_utf8). Alphabetical-first (v1 baseline)."""
    collected = sorted(_collect_reviewable_paths(repo_root, slug), key=lambda x: str(x))
    for p in collected[:max_files]:
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        if len(text) > max_bytes:
            text = text[:max_bytes] + "\n…[truncated]\n"
        yield p.relative_to(repo_root).as_posix(), redact(text)


def rag_manager_py() -> Path:
    return _skill_dir().parent / "rag-brain-manager" / "manager.py"


def _week_iso_utc(now: dt.datetime | None = None) -> str:
    n = now or dt.datetime.now(dt.timezone.utc)
    y, w, _ = n.isocalendar()
    return f"{y}-W{w:02d}"


_DOC_SUMMARY_SYS = (
    "You compress OpenClaw documentation into a factual engineering briefing for experts. "
    "Use thematic headings (cron/agents, gateway/sandbox/security, schemas/config). "
    "Keep concrete facts — max ~26000 characters. Do not omit version-sensitive behaviour."
)


def _rag_query_findings_raw(repo_full: str, since_weeks: int, n: int, verbose: bool) -> dict[str, Any] | None:
    mgr = rag_manager_py()
    if not mgr.is_file():
        log.warning("BRAIN_UNREACHABLE_NO_PRIOR_CONTEXT — missing rag-brain-manager at %s", mgr)
        return None
    cmd = [
        sys.executable,
        str(mgr),
        "--action",
        "query-findings",
        "--collection",
        "open_brain",
        "--where",
        json.dumps({"source": "roho_review", "repo": repo_full}),
        "--since-weeks",
        str(since_weeks),
        "--n-results",
        str(n),
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.SubprocessError as exc:
        log.warning("query-findings transport error: %s", exc)
        return None
    if cp.returncode != 0:
        if verbose or cp.stderr:
            log.warning("query-findings failed rc=%s stderr=%s", cp.returncode, (cp.stderr or "")[:600])
        return None
    try:
        payload = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if payload.get("error"):
        log.warning("query-findings error field: %s", payload.get("error"))
        return None
    return payload


def _rag_query_where_raw(where: dict[str, Any], since_weeks: int | None, n: int, verbose: bool) -> dict[str, Any] | None:
    mgr = rag_manager_py()
    if not mgr.is_file():
        return None
    cmd = [
        sys.executable,
        str(mgr),
        "--action",
        "query-findings",
        "--collection",
        "open_brain",
        "--where",
        json.dumps(where),
        "--n-results",
        str(n),
    ]
    if isinstance(since_weeks, int):
        cmd += ["--since-weeks", str(since_weeks)]
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.SubprocessError as exc:
        if verbose:
            log.warning("query-findings(where) transport: %s", exc)
        return None
    if cp.returncode != 0:
        return None
    try:
        return json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return None


def _format_prior_sections(
    brain: dict[str, Any] | None,
) -> tuple[str, str, int, int]:
    if not brain or brain.get("action") != "query-findings":
        return "(none)", "(none)", 0, 0
    suppressed = frozenset({"fixed", "wontfix", "false-positive"})
    findings = brain.get("findings") or []
    open_lines: list[str] = []
    sup_lines: list[str] = []

    def _pick_title(row: dict[str, Any], md: dict[str, Any]) -> str:
        return str(md.get("finding_title") or (row.get("document") or ""))[:200]

    for row in findings:
        if not isinstance(row, dict):
            continue
        md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        st = str(md.get("status") or "open").lower()
        fp = md.get("fingerprint") or ""
        title = _pick_title(row, md)
        line = (
            f"- fp:{fp} status={st} sev={md.get('severity', '?')} "
            f"cat={md.get('category', '?')} title={title!r}"
        )
        if st in suppressed:
            sup_lines.append(line)
        else:
            open_lines.append(line)

    return (
        "\n".join(open_lines) if open_lines else "(none)",
        "\n".join(sup_lines) if sup_lines else "(none)",
        len(open_lines),
        len(sup_lines),
    )


def ensure_docs_summary(
    docs_concat: str,
    gw_base: str,
    token: str,
    gw_ver: str,
    verbose: bool,
) -> tuple[str, dict[str, int]]:
    root = docs_cache_root(gw_ver)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = root / "_docs_summary.md"
    ttl_h = float(os.environ.get("ECOSYSTEM_DOCS_SUMMARY_TTL_HOURS", "24"))
    if path.is_file() and (time.time() - path.stat().st_mtime) < ttl_h * 3600:
        txt = path.read_text(encoding="utf-8", errors="replace")
        return txt[:96000], {"prompt_tokens": 0, "completion_tokens": 0}

    truncated = docs_concat[:240000]
    msgs = [
        {"role": "system", "content": _DOC_SUMMARY_SYS},
        {"role": "user", "content": "Produce the condensed briefing now:\n\n" + truncated},
    ]
    summary: str
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    try:
        raw, usage, _ = _chat(
            gw_base,
            token,
            "openai-compatible/deepseek-v4-flash",
            msgs,
            timeout=int(os.environ.get("ECOSYSTEM_DOCS_SUMMARY_TIMEOUT_SEC", "240")),
            verbose=verbose,
        )
        summary = (raw or "").strip()
        if not summary:
            summary = truncated[:96000]
    except RuntimeError as exc:
        log.warning("docs summary gateway failed (%s) — truncation fallback", exc)
        summary = truncated[:96000]

    summary = summary[:96000]
    try:
        _atomic_write(path, summary)
    except OSError:
        pass
    return summary, usage


def _git_recent_paths(repo_root: Path, days: int = 14) -> set[str]:
    cp = subprocess.run(
        ["git", "-C", str(repo_root), "log", f"--since={days}.days.ago", "--name-only", "--pretty=format:"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if cp.returncode != 0:
        return set()
    return {ln.strip().replace("\\", "/") for ln in cp.stdout.splitlines() if ln.strip()}


def _paths_from_open_roho_issues(full_repo: str) -> set[str]:
    js = _gh(
        "issue",
        "list",
        "--repo",
        full_repo,
        "--label",
        "roho-review",
        "--state",
        "open",
        "--json",
        "body",
        "-L",
        "80",
    )
    if js.returncode != 0:
        return set()
    try:
        rows = json.loads(js.stdout or "[]")
    except json.JSONDecodeError:
        return set()
    found: set[str] = set()
    for row in rows:
        body = row.get("body") or ""
        for m in re.finditer(r"`([^`]+\.[a-z0-9]+)`", body, flags=re.I):
            found.add(m.group(1).strip("./"))
        for m in re.finditer(r"([A-Za-z0-9_\-.]+/(?:[^`\s]+\.(?:py|md|json|ya?ml|sh)))", body):
            candidate = m.group(1).strip("./")
            if "/" in candidate:
                found.add(candidate)
    return found


def _drift_weight(rel_posix: str) -> float:
    lpath = rel_posix.lower()
    w = 0.0
    if "dockerfile" in lpath or lpath.endswith("/dockerfile"):
        w += 40.0
    if lpath.endswith("skill.md"):
        w += 35.0
    if "/cron-payloads/" in lpath or "post-deploy.sh" in lpath:
        w += 38.0
    if lpath.startswith(("config/", "skills/", "docker-compose/", "scripts/", ".github/")):
        w += 16.0
    if lpath.endswith((".json", ".yml", ".yaml")):
        w += 12.0
    return w


def iter_repo_files_smart(
    repo_root: Path,
    slug: str,
    full_repo: str,
    max_files: int,
    max_bytes: int,
    verbose: bool,
) -> Iterator[tuple[str, str]]:
    """Weighted file selection — SPEC-SYSADMIN-002.1 REQ-SYSADMIN.1-305."""
    rng = random.Random(int(hashlib.sha256(full_repo.encode()).hexdigest()[:8], 16))
    pool = _collect_reviewable_paths(repo_root, slug)

    recent = _git_recent_paths(repo_root)
    hinted = _paths_from_open_roho_issues(full_repo)

    if not pool:
        log.warning(
            "No reviewable paths in clone — alphabetical fallback "
            "(SPEC-SYSADMIN.1 ERR-SYSADMIN.1-010)",
        )
        yield from iter_repo_files(repo_root, slug, max_files, max_bytes, verbose)
        return

    scored: list[tuple[float, Path]] = []
    for p in pool:
        rel = p.relative_to(repo_root).as_posix().replace("\\", "/")
        score = _drift_weight(rel)
        if rel in recent:
            score += 52.0
        for hp in hinted:
            if rel == hp or rel.endswith("/" + hp) or hp.endswith(rel):
                score += 45.0
                break
        scored.append((score, p))

    scored.sort(key=lambda t: (-t[0], str(t[1])))

    rnd_n = max(1, max_files // 5)
    top_n = max(0, max_files - rnd_n)

    picks: list[Path] = []
    seen: set[str] = set()

    for _sc, cand in scored[:top_n]:
        rp = cand.relative_to(repo_root).as_posix()
        if rp not in seen:
            picks.append(cand)
            seen.add(rp)

    tail = [p for _sc, p in scored if p.relative_to(repo_root).as_posix() not in seen]
    rnd_take = min(rnd_n, len(tail))
    if rnd_take > 0 and tail:
        for p in rng.sample(tail, rnd_take):
            rp = p.relative_to(repo_root).as_posix()
            if rp not in seen:
                picks.append(p)
                seen.add(rp)

    if len(picks) < min(8, max(1, max_files // 3)):
        log.warning(
            "Smart file pool thin — alphabetical fallback emitted (SPEC-SYSADMIN.1 ERR-SYSADMIN.1-010)",
        )
        yield from iter_repo_files(repo_root, slug, max_files, max_bytes, verbose)
        return

    yield_count = 0
    for p in picks[:max_files]:
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        if len(text) > max_bytes:
            text = text[:max_bytes] + "\n…[truncated]\n"
        yield_count += 1
        yield p.relative_to(repo_root).as_posix(), redact(text)

    if yield_count == 0:
        yield from iter_repo_files(repo_root, slug, max_files, max_bytes, verbose)


def _write_lane_snapshot(eco_root: Path, week_iso: str, full_repo: str, fragment: dict[str, Any]) -> None:
    base = eco_root / "lanes" / week_iso
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    fname = _repo_slug(full_repo) + ".json"
    (base / fname).write_text(json.dumps(fragment, indent=2), encoding="utf-8")


def _brain_upsert_finding(
    full_repo: str,
    finding: dict[str, Any],
    fp: str,
    run_id: str,
    week_iso: str,
    gw_ver: str,
    issue_url: str,
    verbose: bool,
) -> bool:
    mgr = rag_manager_py()
    if not mgr.is_file():
        return False

    enriched = dict(finding)

    cmd = [
        sys.executable,
        str(mgr),
        "--action",
        "upsert-finding",
        "--collection",
        "open_brain",
        "--repo",
        full_repo,
        "--finding-json",
        json.dumps(enriched),
        "--run-id",
        run_id,
        "--week-iso",
        week_iso,
        "--gateway-version",
        gw_ver,
        "--issue-url",
        issue_url or "",
        "--fingerprint",
        fp,
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if cp.returncode != 0:
        if verbose:
            log.warning("brain upsert failed fp=%s err=%s", fp, (cp.stderr or "")[:600])
        return False
    return True


def _build_tree_summary(repo_root: Path, max_lines: int = 200) -> str:
    lines: list[str] = []

    def _walk(rp: Path, depth: int, prefix: str) -> None:
        if len(lines) >= max_lines or depth > 4:
            return
        try:
            kids = sorted([x for x in rp.iterdir() if x.name not in _SKIP_PARTS], key=lambda x: x.name)
        except OSError:
            return
        for ch in kids[:40]:
            lines.append(f"{prefix}{ch.name}/" if ch.is_dir() else f"{prefix}{ch.name}")
            if ch.is_dir():
                _walk(ch, depth + 1, prefix + "  ")

    _walk(repo_root, 0, "")
    return "\n".join(lines[:max_lines])


def _parse_findings(raw: str) -> list[dict[str, Any]]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    data = json.loads(s)
    if not isinstance(data, list):
        raise ValueError("expected JSON array")
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        out.append(item)
    return out


def _fingerprint(full_repo: str, f: dict[str, Any]) -> str:
    h = hashlib.sha256(
        f"{full_repo}|{f.get('category','')}|{f.get('affected_path','')}|{f.get('title','')}".encode()
    ).hexdigest()[:12]
    return h


def _gh(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)


def clone_repo(full_repo: str, dest: Path, verbose: bool) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    cp = subprocess.run(["gh", "repo", "clone", full_repo, str(dest), "--", "--depth=1"], capture_output=True, text=True, timeout=300)
    if cp.returncode != 0:
        log.error("gh clone failed %s — %s", full_repo, cp.stderr.strip())
        return False
    return True


def _find_existing_issue(full_repo: str, fp: str) -> tuple[int | None, str | None]:
    owner, name = full_repo.split("/", 1)
    js = _gh(
        "issue",
        "list",
        "--repo",
        full_repo,
        "--label",
        "roho-review",
        "--state",
        "open",
        "--json",
        "number,title",
        "-L",
        "100",
    )
    if js.returncode != 0:
        return None, None
    try:
        rows = json.loads(js.stdout)
    except json.JSONDecodeError:
        return None, None
    needle = f"(fp:{fp})"
    for row in rows:
        if needle in (row.get("title") or ""):
            return int(row["number"]), row.get("title")
    return None, None


def _append_redetect(full_repo: str, num: int, block: str) -> None:
    v = _gh("issue", "view", str(num), "--repo", full_repo, "--json", "body")
    body = ""
    try:
        body = json.loads(v.stdout)["body"]
    except Exception:
        body = ""
    new_body = body.rstrip() + "\n\n" + block.strip() + "\n"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(new_body)
        tmp_path = tf.name
    subprocess.run(["gh", "issue", "edit", str(num), "--repo", full_repo, "--body-file", tmp_path], check=False, timeout=60)
    Path(tmp_path).unlink(missing_ok=True)


def _file_markdown_issue(title: str, body: str, labels: list[str], short_repo: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as bf:
        bf.write(body)
        bfp = bf.name
    lbl = ",".join(labels)
    cmd = [
        sys.executable if (sys.executable) else "python3",
        str(github_manager_py()),
        "--action",
        "create-issue",
        "--repo",
        short_repo,
        "--title",
        title,
        "--labels",
        lbl,
        "--body-file",
        bfp,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    Path(bfp).unlink(missing_ok=True)
    url = ""
    for line in (proc.stdout or "").splitlines():
        if line.strip().startswith("URL:"):
            url = line.split("URL:", 1)[1].strip()
            break
    if proc.returncode != 0:
        log.error("create-issue-markdown failed: %s %s", proc.stderr, proc.stdout)
    return url


def _post_mm(channel_spec: str, message: str) -> bool:
    if not mattermost_bridge_py().is_file():
        return False
    cp = subprocess.run(
        [sys.executable, str(mattermost_bridge_py()), "--action", "post", "--channel", channel_spec, "--message", message],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return cp.returncode == 0


def _chat(
    gw_base: str,
    token: str,
    provider_model_hint: str,
    messages: list[dict[str, str]],
    timeout: int,
    verbose: bool,
) -> tuple[str, dict[str, int], str]:
    """Call gateway ``/v1/chat/completions``.

    The JSON ``model`` field MUST be a gateway route id (``openclaw`` or ``openclaw/<agentId>``).
    ``provider_model_hint`` is the operator/cron preference (e.g. reasoner slug); it is logged only —
    actual provider routing comes from the agent's ``openclaw.json`` (Issues #304, #305).
    """
    hdr = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    url = f"{gw_base.rstrip('/')}/v1/chat/completions"
    primary_route = ECOSYSTEM_GATEWAY_CHAT_MODEL
    fallback_routes: tuple[str, ...] = ()
    if primary_route == "openclaw":
        fallback_routes = ("openclaw/main",)

    def one(route: str) -> requests.Response:
        return requests.post(
            url,
            headers=hdr,
            json={
                "model": route,
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 8192,
            },
            timeout=timeout,
        )

    backoff = [1.0, 2.0, 4.0]
    last_exc: Exception | None = None

    def call_with_retries(route: str) -> requests.Response | None:
        nonlocal last_exc
        last_exc = None
        for i, delay in enumerate(backoff):
            try:
                resp = one(route)
                if resp.status_code in (502, 503, 504) and i < len(backoff) - 1:
                    time.sleep(delay)
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if i < len(backoff) - 1:
                    time.sleep(delay)
        return None

    attempted_route = primary_route
    resp = call_with_retries(attempted_route)
    if resp is None:
        raise RuntimeError(f"GATEWAY_UNREACHABLE: {last_exc}")

    def _invalid_model(resp_obj: requests.Response) -> bool:
        t = (resp_obj.text or "").lower()
        return resp_obj.status_code == 400 and ("invalid" in t and "model" in t)

    # Try explicit agent route if bare openclaw is rejected by this gateway build.
    if resp.status_code >= 400 and (
        _invalid_model(resp) or resp.status_code == 410 or "model_retired" in (resp.text or "").lower()
    ):
        for alt in fallback_routes:
            log.warning(
                "gateway chat route %r HTTP %s — retrying provider_hint=%r route=%r",
                attempted_route,
                resp.status_code,
                provider_model_hint,
                alt,
            )
            attempted_route = alt
            resp = call_with_retries(attempted_route)
            if resp is not None and resp.status_code < 400:
                break
        if resp is None:
            raise RuntimeError("MODEL_UNAVAILABLE")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from gateway: {exc}") from exc

    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    usage = payload.get("usage") or {}
    umap = {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }
    if verbose:
        log.info(
            "LLM gateway_route=%s provider_hint=%s usage=%s",
            attempted_route,
            provider_model_hint,
            umap,
        )

    return content, umap, attempted_route


def _issue_body(
    f: dict[str, Any],
    gw: str,
    run_id: str,
    fp: str,
    doc_gaps: list[str],
) -> str:
    gaps = ""
    if doc_gaps:
        gaps = "\n## Documentation Gaps\n" + "\n".join(f"- {g}" for g in doc_gaps) + "\n"
    ev = str(f.get("evidence", ""))[:2000]
    lines = f.get("affected_lines")
    line_bits = f" (`{lines}`)" if lines else ""
    docref = f.get("openclaw_doc_reference") or "_None — finding inferred from cross-repo consistency_"
    return f"""**Severity:** {f.get("severity")}
**Category:** {f.get("category")}
**Affected path:** `{f.get("affected_path", "*")}`{line_bits}
**Estimated effort:** {f.get("estimated_effort", "small")}
**Detected by:** Roho weekly ecosystem review ({SPEC_ID}), gateway v{gw}, run `{run_id}` (fp:{fp})

## Summary
{f.get("summary", "")}

## Evidence
```
{ev}
```

## OpenClaw documentation reference
{docref}

## Suggested resolution
{f.get("suggested_resolution", "")}

## Why this severity
{f.get("rationale_for_severity", "")}

---
_Filed automatically by `system-admin --action ecosystem-review`. Reply on this issue to flag a false positive — Roho will record the fingerprint and suppress on future runs._
{gaps}
"""


def run_weekly_ecosynthesis(
    *,
    model: str,
    dry_run: bool,
    verbose: bool,
    out_dir: Path | None,
) -> tuple[int, dict[str, Any]]:
    """SPEC-SYSADMIN-002.1 synthesis pass — Brain + flash patterns + last-run v1.1."""
    import sys as _sys

    if verbose:
        log.setLevel(logging.INFO)
        if not log.handlers:
            lh = logging.StreamHandler(_sys.stderr)
            lh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            log.addHandler(lh)

    started = dt.datetime.now(dt.timezone.utc)
    eco_root = Path(out_dir).expanduser().resolve() if out_dir else ecosystem_review_cache()
    eco_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    week_iso = _week_iso_utc()
    gw = detect_gateway_version(verbose)
    gw_base = resolve_gateway_base(verbose)
    token = resolve_gateway_token()

    result: dict[str, Any] = {
        "schema_version": "1.1",
        "spec_id": SPEC_ID,
        "spec_amendment": "SPEC-SYSADMIN-002.1",
        "run_type": "synthesis",
        "error_code": None,
        "week_iso": week_iso,
        "gateway_version": gw,
        "provider_model_preference": model,
        "warnings": [],
        "rollup_message": "",
        "cross_repo_patterns": [],
        "synthesis": {},
        "lanes": {},
    }

    if dry_run:
        log.info("synthesis dry-run — would query Brain for week %s", week_iso)
        result["patterns"] = []
        result["totals"] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        result["trend_7d"] = {"filed": 0, "updated": 0, "closed": 0, "suppressed": 0}
        return 0, result

    if not token:
        result["error_code"] = "MISSING_GATEWAY_TOKEN"
        return 1, result

    brain_ok = True
    week_rows: list[dict[str, Any]] = []
    brain_payload = _rag_query_where_raw({"source": "roho_review", "week_iso": week_iso}, None, 500, verbose)
    if not brain_payload or brain_payload.get("action") != "query-findings":
        brain_ok = False
        result["warnings"].append("BRAIN_UNREACHABLE_DEGRADED_SYNTHESIS")
        log.warning("Synthesis: Brain query for week %s failed — degraded rollup", week_iso)
    else:
        week_rows = list(brain_payload.get("findings") or [])

    lane_dir = eco_root / "lanes" / week_iso
    lanes: dict[str, Any] = {}
    if lane_dir.is_dir():
        for pth in sorted(lane_dir.glob("*.json")):
            try:
                lanes[pth.stem] = json.loads(pth.read_text(encoding="utf-8"))
            except Exception:
                continue
    result["lanes"] = lanes

    lines: list[str] = []
    for row in week_rows[:450]:
        if not isinstance(row, dict):
            continue
        md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        fp = md.get("fingerprint") or row.get("id")
        repo = md.get("repo") or ""
        lines.append(json.dumps({
            "repo": repo,
            "fingerprint": fp,
            "severity": md.get("severity"),
            "category": md.get("category"),
            "status": md.get("status"),
            "summary": (row.get("document") or "")[:420],
            "title": md.get("finding_title"),
        }, separators=(",", ":")))

    brain_blob = "\n".join(lines)[:58_000]
    patt_raw = "[]"
    syn_in = syn_out = 0
    if brain_blob.strip():
        s_msgs = [
            {"role": "system", "content": SYNTHESIS_SYSTEM_TEMPLATE},
            {"role": "user", "content": SYNTHESIS_USER_TEMPLATE.format(week_iso=week_iso, brain_lines=brain_blob)},
        ]
        try:
            patt_raw, su, _r = _chat(gw_base, token, model, s_msgs, timeout=240, verbose=verbose)
            syn_in += su.get("prompt_tokens", 0)
            syn_out += su.get("completion_tokens", 0)
        except RuntimeError as exc:
            result["warnings"].append(f"synthesis_llm_failed:{exc}")
            patt_raw = "[]"

    patterns: list[dict[str, Any]] = []
    try:
        ptxt = patt_raw.strip()
        if ptxt.startswith("```"):
            ptxt = re.sub(r"^```[a-zA-Z0-9]*\n", "", ptxt)
            ptxt = re.sub(r"\n```\s*$", "", ptxt)
        parsed_pt = json.loads(ptxt or "[]")
        if isinstance(parsed_pt, list):
            patterns = [x for x in parsed_pt if isinstance(x, dict)]
    except (json.JSONDecodeError, ValueError):
        patterns = []

    tot_sev = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for row in week_rows:
        if not isinstance(row, dict):
            continue
        md = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        sev = str(md.get("severity") or "medium").lower()
        if sev in tot_sev:
            tot_sev[sev] += 1

    # Fallback to per-lane findings aggregation if week_rows is empty but lanes exist (Roho Issue #11)
    if not any(tot_sev.values()) and lanes:
        for lane_data in lanes.values():
            if isinstance(lane_data, dict):
                # 1. Direct findings list in lane payload
                for finding in lane_data.get("findings", []):
                    if isinstance(finding, dict):
                        sev = str(finding.get("severity") or "medium").lower()
                        if sev in tot_sev:
                            tot_sev[sev] += 1
                # 2. Pre-calculated summary totals in lane payload
                st = lane_data.get("totals") or lane_data.get("summary_totals")
                if isinstance(st, dict):
                    for k in tot_sev:
                        tot_sev[k] += int(st.get(k) or 0)
                # 3. findings_by_repo dict
                fbr = lane_data.get("findings_by_repo") or {}
                if isinstance(fbr, dict):
                    for repo_counts in fbr.values():
                        if isinstance(repo_counts, dict):
                            for k in tot_sev:
                                tot_sev[k] += int(repo_counts.get(k) or 0)

    result["totals"] = tot_sev
    result["trend_7d"] = {
        "filed": len(week_rows),
        "updated": 0,
        "closed": 0,
        "suppressed": 0,
    }

    cost_syn = syn_in * 0.14e-6 + syn_out * 0.28e-6
    if cost_syn > 0.10:
        _file_markdown_issue(
            f"[Roho Synthesis {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}] Cost anomaly",
            f"cost_usd_estimate={cost_syn:.4f} prompt_tokens={syn_in} completion_tokens={syn_out}\n\nSPEC-SYSADMIN-002.1 REQ-SYSADMIN.1-405",
            ["roho-review", "sysadmin-002", "severity:medium"],
            "openclaw-docker",
        )

    crit_pattern = False
    for pt in patterns:
        sevp = str(pt.get("severity", "")).lower()
        repos_a = pt.get("repos_affected") if isinstance(pt.get("repos_affected"), list) else []
        if sevp == "critical" and len(repos_a) >= 2:
            crit_pattern = True

    rollup = (
        f"[Roho] [ECOSYSTEM-REVIEW] Weekly synthesis ({week_iso}) gateway={gw} brain_ok={brain_ok}\n"
        f"This-week rows: {len(week_rows)} | Lane snapshots: {len(lanes)}\n"
        f"Patterns: {len(patterns)} | synthesis_tokens in={syn_in} out={syn_out}\n"
    )
    result["rollup_message"] = rollup
    _post_mm(DEFAULT_AGENT_ROHO_CID, rollup)
    if crit_pattern:
        _post_mm(DEFAULT_ALERTS_CID, f"@don [SYNTHESIS] Critical cross-repo pattern detected — see #agent-roho ({week_iso})")

    lr_path = eco_root / "last-run.json"
    ended = dt.datetime.now(dt.timezone.utc)
    result["run_id"] = f"syn-{gw}-{ended.strftime('%Y%m%d%H%M%S')}"
    result["started_at"] = started.isoformat().replace("+00:00", "Z")
    result["ended_at"] = ended.isoformat().replace("+00:00", "Z")
    result["token_usage"] = {"prompt_tokens": syn_in, "completion_tokens": syn_out, "total_tokens": syn_in + syn_out}
    result["estimated_cost_usd"] = round(cost_syn, 4)
    result["brain_ok"] = brain_ok
    result["documentation_gaps"] = []
    result["cross_repo_patterns"] = patterns[:12]
    result["patterns"] = patterns[:12]
    result["synthesis"] = {
        "started_at": result["started_at"],
        "ended_at": result["ended_at"],
        "model": model,
        "tokens": {"prompt": syn_in, "completion": syn_out, "total": syn_in + syn_out},
        "cost_usd": round(cost_syn, 6),
        "patterns_count": len(patterns),
    }
    lr_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0, result


def run_seed_labels() -> int:
    for full in sorted(ECOSYSTEM_REPOS_FULL):
        for name, color, desc in LABELS_DEFAULT:
            cp = subprocess.run(
                ["gh", "label", "create", name, "--repo", full, "--color", color, "--description", desc, "--force"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode != 0 and "already exists" not in (cp.stderr or "").lower():
                log.warning("label %s @ %s: %s", name, full, cp.stderr.strip())
        print(f"seed-labels: {full} OK")
    return 0


def run_ecosystem_review(
    repo_csv: str | None,
    max_files: int,
    max_bytes: int,
    model: str,
    dry_run: bool,
    allow_foreign: bool,
    verbose: bool,
    out_dir: Path | None = None,
    mode: str = "fanout",
) -> tuple[int, dict[str, Any]]:
    import sys as _sys

    if verbose:
        log.setLevel(logging.INFO)
        if not log.handlers:
            lh = logging.StreamHandler(_sys.stderr)
            lh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            log.addHandler(lh)

    result: dict[str, Any] = {
        "spec_id": SPEC_ID,
        "error_code": None,
        "gateway_version": None,
        "gateway_chat_model": ECOSYSTEM_GATEWAY_CHAT_MODEL,
        "provider_model_preference": model,
        "model_used": ECOSYSTEM_GATEWAY_CHAT_MODEL,
        "model_was_fallback": False,
        "repos_reviewed": [],
        "findings_by_repo": {},
        "totals": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "estimated_cost_usd": 0.0,
        "documentation_gaps": [],
        "findings": [],
        "errors": [],
        "files_reviewed": {},
        "brain_upserts": 0,
        "spec_amendment": "SPEC-SYSADMIN-002.1",
    }

    if mode == "synthesis":
        return run_weekly_ecosynthesis(model=model, dry_run=dry_run, verbose=verbose, out_dir=out_dir)

    started_at = dt.datetime.now(dt.timezone.utc)
    week_iso = _week_iso_utc()
    result["week_iso"] = week_iso

    selected: list[str] = []
    if repo_csv and repo_csv.strip():
        raw = [p.strip() for p in repo_csv.split(",") if p.strip()]
        for r in raw:
            normalized = r if "/" in r else f"donorazulume/{r}"
            if normalized not in ECOSYSTEM_REPOS_FULL and not allow_foreign:
                result["error_code"] = "REPO_NOT_IN_ALLOWLIST"
                return 2, result
            selected.append(normalized)
    else:
        selected = sorted(ECOSYSTEM_REPOS_FULL)

    if mode == "lane" and len(selected) != 1:
        result["error_code"] = "LANE_REQUIRES_SINGLE_REPO"
        return 2, result

    result["repo"] = ",".join(selected)
    gw = detect_gateway_version(verbose)
    result["gateway_version"] = gw
    gw_base = resolve_gateway_base(verbose)
    token = resolve_gateway_token()
    if not token:
        log.error("OPENCLAW_GATEWAY_TOKEN unset and absent from openclaw.json gateway.auth.token")
        result["errors"].append("missing gateway token")

    docs_md, gaps = fetch_docs_action(verbose)
    result["documentation_gaps"] = gaps

    run_id = f"{gw}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')}-{(os.urandom(3).hex())}"
    if out_dir is not None:
        eco_root = Path(out_dir).expanduser().resolve()
        eco_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    else:
        eco_root = ecosystem_review_cache()
    cum_in = cum_out = 0

    if dry_run:
        log.info("dry-run: would review repos=%s model=%s", selected, model)
        for full in selected:
            log.info(" planned repo=%s shallow-clone glob walk max_files=%s", full, max_files)
        result["repos_reviewed"] = selected
        result["files_reviewed"] = {r: [] for r in selected}
        return 0, result

    rundir = eco_root / run_id
    rundir.mkdir(parents=True, exist_ok=True)

    if not token:
        return 1, result

    docs_summary, sum_u = ensure_docs_summary(docs_md, gw_base, token, gw, verbose)
    cum_in += sum_u.get("prompt_tokens", 0)
    cum_out += sum_u.get("completion_tokens", 0)
    brain_upserts_tot = 0

    for full in selected:
        slug_short = full.split("/", 1)[1]
        if cum_in >= TOKEN_IN_CAP or cum_out >= TOKEN_OUT_CAP:
            result["error_code"] = "TOKEN_BUDGET_EXCEEDED"
            break

        td = tempfile.mkdtemp(prefix="roho_review_")
        checkout = Path(td) / slug_short
        try:
            if not clone_repo(full, checkout, verbose):
                result["errors"].append(f"clone_failed:{full}")
                continue

            prior = _rag_query_findings_raw(full, 8, 30, verbose)
            ob, sb, ocn, scn = _format_prior_sections(prior)

            system_prompt = SYSTEM_LANE_TEMPLATE.format(
                gateway_version=gw,
                docs_summary_8k=docs_summary[:96000],
                open_findings_block=ob,
                suppressed_findings_block=sb,
                open_count=ocn,
                suppressed_count=scn,
            )

            chunks = []
            rels = []
            for rel, txt in iter_repo_files_smart(checkout, slug_short, full, max_files, max_bytes, verbose):
                chunks.append(f"### {rel}\n```\n{txt}\n```\n")
                rels.append(rel)
            result["files_reviewed"][full] = rels
            tree = _build_tree_summary(checkout)

            per_cap = TOKEN_IN_CAP // max(1, len(selected))
            user_prompt = USER_LANE_TEMPLATE.format(
                repo=full,
                gateway_version=gw,
                tree_summary=tree[:8000],
                max_bytes_per_file=max_bytes,
                file_contents="\n".join(chunks)[: max(per_cap // 2, 120_000)],
            )
            msgs = [
                {"role": "system", "content": system_prompt[:400_000]},
                {"role": "user", "content": user_prompt[:400_000]},
            ]

            repo_findings_arr: list[dict[str, Any]] = []

            date_prefix = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
            raw = ""
            usage: dict[str, int] = {}
            gateway_route_used = ECOSYSTEM_GATEWAY_CHAT_MODEL
            try:
                raw, usage, gateway_route_used = _chat(gw_base, token, model, msgs, timeout=600, verbose=verbose)
                cum_in += usage.get("prompt_tokens", 0)
                cum_out += usage.get("completion_tokens", 0)
            except RuntimeError as exc:
                err = str(exc)
                log.error("_chat repo=%s failed: %s", full, err)
                if "MODEL_UNAVAILABLE" in err:
                    result["error_code"] = "MODEL_UNAVAILABLE"
                result["errors"].append(f"gateway_chat_failed:{full}:{err[:160]}")
                continue
            if gateway_route_used != ECOSYSTEM_GATEWAY_CHAT_MODEL:
                result["model_was_fallback"] = True
            result["model_used"] = gateway_route_used
            try:
                repo_findings_arr = _parse_findings(raw)
            except (json.JSONDecodeError, ValueError):
                retry_raw = ""
                try:
                    msgs2 = msgs + [{"role": "user", "content": STRICT_RETRY}]
                    raw2, usage2, _ = _chat(
                        gw_base, token, model, msgs2, timeout=300, verbose=verbose
                    )
                    cum_in += usage2.get("prompt_tokens", 0)
                    cum_out += usage2.get("completion_tokens", 0)
                    retry_raw = raw2
                    repo_findings_arr = _parse_findings(raw2)
                except (json.JSONDecodeError, ValueError, RuntimeError):
                    (rundir / f"{slug_short}.raw.txt").write_text(
                        raw + "\n\n---RETRY---\n" + retry_raw, encoding="utf-8"
                    )
                    result["errors"].append(f"LLM_RESPONSE_INVALID:{full}")
                    repo_findings_arr = []
            result["repos_reviewed"].append(full)
            fbr: dict[str, Any] = {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "issues_filed": [],
                "issues_updated": [],
                "skipped_dedup": 0,
            }
            sev_known = frozenset(fbr.keys())
            repo_brain = 0
            for f in repo_findings_arr:
                sev = str(f.get("severity", "medium")).lower()
                if sev in fbr:
                    fbr[sev] += 1
                if sev in sev_known:
                    result["totals"][sev] += 1  # type: ignore[operator]

                fp = _fingerprint(full, f)
                title_base = str(f.get("title", "Finding"))[:80]
                issue_title = f"[Roho Weekly Review {date_prefix}] {title_base} (fp:{fp})"

                body = _issue_body(f, gw, run_id, fp, gaps)
                lbl = ["roho-review", "enhancement", "sysadmin-002", f"severity:{sev}"]

                if cum_in >= TOKEN_IN_CAP or cum_out >= TOKEN_OUT_CAP:
                    result["error_code"] = "TOKEN_BUDGET_EXCEEDED"
                    break

                num, _ = _find_existing_issue(full, fp)
                mention = "@don "
                plain_summary = (
                    f"{mention}[ECOSYSTEM-REVIEW CRITICAL]\nRepo: {full}\n{title_base}\nSeverity: critical\n(fp:{fp})"
                    if sev == "critical"
                    else ""
                )

                issue_url = ""
                if num:
                    blk = f"## Re-detected on {date_prefix}\n{summary_block(f)}\n"
                    _append_redetect(full, num, blk)
                    fbr["issues_updated"].append(f"https://github.com/{full}/issues/{num}")
                    issue_url = f"https://github.com/{full}/issues/{num}"
                    fbr["skipped_dedup"] = int(fbr.get("skipped_dedup") or 0) + 1
                else:
                    issue_url = _file_markdown_issue(issue_title, body, lbl, slug_short)
                    if issue_url:
                        fbr["issues_filed"].append(issue_url)
                    if sev == "critical" and plain_summary:
                        _post_mm(DEFAULT_ALERTS_CID, plain_summary)
                    elif sev == "high":
                        _post_mm(DEFAULT_AGENT_ROHO_CID, f"[ECOSYSTEM-REVIEW HIGH] {full}\n{title_base}\n(fp:{fp})")

                tiny = dict(f)
                tiny["github_issue_url"] = issue_url or None
                tiny["skipped_reason"] = "dedup_existing_issue" if num else None
                tiny["fingerprint"] = fp
                result["findings"].append(tiny)

                if _brain_upsert_finding(full, f, fp, run_id, week_iso, gw, issue_url or "", verbose):
                    brain_upserts_tot += 1
                    repo_brain += 1

            result["findings_by_repo"][full] = fbr

            _write_lane_snapshot(
                eco_root,
                week_iso,
                full,
                {
                    "repo": full,
                    "week_iso": week_iso,
                    "run_id": run_id,
                    "gateway_version": gw,
                    "provider_model_preference": model,
                    "findings_by_repo": {full: fbr},
                    "files_reviewed": rels,
                    "token_usage_accum": {"prompt_tokens": cum_in, "completion_tokens": cum_out},
                    "brain_upserts": repo_brain,
                },
            )
        finally:
            shutil.rmtree(td, ignore_errors=True)

        if result.get("error_code") == "TOKEN_BUDGET_EXCEEDED":
            break
        if verbose:
            soft_warn = cum_in + cum_out >= TOTAL_TOKEN_WARN
            log.info(
                "cumulative_tokens in=%s out=%s soft_warn=%s",
                cum_in,
                cum_out,
                soft_warn,
            )

    result["token_usage"] = {"prompt_tokens": cum_in, "completion_tokens": cum_out, "total_tokens": cum_in + cum_out}
    # rough DeepSeek-style pricing placeholder (SPEC spot-check): $0.14/M in, $0.28/M out-ish
    result["estimated_cost_usd"] = round(cum_in * 0.14e-6 + cum_out * 0.28e-6, 4)

    result["brain_upserts"] = brain_upserts_tot
    result["run_id"] = run_id
    result["started_at"] = started_at.isoformat().replace("+00:00", "Z")
    result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    (rundir / "usage.json").write_text(json.dumps({"token_usage": result["token_usage"], "usd": result["estimated_cost_usd"]}, indent=2), encoding="utf-8")

    code = 0
    if result.get("error_code") == "TOKEN_BUDGET_EXCEEDED":
        code = 1
        bd_title = f"[Roho Weekly Review {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')}] Budget exceeded — partial review (fp:budget-{run_id[:8]})"
        bd_body = f"Cumulative tokens prompt={cum_in} completion={cum_out}. Run aborted per SPEC-SYSADMIN-002.\n"
        _file_markdown_issue(bd_title, bd_body, ["roho-review", "sysadmin-002", "severity:medium"], "openclaw-docker")
        _post_mm(DEFAULT_AGENT_ROHO_CID, "[ECOSYSTEM-REVIEW] Token budget exceeded — partial results and tracking issue filed in openclaw-docker.")
    elif (
        selected
        and not result["repos_reviewed"]
        and result.get("error_code") != "TOKEN_BUDGET_EXCEEDED"
        and result["errors"]
        and any(str(e).startswith("gateway_chat_failed:") for e in result["errors"])
    ):
        result.setdefault("error_code", "ECOSYSTEM_REVIEW_NO_LLM_OUTPUT")
        code = 1

    return code, result


def summary_block(f: dict[str, Any]) -> str:
    return f"- **{f.get('title')}** ({f.get('severity')}) `{f.get('affected_path','*')}`"
