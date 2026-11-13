#!/usr/bin/env python3
"""Programmatic orchestrator for compiling and delivering the LifeOS Morning Briefing.

Performs briefing compilation (server-side with local fallbacks), emails a reference copy
to donorazulume@gmail.com, sends an actionable summary DM to Don, and logs the execution.
"""

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types
from datetime import datetime, time, timezone
from typing import Any

# Force line-buffering on stdout/stderr to prevent OpenClaw noOutputTimeout aborts (Issue #908)
try:
    _reconfig_out = getattr(sys.stdout, "reconfigure", None)
    if callable(_reconfig_out):
        _reconfig_out(line_buffering=True)
    _reconfig_err = getattr(sys.stderr, "reconfigure", None)
    if callable(_reconfig_err):
        _reconfig_err(line_buffering=True)
except Exception:
    pass

import requests  # type: ignore

# Ensure we can resolve local imports from this directory
lib_dir = str(pathlib.Path(__file__).resolve().parent)
if lib_dir not in sys.path:
    sys.path.insert(0, lib_dir)

import email_utils
import mcp_bible
import mcp_google
import mcp_lifeos
import mcp_m365


def build_bible_section() -> str:
    """Fetch daily devotional reading and verse of the day from openclaw-mcp-bible.
    
    Provides strict graceful degradation so briefing generation never fails even if Bible MCP
    is unreachable or returns an error.
    """
    try:
        devotional = mcp_bible.call("get_daily_devotional")
        if isinstance(devotional, dict) and not devotional.get("error"):
            title = devotional.get("title", "Daily Devotional")
            theme = devotional.get("theme", "")
            date_str = devotional.get("date", "")
            ref = devotional.get("bible_reference", "")
            body = devotional.get("body", "")
            
            section = "\n\n### 📖 Scripture & Daily Devotional (UCB Word For Today)\n"
            if date_str:
                section += f"**Date**: {date_str}\n"
            if theme:
                section += f"**Theme**: {theme}\n"
            section += f"**Title**: {title}\n"
            if ref:
                section += f"**Reading**: {ref}\n"
            if body:
                snippet = body if len(body) <= 500 else body[:500] + "..."
                section += f"\n> {snippet}\n"
            return section
    except Exception as exc:
        print(f"WARNING: build_bible_section failed gracefully: {exc}")
    return ""


def resolve_bridge_path():
    home = os.environ.get("HOME", "/home/node")
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    candidates = [
        os.path.join(home, ".openclaw", "workspace", "skills", "mattermost-bridge", "bridge.py"),
        os.path.join(str(base_dir), "skills", "mattermost-bridge", "bridge.py"),
        "/home/node/.openclaw/workspace/skills/mattermost-bridge/bridge.py",
        os.path.join(home, ".openclaw", "skills", "mattermost-bridge", "bridge.py"),
        "/home/node/.openclaw/skills/mattermost-bridge/bridge.py",
        "/home/node/amara/.openclaw/skills/mattermost-bridge/bridge.py",
        "/home/node/rob/.openclaw/skills/mattermost-bridge/bridge.py",
        "/workspace/skills/mattermost-bridge/bridge.py",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def resolve_script_path(rel_path):
    home = os.environ.get("HOME", "/home/node")
    base_dir = pathlib.Path(__file__).resolve().parents[2]
    candidates = [
        os.path.join(home, ".openclaw", "workspace", rel_path),
        os.path.join(str(base_dir), rel_path),
        os.path.join("/workspace", rel_path),
        os.path.join("/home/node/.openclaw/workspace", rel_path),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]

def resolve_workspace_file(filename: str) -> str:
    import tempfile
    home = os.environ.get("HOME", "/home/node")
    candidates = [
        os.environ.get("OPENCLAW_WORKSPACE_DIR"),
        os.environ.get("OPENCLAW_STATE_DIR"),
        os.path.join(home, ".openclaw", "workspace"),
        "/home/node/.openclaw/workspace",
        "/workspace",
        tempfile.gettempdir(),
        "/tmp",
    ]
    for d in candidates:
        if not d:
            continue
        try:
            p = pathlib.Path(d)
            if p.is_dir() and os.access(p, os.W_OK):
                return str(p / filename)
            elif not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                if os.access(p, os.W_OK):
                    return str(p / filename)
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), filename)

DEFAULT_LIFEOS_BRIEF_MODEL = "openai-compatible/deepseek-v4-pro"

CANONICAL_SECTION_SCHEMA: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("1. Headline & Grounding", ("headline", "grounding", "executive", "overview", "posture", "summary"), None),
    ("2. Today's Priorities", ("priorit", "action item", "immediate", "hitl", "decision"), None),
    ("3. Calendar & Commitments", ("calendar", "schedule", "commitment", "meeting", "conflict"), None),
    ("4. Alerts & Integrity Warnings", ("alert", "integrity", "warning", "stale", "degraded", "escalation", "push failed"), "heatmap"),
    ("5. Statutory & Compliance", ("statutory", "compliance", "filing", "hmrc", "companies house", "ct600", "penalty"), None),
    ("6. Crypto & Macro", ("crypto", "macro", "btc", "coingecko", "market"), None),
    ("7. Portfolio & Ledger", ("portfolio", "ledger", "trading 212", "equit", "finance", "financial"), "portfolio_trend"),
    ("8. Property & Legal", ("property", "legal", "amara", "tenan", "holmbury", "possession"), None),
    ("9. Documents & Executive Inbox", ("document", "letter", "inbox", "correspondence", "mail"), None),
    ("10. Deltas & Daily Scripture", ("scripture", "devotional", "bible", "ucb", "delta", "state change"), None),
)


def resolve_lifeos_brief_model() -> str:
    """Resolve explicit LifeOS briefing synthesis model (REQ-LIFEOS-211 / NARR-003)."""
    for env_key in ("LIFEOS_BRIEF_MODEL", "OPENCLAW_GATEWAY_MODEL"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val
    return DEFAULT_LIFEOS_BRIEF_MODEL


def _strip_provider_prefix(model_name: str) -> str:
    for prefix in ("openai-compatible/", "deepseek/"):
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name


def _resolve_gateway_model_for_url(url: str, resolved_model: str) -> str:
    """Map model IDs to 'openclaw*' when targeting an OpenClaw HTTP gateway (:18789/:18790/:18791) (REQ-LIFEOS-219)."""
    if any(port in url for port in (":18789", ":18790", ":18791")):
        if resolved_model.startswith("openclaw"):
            return resolved_model
        return "openclaw"
    return _strip_provider_prefix(resolved_model)


def call_deepseek_gateway(system_prompt, user_prompt="", model=None, max_retries=2):
    if isinstance(system_prompt, list):
        messages = system_prompt
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    resolved_model = (model or "").strip() or resolve_lifeos_brief_model()
    explicit_gw = os.environ.get("DEEPSEEK_GATEWAY_URL", "").strip()
    ds_key = (os.environ.get("DEEPSEEK_API_KEY_ROHO") or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    attempt_timeout_s = int(os.environ.get("LIFEOS_LLM_ATTEMPT_TIMEOUT_S", "95"))

    if not explicit_gw and ds_key and "deepseek" in resolved_model.lower():
        direct_url = os.environ.get("DEEPSEEK_API_URL", "https://api.deepseek.com/v1/chat/completions")
        direct_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ds_key}"}
        direct_payload = {
            "model": _strip_provider_prefix(resolved_model),
            "messages": messages,
            "temperature": 0.1,
        }
        last_direct_err = None
        for d_attempt in range(1, max(1, max_retries) + 1):
            try:
                resp = requests.post(direct_url, json=direct_payload, headers=direct_headers, timeout=attempt_timeout_s)
                resp.raise_for_status()
                data = resp.json()
                if "choices" in data and len(data["choices"]) > 0:
                    msg = data["choices"][0].get("message") or {}
                    return msg.get("content") or msg.get("reasoning_content") or ""
            except Exception as exc:
                last_direct_err = exc
                print(f"Warning: direct DeepSeek API synthesis attempt {d_attempt}/{max_retries} failed ({exc}).")
                if d_attempt < max_retries:
                    import time as _time
                    _time.sleep(d_attempt)
        if os.environ.get("LIFEOS_ALLOW_GATEWAY_FALLBACK") != "1":
            raise RuntimeError(f"Direct DeepSeek synthesis failed after {max_retries} attempts: {last_direct_err}")

    url = explicit_gw or "http://openclaw:18789/v1/chat/completions"
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": _resolve_gateway_model_for_url(url, resolved_model),
        "messages": messages,
        "temperature": 0.1,
    }
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=attempt_timeout_s)
            resp.raise_for_status()
            data = resp.json()
            if "choices" in data and len(data["choices"]) > 0:
                return data["choices"][0]["message"].get("content", "")
            else:
                raise RuntimeError(f"Unexpected gateway completions response: {data}")
        except Exception as exc:
            last_err = exc
            print(f"Warning: call_deepseek_gateway attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                import time as _time
                _time.sleep(attempt * 2)
    raise RuntimeError(f"All {max_retries} gateway attempts failed: {last_err}")


def _match_canonical_slot(heading: str) -> int:
    h_lower = heading.strip().lower()
    num_match = re.match(r"^(10|[1-9])\.\s+", h_lower)
    explicit_idx = (int(num_match.group(1)) - 1) if num_match else None

    best_idx = -1
    best_score = 0
    for idx, (_canonical_heading, keywords, _chart_key) in enumerate(CANONICAL_SECTION_SCHEMA):
        score = sum(4 for kw in keywords if kw in h_lower)
        if explicit_idx == idx:
            score += 1
        if score > best_score:
            best_score = score
            best_idx = idx
    if best_idx >= 0:
        return best_idx
    if explicit_idx is not None and 0 <= explicit_idx < len(CANONICAL_SECTION_SCHEMA):
        return explicit_idx
    return 0


def normalize_briefing_sections(
    markdown_brief: str,
    briefing_res: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Normalize Markdown briefing into the 10-slot canonical section schema (REQ-LIFEOS-209, REQ-LIFEOS-225)."""
    text = (markdown_brief or "").strip()
    lines = text.splitlines()
    doc_title = "LifeOS Morning Briefing"

    has_h2 = any(re.match(r"^##\s+\S", ln) for ln in lines)
    slot_chunks: list[list[str]] = [[] for _ in CANONICAL_SECTION_SCHEMA]
    current_slot = 0

    for ln in lines:
        h1_m = re.match(r"^#\s+(.+)$", ln)
        if h1_m:
            if doc_title == "LifeOS Morning Briefing":
                doc_title = h1_m.group(1).strip()
            continue
        h2_m = re.match(r"^##\s+(.+)$", ln)
        h3_scripture_m = re.match(r"^###\s+(.+(?:Scripture|Devotional|UCB Word For Today).*)$", ln, flags=re.IGNORECASE)
        h3_fallback_m = re.match(r"^###\s+(.+)$", ln) if not has_h2 else None
        chosen = h2_m or h3_scripture_m or h3_fallback_m
        if chosen:
            heading_text = chosen.group(1).strip()
            current_slot = _match_canonical_slot(heading_text)
            continue
        slot_chunks[current_slot].append(ln)

    roho_fresh = False
    if isinstance(briefing_res, dict):
        st_map = briefing_res.get("staleness") or ((briefing_res.get("meta") or {}).get("staleness") if isinstance(briefing_res.get("meta"), dict) else None) or {}
        if isinstance(st_map, dict) and isinstance(st_map.get("ROHO"), dict):
            roho_fresh = st_map["ROHO"].get("stale") is False

    sections_out: list[dict[str, Any]] = []
    for idx, (canonical_heading, _keywords, chart_key) in enumerate(CANONICAL_SECTION_SCHEMA):
        raw_md = "\n".join(slot_chunks[idx]).strip()
        if raw_md:
            body_html = email_utils.markdown_to_html(raw_md)
            populated = True
        elif idx == 2 and roho_fresh:
            body_html = "<p>No calendar events scheduled for today.</p>"
            populated = True
        else:
            body_html = "<p><em>No updates recorded for this domain today.</em></p>"
            populated = False
        sections_out.append(
            {
                "heading": canonical_heading,
                "body_html": body_html,
                "chart_key": chart_key,
                "populated": populated,
            }
        )
    return doc_title, sections_out


def _resolve_visual_gen_dir() -> pathlib.Path:
    skill_root = pathlib.Path(__file__).resolve().parents[1] / "visual-gen"
    candidates = [
        skill_root,
        pathlib.Path("/home/node/.openclaw/skills/visual-gen"),
        pathlib.Path("/home/node/.openclaw/workspace/skills/visual-gen"),
    ]
    for cand in candidates:
        if (cand / "lib" / "report_builder.py").is_file():
            return cand
    return skill_root


def _load_visual_gen_modules() -> tuple[Any, Any]:
    vg_dir = _resolve_visual_gen_dir()
    lib_path = vg_dir / "lib"
    if not (lib_path / "report_builder.py").is_file():
        raise FileNotFoundError(f"visual-gen report_builder.py not found under {vg_dir}")

    pkg_name = "_lifeos_visual_gen_lib"
    pkg = sys.modules.get(pkg_name)
    if pkg is None:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(lib_path)]  # type: ignore[attr-defined]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg

    def _load_sub(sub_name: str) -> Any:
        full_name = f"{pkg_name}.{sub_name}"
        if full_name in sys.modules:
            return sys.modules[full_name]
        spec = importlib.util.spec_from_file_location(full_name, lib_path / f"{sub_name}.py")
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load {full_name}")
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = pkg_name
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
        return mod

    themes_mod = _load_sub("themes")
    report_builder_mod = _load_sub("report_builder")
    return themes_mod, report_builder_mod


def _build_visual_briefing_html(
    markdown_brief: str,
    briefing_res: dict[str, Any] | None = None,
) -> str:
    """Build standalone Gmail-safe HTML briefing with base64 PNG charts via skills/visual-gen."""
    themes_mod, report_builder_mod = _load_visual_gen_modules()
    doc_title, sections = normalize_briefing_sections(markdown_brief, briefing_res=briefing_res)

    chart_payload: dict[str, Any] = {}
    date_str = datetime.now().strftime("%Y-%m-%d")
    if isinstance(briefing_res, dict):
        if isinstance(briefing_res.get("date"), str) and briefing_res["date"]:
            date_str = briefing_res["date"]
        raw_cp = briefing_res.get("chart_payload")
        if not isinstance(raw_cp, dict) and isinstance(briefing_res.get("meta"), dict):
            raw_cp = briefing_res["meta"].get("chart_payload")
        if isinstance(raw_cp, dict):
            chart_payload = raw_cp

    portfolio_trend = chart_payload.get("portfolio_trend") if isinstance(chart_payload.get("portfolio_trend"), dict) else None
    heatmap = chart_payload.get("heatmap") if isinstance(chart_payload.get("heatmap"), dict) else None
    if portfolio_trend is None and heatmap is None:
        populated_flags = [1.0 if s.get("populated") else 0.0 for s in sections]
        heatmap = {
            "rows": ["Headline / Prio", "Cal / Alerts", "Stat / Crypto", "Port / Prop", "Docs / Devot"],
            "cols": ["Slot A", "Slot B"],
            "values": [
                [populated_flags[0], populated_flags[1]],
                [populated_flags[2], populated_flags[3]],
                [populated_flags[4], populated_flags[5]],
                [populated_flags[6], populated_flags[7]],
                [populated_flags[8], populated_flags[9]],
            ],
        }

    report_data: dict[str, Any] = {
        "title": doc_title,
        "date": date_str,
        "subtitle": f"OpenClaw LifeOS Executive Briefing · {date_str}",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sections": sections,
    }
    if portfolio_trend is not None:
        report_data["portfolio_trend"] = portfolio_trend
    if heatmap is not None:
        report_data["heatmap"] = heatmap

    theme_dict = themes_mod.load_theme("executive-light")
    tmp_dir = pathlib.Path(tempfile.mkdtemp(prefix="lifeos-brief-html-"))
    try:
        out_html = tmp_dir / "morning-briefing.html"
        report_builder_mod.build_report(
            "morning-briefing",
            report_data,
            out_html,
            theme_dict,
            title=doc_title,
            embed_images=True,
            dpi=96,
        )
        raw_html = out_html.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return email_utils.sanitize_briefing_html(raw_html)


def render_briefing_email_html(
    markdown_brief: str,
    briefing_res: dict[str, Any] | None = None,
) -> str:
    """Render Gmail-safe HTML briefing with charts, degrading gracefully to complete HTML text on failure."""
    try:
        html_doc = _build_visual_briefing_html(markdown_brief, briefing_res=briefing_res)
        if html_doc and html_doc.lstrip().lower().startswith("<!doctype html>"):
            return html_doc
        raise RuntimeError("visual-gen returned incomplete HTML document")
    except Exception as exc:
        print(f"WARNING: Visual briefing HTML render failed ({exc}); degrading to text-only HTML email.")
        return email_utils.wrap_markdown_email_html(markdown_brief, title="LifeOS Morning Briefing")


def recover_missing_producers(date_str: str) -> dict[str, Any]:
    """Poll producer freshness pre-compile and pull-recover missing same-day sections (REQ-LIFEOS-221, REQ-LIFEOS-228)."""
    recovered: list[str] = []
    errors: list[str] = []
    try:
        get_fn = getattr(mcp_lifeos, "get_json", None)
        if not callable(get_fn):
            return {"polled": False, "recovered": recovered, "errors": [], "missing_producer": []}
        res_tuple: Any = get_fn("/lifeos_agent_poll", {"date": date_str}, timeout=15.0)
        status_code, poll_data = res_tuple
        if status_code != 200 or not isinstance(poll_data, dict):
            return {"polled": False, "recovered": recovered, "errors": [f"poll HTTP {status_code}"], "missing_producer": []}
    except Exception as exc:
        return {"polled": False, "recovered": recovered, "errors": [str(exc)], "missing_producer": []}

    raw_agents = poll_data.get("agents")
    if isinstance(raw_agents, dict):
        agents_map: dict[str, Any] = raw_agents
    elif isinstance(poll_data.get("present"), list):
        present_set = {str(a).upper() for a in poll_data["present"]}
        agents_map = {aid: ({"ok": True} if aid in present_set else None) for aid in ("ROHO", "ROB", "AMARA", "LETTER_ANALYST")}
    else:
        agents_map = {}
    if not agents_map.get("ROHO"):
        now_dt = datetime.now(timezone.utc)
        start_iso = datetime.combine(now_dt.date(), time.min, tzinfo=timezone.utc).isoformat()
        end_iso = datetime.combine(now_dt.date(), time.max, tzinfo=timezone.utc).isoformat()
        events_list: list[dict[str, Any]] = []
        try:
            cal_res = mcp_google.call(
                "google_calendar_list_events",
                {"start": start_iso, "end": end_iso, "calendar_id": "primary"},
            )
            if isinstance(cal_res, dict) and isinstance(cal_res.get("events"), list):
                events_list = [e for e in cal_res["events"] if isinstance(e, dict)]
        except Exception as cal_exc:
            errors.append(f"ROHO.calendar_fetch: {cal_exc}")
        now_iso = datetime.now(timezone.utc).isoformat()
        roho_payload = {
            "calendar": {
                "events": events_list,
                "total": len(events_list),
                "fetched_at": now_iso,
                "source": "precompile_pull_recovery",
            },
            "__producer": {
                "name": "run_morning_briefing.py:recover_missing_producers",
                "version": "1.2.0",
                "generated_at": now_iso,
                "source_fetched_at": now_iso,
                "cadence_seconds": 86400,
            },
        }
        try:
            mcp_lifeos.call(
                "tools/lifeos_section_push",
                {"agent_id": "ROHO", "content": json.dumps(roho_payload)},
            )
            recovered.append("ROHO")
        except Exception as push_exc:
            errors.append(f"ROHO.push: {push_exc}")

    for sat_aid in ("ROB", "AMARA", "LETTER_ANALYST"):
        if agents_map.get(sat_aid):
            continue
        cmd_env = os.environ.get(f"LIFEOS_RECOVERY_CMD_{sat_aid}", "").strip()
        if cmd_env:
            try:
                subprocess.run(cmd_env, shell=True, check=True, timeout=90)
                recovered.append(sat_aid)
            except Exception as rec_exc:
                errors.append(f"{sat_aid}.recovery: {rec_exc}")

    # REQ-LIFEOS-228: presence != provenance. A section pushed today with no __producer
    # block is flagged pre-compile rather than silently passing `present`.
    missing_producer: list[str] = []
    poll_producers = poll_data.get("producers")
    if isinstance(poll_producers, dict):
        for aid, info in sorted(poll_producers.items()):
            if isinstance(info, dict) and info.get("missing_producer") and aid not in recovered:
                missing_producer.append(aid)
                errors.append(f"{aid}.missing_producer")

    return {
        "polled": True,
        "recovered": recovered,
        "errors": errors,
        "missing_producer": missing_producer,
    }


def _request_compiled_briefing() -> Any:
    """Invoke explicit compile endpoint, falling back to legacy get_today if mocked response lacks content."""
    res = mcp_lifeos.call("tools/lifeos_briefing_compile", {"force_recompile": False})
    if isinstance(res, dict) and "content" not in res and not res.get("error"):
        return mcp_lifeos.call("tools/lifeos_briefing_get_today")
    return res


# REQ-LIFEOS-228: producer provenance + evidence-age audit gate.
# Observed 2026-11-13: a section can be pushed "today" (staleness ok) while the
# mail corpus behind its prose ends 16 days earlier, and a same-day push can
# omit __producer entirely (missing_producer=true) while staleness still reads ok.
_DATED_CLAIM_RE = re.compile(
    r"(?:\b(?:pending|awaiting|scheduled|to be confirmed|tbc|outstanding|unconfirmed)\b.{0,120}?"
    r"\b(?:confirmation|completion|start|visit|appointment|installation|inspection|booking)\b|"
    r"\b(?:confirmation|completion|start|visit|appointment|installation|inspection|booking)\b.{0,120}?"
    r"\b(?:pending|awaiting|scheduled|to be confirmed|tbc|outstanding|unconfirmed)\b)",
    re.IGNORECASE,
)
_AGENT_TOKEN_RE = re.compile(r"\b(roho|rob|amara|letter[_-]analyst)\b", re.IGNORECASE)
_SLOT_EXCLUSIVE_OWNER: dict[int, str] = {
    2: "roho",
    4: "rob",
    5: "rob",
    6: "rob",
    7: "amara",
}
_DEFAULT_EVIDENCE_MAX_AGE_H = 36


def _parse_iso_utc(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evidence_audit(
    briefing_res_dict: dict[str, Any] | None,
    reference: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return per-agent provenance/evidence gaps as structured facts (REQ-LIFEOS-228).

    Two independent findings, deliberately not collapsed:
      * missing_producer  — push carried no __producer block (provenance absent)
      * evidence_age_h    — how old the claimed source evidence is, in hours
    """
    if not isinstance(briefing_res_dict, dict):
        return []
    ref = reference or datetime.now(timezone.utc)
    raw_meta = briefing_res_dict.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    raw_producers = briefing_res_dict.get("producers") or meta.get("producers")
    producers: dict[str, Any] = raw_producers if isinstance(raw_producers, dict) else {}
    raw_staleness = briefing_res_dict.get("staleness") or meta.get("staleness")
    staleness: dict[str, Any] = raw_staleness if isinstance(raw_staleness, dict) else {}

    findings: list[dict[str, Any]] = []
    for agent_id in sorted(set(producers) | set(staleness)):
        raw_p = producers.get(agent_id)
        prod: dict[str, Any] = raw_p if isinstance(raw_p, dict) else {}
        raw_s = staleness.get(agent_id)
        stale: dict[str, Any] = raw_s if isinstance(raw_s, dict) else {}
        if not prod and not stale:
            continue
        # Reachability guard: a stub/partial payload without a section push (e.g. an
        # offline compile that never contacted LifeOS) is not evidence of a provenance
        # gap. Only judge an agent whose section actually carries a push timestamp.
        if not (prod.get("pushed_at") or stale.get("pushed_at")):
            continue

        ev_ts_raw = (
            prod.get("evidence_fetched_at")
            or prod.get("source_fetched_at")
            or stale.get("source_fetched_at")
        )
        source_fetched = _parse_iso_utc(ev_ts_raw)
        age_h: float | None = None
        if source_fetched is not None:
            age_h = round(max(0.0, (ref - source_fetched).total_seconds()) / 3600.0, 1)

        raw_max_age = prod.get("evidence_max_age_h")
        max_age_h = (
            int(raw_max_age)
            if isinstance(raw_max_age, (int, float)) and not isinstance(raw_max_age, bool) and raw_max_age > 0
            else _DEFAULT_EVIDENCE_MAX_AGE_H
        )
        missing_producer = bool(prod.get("missing_producer"))
        evidence_stale = age_h is not None and age_h > max_age_h
        if not missing_producer and not evidence_stale:
            continue
        findings.append(
            {
                "agent_id": agent_id,
                "missing_producer": missing_producer,
                "section_status": stale.get("status"),
                "source_fetched_at": ev_ts_raw,
                "evidence_age_h": age_h,
                "evidence_max_age_h": max_age_h,
                "evidence_stale": evidence_stale,
            }
        )
    return findings


def _call_lifeos_with_timeout(endpoint: str, body: dict[str, Any], timeout: float = 20.0) -> Any:
    try:
        return mcp_lifeos.call(endpoint, body, timeout=timeout)
    except TypeError:
        return mcp_lifeos.call(endpoint, body)


def _record_evidence_audit(findings: list[dict[str, Any]], date_str: str) -> None:
    """Persist the audit to LifeOS state and raise a single warning alert (best effort)."""
    if not findings:
        return
    try:
        _call_lifeos_with_timeout(
            "tools/lifeos_state_set",
            {"key": f"roho_evidence_audit_{date_str}", "value": json.dumps({"date": date_str, "findings": findings})},
            timeout=20.0,
        )
    except Exception as exc:
        print(f"WARNING: evidence audit state write failed: {exc}", file=sys.stderr)
    try:
        _call_lifeos_with_timeout(
            "tools/lifeos_alert_raise",
            {
                "alert_id": "lifeos-evidence-age",
                "severity": "warning",
                "message": "Section freshness ok but underlying evidence stale: "
                + ", ".join(
                    f"{f['agent_id']} (age={f['evidence_age_h']}h, max {f['evidence_max_age_h']}h)"
                    if f.get("evidence_age_h") is not None
                    else f"{f['agent_id']} (no producer provenance)"
                    for f in findings
                ),
            },
            timeout=20.0,
        )
    except Exception as exc:
        print(f"NOTE: evidence alert not raised: {exc}", file=sys.stderr)


def _format_finding_note(finding: dict[str, Any]) -> str:
    if finding.get("evidence_age_h") is not None:
        return f"{finding['agent_id']} evidence {finding['evidence_age_h']}h old (max {finding['evidence_max_age_h']}h)"
    return f"{finding['agent_id']} push had no producer provenance"


def _suppress_stale_dated_claims(markdown: str, findings: list[dict[str, Any]]) -> tuple[str, int]:
    """Mark, never delete (REQ-LIFEOS-228). Append a provenance tag to dated
    'pending/scheduled' claims when that agent's evidence is stale or unattributed."""
    stale_agents: dict[str, dict[str, Any]] = {
        str(f["agent_id"]).lower(): f
        for f in findings
        if f.get("evidence_stale") or f.get("missing_producer")
    }
    if not stale_agents:
        return markdown, 0

    out_lines: list[str] = []
    tagged = 0
    audit_note = "; ".join(_format_finding_note(f) for f in findings)
    current_section_owner: str | None = None

    for line in markdown.splitlines():
        h2_m = re.match(r"^##\s+(.+)$", line)
        if h2_m:
            slot_idx = _match_canonical_slot(h2_m.group(1).strip())
            current_section_owner = _SLOT_EXCLUSIVE_OWNER.get(slot_idx)
            out_lines.append(line)
            continue
        if line.startswith(("#", "|", "---", ">")) or len(line) < 20:
            out_lines.append(line)
            continue
        if not _DATED_CLAIM_RE.search(line) or "UNVERIFIED-EVIDENCE" in line:
            out_lines.append(line)
            continue

        named_all = list(
            dict.fromkeys(m.group(1).lower().replace("-", "_") for m in _AGENT_TOKEN_RE.finditer(line))
        )
        if named_all:
            named_stale = [a for a in named_all if a in stale_agents]
            if not named_stale:
                out_lines.append(line)
                continue
            line_note = "; ".join(_format_finding_note(stale_agents[a]) for a in named_stale)
        elif current_section_owner is not None:
            if current_section_owner not in stale_agents:
                out_lines.append(line)
                continue
            line_note = _format_finding_note(stale_agents[current_section_owner])
        else:
            line_note = audit_note

        out_lines.append(f"{line} *(UNVERIFIED-EVIDENCE: {line_note})*")
        tagged += 1
    if tagged:
        print(f"EVIDENCE-RECONCILE: tagged {tagged} dated claim(s) whose evidence is stale or unattributed")
    return "\n".join(out_lines), tagged


def _ensure_evidence_freshness_notice(markdown: str, findings: list[dict[str, Any]]) -> str:
    """Guarantee reader-visible Evidence Freshness Notice naming every stale/unattributed producer (REQ-LIFEOS-231)."""
    flagged = [f for f in findings if f.get("evidence_stale") or f.get("missing_producer")]
    if not flagged or "Evidence Freshness Notice" in (markdown or ""):
        return markdown
    tags = "; ".join(f"*(UNVERIFIED-EVIDENCE: {_format_finding_note(f)})*" for f in flagged)
    notice_line = f"> ⚠️ **Evidence Freshness Notice**: {tags}"
    lines = (markdown or "").splitlines()
    if lines and lines[0].startswith("# "):
        return "\n".join([lines[0], "", notice_line, *lines[1:]])
    return f"{notice_line}\n\n{markdown}"


def _emit_delivery_telemetry(
    html_body: str,
    markdown_brief: str,
    briefing_res_dict: dict[str, Any] | None,
    date_str: str,
) -> dict[str, int]:
    """Emit structured delivery telemetry on stderr and briefing metadata (REQ-LIFEOS-233)."""
    _, sections = normalize_briefing_sections(markdown_brief, briefing_res=briefing_res_dict)
    telemetry = {
        "html_bytes": len((html_body or "").encode("utf-8")),
        "data_image_count": (html_body or "").count("data:image/"),
        "unverified_tag_count": (html_body or "").count("UNVERIFIED-EVIDENCE"),
        "populated_section_count": sum(1 for s in sections if s.get("populated")),
    }
    print(f"DELIVERY-TELEMETRY [{date_str}]: {json.dumps(telemetry)}", file=sys.stderr)
    if isinstance(briefing_res_dict, dict):
        briefing_res_dict["delivery_telemetry"] = telemetry
    return telemetry


def _deliver_email(
    markdown_brief: str,
    briefing_res_dict: dict[str, Any] | None,
    date_str: str,
    target_addr: str | None = None,
    *,
    expected_unverified_tags: int = 0,
) -> tuple[bool, list[str]]:
    """Enforce fallback, substance, and UNVERIFIED-EVIDENCE gates before delivering HTML briefing (REQ-LIFEOS-215, REQ-LIFEOS-216, REQ-LIFEOS-230)."""
    if not target_addr:
        target_addr = "donorazulume@gmail.com"
    meta = (briefing_res_dict.get("meta") if isinstance(briefing_res_dict, dict) and isinstance(briefing_res_dict.get("meta"), dict) else {}) or {}
    compiled_via = (
        (briefing_res_dict.get("compiled_via") if isinstance(briefing_res_dict, dict) else None)
        or meta.get("compiled_via")
        or (
            "deterministic_fallback"
            if (isinstance(briefing_res_dict, dict) and briefing_res_dict.get("fallback") is True) or meta.get("fallback") is True
            else "llm"
        )
    )
    clean_brief = (markdown_brief or "").strip()
    is_local_or_det_fallback = (
        compiled_via == "deterministic_fallback"
        or "# LifeOS Daily Briefing Fallback" in clean_brief
        or "Compiled via deterministic state fallback" in clean_brief
        or "Compiled via local orchestration fallback" in clean_brief
    )
    if is_local_or_det_fallback:
        fb_cause = (
            (briefing_res_dict.get("fallback_error") if isinstance(briefing_res_dict, dict) else None)
            or meta.get("fallback_error")
            or compiled_via
        )
        post_failure_alert(
            f"Briefing fallback executed (compiled_via={compiled_via}, cause={fb_cause}); stub email withheld for {date_str}"
        )
        return False, [f"email: withheld fallback briefing (compiled_via={compiled_via}, cause={fb_cause})"]

    min_chars = int(os.environ.get("LIFEOS_MIN_BRIEFING_CHARS", "40"))
    non_heading_lines = [
        ln.strip()
        for ln in clean_brief.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if len(clean_brief) < min_chars or not non_heading_lines or clean_brief == "Processed 0 sections":
        return False, [
            f"email: withheld low-substance briefing (chars={len(clean_brief)}, body_lines={len(non_heading_lines)})"
        ]

    raw_staleness = meta.get("staleness")
    staleness: dict[str, Any] = raw_staleness if isinstance(raw_staleness, dict) else {}
    stale_satellites = [aid for aid, st in staleness.items() if isinstance(st, dict) and st.get("stale") is True]
    if stale_satellites and "Satellite Data Notice" not in markdown_brief:
        markdown_brief = (
            f"{markdown_brief.rstrip()}\n\n"
            f"> ⚠️ **Satellite Data Notice**: Stale producer section(s) detected ({', '.join(sorted(stale_satellites))}).\n"
        )

    subject_str = f"LifeOS Morning Briefing — {date_str}"
    html_body = render_briefing_email_html(markdown_brief, briefing_res=briefing_res_dict)
    telemetry = _emit_delivery_telemetry(html_body, markdown_brief, briefing_res_dict, date_str)

    # REQ-LIFEOS-230 (#1260): Serving-path governance assertion — fail closed if HTML rendering
    # stripped or lost required UNVERIFIED-EVIDENCE provenance markers.
    if expected_unverified_tags > 0 and telemetry["unverified_tag_count"] < expected_unverified_tags:
        err_detail = (
            f"unverified_tag_strip: rendered HTML contains {telemetry['unverified_tag_count']} "
            f"UNVERIFIED-EVIDENCE tag(s), expected >= {expected_unverified_tags} for {date_str}"
        )
        post_failure_alert(err_detail)
        write_failure_receipt(
            date_str,
            {
                "date": date_str,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "error_class": "unverified_tag_strip",
                "email_ok": False,
                "errors": [f"email: {err_detail}"],
                "delivery_telemetry": telemetry,
            },
        )
        return False, [f"email: withheld ({err_detail})"]

    print(f"Delivering reference email via M365 MCP to {target_addr}...")
    m365_errors: list[str] = []
    try:
        send_res = mcp_m365.call(
            "m365_mail_send",
            {"to": [target_addr], "subject": subject_str, "body_html": html_body},
        )
        if isinstance(send_res, dict) and send_res.get("error"):
            raise RuntimeError(str(send_res["error"]))
        print(f"Email sent successfully to {target_addr} via M365 MCP tool.")
        return True, []
    except Exception as mcp_err:
        m365_errors.append(f"m365_mail_send: {mcp_err}")
        print(f"WARNING: Direct M365 MCP tool call failed: {mcp_err}. Trying M365 proxy endpoint...")

    try:
        mcp_m365.request(  # type: ignore # pyright: ignore[reportAttributeAccessIssue]
            "POST",
            "/me/sendMail",
            payload={
                "message": {
                    "subject": subject_str,
                    "body": {"contentType": "HTML", "content": html_body},
                    "toRecipients": [{"emailAddress": {"address": target_addr}}],
                },
                "saveToSentItems": True,
            },
        )
        print(f"Email sent successfully to {target_addr} via M365 proxy.")
        return True, []
    except Exception as proxy_err:
        m365_errors.append(f"m365_proxy: {proxy_err}")
        print(f"WARNING: M365 proxy send failed ({proxy_err}). Attempting authenticated Gmail failover...")

    try:
        g_res = mcp_google.call(
            "google_mail_send",
            {"to": target_addr, "subject": subject_str, "body_html": html_body},
        )
        if (
            isinstance(g_res, dict)
            and not g_res.get("error")
            and g_res.get("status") == "sent"
            and bool(g_res.get("message_id") or g_res.get("id"))
        ):
            print(f"Email sent successfully to {target_addr} via Gmail MCP failover (id={g_res.get('message_id') or g_res.get('id')}).")
            return True, []
        m365_errors.append(f"google_mail_send_unconfirmed: {g_res}")
    except Exception as g_err:
        m365_errors.append(f"google_mail_send: {g_err}")

    joined_err = "; ".join(m365_errors)
    print(f"ERROR: Failed to send email: {joined_err}")
    return False, [f"email: {joined_err}"]


def _enforce_producer_freshness_gate(
    date_str: str,
    briefing_res_dict: dict[str, Any] | None,
    briefing_file_path: str | pathlib.Path,
    *,
    durable_ok: bool = True,
    email_ok: bool = True,
    dm_ok: bool = True,
    archive_ok: bool = True,
) -> list[str]:
    """Raise high-severity alerts on >=2 consecutive stale days and write stale failure receipts (REQ-LIFEOS-222, REQ-LIFEOS-223)."""
    if not isinstance(briefing_res_dict, dict):
        return []
    meta = briefing_res_dict.get("meta") if isinstance(briefing_res_dict.get("meta"), dict) else {}
    staleness = briefing_res_dict.get("staleness") or (meta.get("staleness") if isinstance(meta, dict) else None) or {}
    if not isinstance(staleness, dict):
        return []

    stale_notes: list[str] = []
    for aid, s_info in sorted(staleness.items()):
        if not isinstance(s_info, dict) or s_info.get("stale") is not True:
            continue
        consec = int(s_info.get("consecutive_stale_days") or 1)
        st_status = str(s_info.get("status") or "stale")
        note = f"stale_producer:{aid} (status={st_status}, consecutive_stale_days={consec})"
        stale_notes.append(note)
        if consec >= 2:
            alert_msg = (
                f"Producer {aid} has published no fresh LifeOS section for {consec} consecutive days "
                f"(status={st_status}) as of {date_str}."
            )
            try:
                mcp_lifeos.call(
                    "tools/lifeos_alert_raise",
                    {
                        "alert_id": f"stale_producer_{aid.lower()}_{date_str}",
                        "severity": "high",
                        "message": alert_msg,
                    },
                )
            except Exception as raise_exc:
                print(f"WARNING: Could not raise LifeOS staleness alert for {aid}: {raise_exc}")
            post_failure_alert(alert_msg)

    if stale_notes and (durable_ok and email_ok and dm_ok):
        write_failure_receipt(
            date_str,
            {
                "date": date_str,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "durable_ok": durable_ok,
                "email_ok": email_ok,
                "dm_ok": dm_ok,
                "archive_ok": archive_ok,
                "stale_producers": True,
                "errors": stale_notes,
                "briefing_file": str(briefing_file_path),
            },
        )
    return stale_notes


def main():
    print("==== Step 1: Compile Today's Briefing ====")
    today_str = datetime.now().strftime("%Y-%m-%d")
    recover_missing_producers(today_str)
    markdown_brief = ""
    briefing_res_dict: dict[str, Any] | None = None
    used_local_fallback = False
    local_fallback_error: str | None = None
    for attempt in range(1, 4):
        try:
            print(f"Requesting compiled briefing from LifeOS MCP (attempt {attempt}/3)...")
            briefing_res = _request_compiled_briefing()
            # FastMCP returns a JSON string, which mcp_lifeos resolves
            if isinstance(briefing_res, str):
                try:
                    briefing_res = json.loads(briefing_res)
                except Exception:
                    pass
            if isinstance(briefing_res, dict):
                briefing_res_dict = briefing_res
                markdown_brief = briefing_res.get("content") or ""
            else:
                markdown_brief = str(briefing_res)
                
            if not markdown_brief or markdown_brief.strip() == "" or "Error" in markdown_brief:
                raise ValueError("Invalid briefing content received from MCP")
            print("Briefing compiled successfully via LifeOS MCP.")
            break
        except Exception as e:
            local_fallback_error = str(e)
            print(f"WARNING: LifeOS MCP briefing compilation attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                import time as _time
                _time.sleep(attempt * 2)
            else:
                print("Falling back to manual compilation...")
    
    if not markdown_brief:
        used_local_fallback = True
        # Fallback 1: Fetch calendar raw (server-side fetch failed)
        now = datetime.now()
        start_of_day = datetime.combine(now.date(), time.min)
        end_of_day = datetime.combine(now.date(), time.max)
        start_iso = start_of_day.astimezone().isoformat()
        end_iso = end_of_day.astimezone().isoformat()
        events_summary = []
        try:
            events_res = mcp_google.call("google_calendar_list_events", {
                "start": start_iso,
                "end": end_iso,
                "calendar_id": "primary"
            })
            events = events_res.get("events", [])
            for ev in events:
                start_val = ev.get("start")
                start_time = start_val.get("dateTime", start_val.get("date")) if isinstance(start_val, dict) else start_val
                end_val = ev.get("end")
                end_time = ev.get("dateTime", ev.get("date")) if isinstance(end_val, dict) else end_val
                events_summary.append({"summary": ev.get("summary"), "start": start_time, "end": end_time})
        except Exception as cal_err:
            print(f"Fallback Calendar fetch failed: {cal_err}")

        # Fallback 2: Read workspace files
        state_text = ""
        for filename in ["USER.md", "MEMORY.md", "SOUL.md", "LESSONS.md"]:  # HEARTBEAT.md retired (OpenClaw 2026.9.6+, #1152)
            path = os.path.join("/home/node/.openclaw/workspace", filename)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        state_text += f"\n--- {filename} ---\n{f.read()}\n"
                except Exception as read_err:
                    print(f"Could not read {filename}: {read_err}")
                    
        # Fallback 3: Query RAG directly across multi-agent domains
        try:
            openbrain_client = resolve_script_path("skills/openbrain-client/client.py")
            cmd = [
                "python3", openbrain_client,
                "--action", "semantic-query",
                "--collection", "open_brain",
                "--query", "active status, portfolio, property, alerts, priorities",
                "--search-mode", "hybrid",
                "--expand-query",
                "--n-results", "10"
            ]
            rag_output = subprocess.check_output(cmd, text=True, timeout=30)
        except Exception as rag_err:
            print(f"Fallback RAG query failed: {rag_err}")
            rag_output = "No RAG context available."

        system_prompt = (
            "You are Roho, the Executive Coordinator for LifeOS. Your task is to compile a comprehensive, "
            "well-formed daily morning briefing summarizing the status of the entire agent ecosystem "
            "(Roho - Executive, Rob - Financial/Markets/Filings, Amara - Property/Operations). "
            "Include key schedule items, financial posture, property highlights, active alerts, and immediate action items."
        )
        user_prompt = (
            f"Please compile today's LifeOS Morning Briefing based on these multi-agent inputs:\n\n"
            f"--- Calendar Events ---\n{json.dumps(events_summary, indent=2)}\n\n"
            f"--- Direct State Files ---\n{state_text}\n\n"
            f"--- Retrieved Multi-Agent Context ---\n{rag_output}\n"
        )
        try:
            local_synthesized = call_deepseek_gateway(system_prompt, user_prompt)
            markdown_brief = (
                f"{local_synthesized}\n\n_ Compiled via local orchestration fallback ({today_str}) _"
            )
        except Exception as llm_err:
            local_fallback_error = f"{local_fallback_error}; local_llm={llm_err}"
            print(f"CRITICAL: Fallback LLM briefing compilation failed: {llm_err}")
            markdown_brief = f"# LifeOS Daily Briefing Fallback\n\nFailed to compile full briefing. Calendar events:\n{json.dumps(events_summary, indent=2)}"
        briefing_res_dict = {
            "compiled_via": "deterministic_fallback",
            "fallback": True,
            "fallback_error": local_fallback_error or "local_orchestration_fallback",
        }

    bible_section = build_bible_section()
    if bible_section and bible_section not in markdown_brief:
        markdown_brief += bible_section

    # Step 1b: evidence-provenance gate (REQ-LIFEOS-228, REQ-LIFEOS-229, REQ-LIFEOS-231).
    # Runs before persist/deliver AND syncs briefing_res_dict["content"] so HTML rendering
    # and durable archives always see identical UNVERIFIED-EVIDENCE markers.
    evidence_findings = _evidence_audit(briefing_res_dict)
    expected_unverified_tags = 0
    if evidence_findings:
        for finding in evidence_findings:
            print(f"EVIDENCE-RECONCILE: {json.dumps(finding)}")
        markdown_brief, _tagged_claims = _suppress_stale_dated_claims(markdown_brief, evidence_findings)
        markdown_brief = _ensure_evidence_freshness_notice(markdown_brief, evidence_findings)
        expected_unverified_tags = max(1, sum(1 for f in evidence_findings if f.get("evidence_stale") or f.get("missing_producer")))
        _record_evidence_audit(evidence_findings, today_str)
    else:
        print("EVIDENCE-RECONCILE: all section evidence within age budget and producer-attributed")

    if isinstance(briefing_res_dict, dict):
        briefing_res_dict["content"] = markdown_brief

    briefing_file_path = resolve_workspace_file("briefing.md")
    with open(briefing_file_path, "w", encoding="utf-8") as f:
        f.write(markdown_brief)

    delivery_errors: list[str] = []
    durable_ok = False
    email_ok = False
    dm_ok = False
    archive_ok = False

    # Persist durable briefing artifact to reports/briefings/ for peer agent visibility (Issue #1165)
    try:
        reports_dir = resolve_workspace_file("reports/briefings")
        os.makedirs(reports_dir, exist_ok=True)
        durable_path = os.path.join(reports_dir, f"briefing_{today_str}.md")
        with open(durable_path, "w", encoding="utf-8") as f:
            f.write(markdown_brief)
        durable_ok = True
    except Exception as persist_err:
        err_msg = f"durable_archive: {persist_err}"
        delivery_errors.append(err_msg)
        print(f"ERROR: Failed to persist durable briefing artifact to reports/briefings/: {persist_err}")

    print("\n==== Step 2: Deliver Reference Email ====")
    email_ok, email_errs = _deliver_email(
        markdown_brief,
        briefing_res_dict,
        today_str,
        expected_unverified_tags=expected_unverified_tags,
    )
    delivery_errors.extend(email_errs)

    dm_file_path = resolve_workspace_file("dm_message.txt")
    print("\n==== Step 3: Deliver Actionable Summary DM ====")
    try:
        print("Requesting executive summary from LifeOS MCP...")
        summary_res = mcp_lifeos.call("tools/lifeos_briefing_get_executive_summary")
        if isinstance(summary_res, str):
            try:
                summary_res = json.loads(summary_res)
            except Exception:
                pass
        if isinstance(summary_res, dict):
            dm_message = summary_res.get("executive_summary")
        else:
            dm_message = str(summary_res)
            
        if not dm_message or dm_message.strip() == "" or "Error" in dm_message:
            raise ValueError("Invalid summary received from LifeOS MCP")
    except Exception as e:
        print(f"WARNING: LifeOS MCP executive summary failed: {e}. Falling back to gateway query...")
        try:
            system_prompt = (
                "You are a precise summarizer. Summarize the daily briefing into a concise, 3-5 bullet actionable summary "
                "and a single explicit question for Don. The summary must be brief, direct, and action-oriented."
            )
            user_prompt = f"Briefing:\n{markdown_brief}"
            dm_message = call_deepseek_gateway(system_prompt, user_prompt)
        except Exception as llm_err:
            print(f"ERROR: Fallback summary generation failed: {llm_err}")
            dm_message = "Actionable summary could not be compiled."

    try:
        with open(dm_file_path, "w", encoding="utf-8") as f:
            f.write(dm_message)

        print("Posting DM to Mattermost...")
        dm_cmd = [
            "python3", resolve_bridge_path(),
            "--action", "dm",
            "--username", "don",
            "--message-file", dm_file_path
        ]
        subprocess.run(dm_cmd, check=True)
        print("DM posted successfully.")
        dm_ok = True
    except Exception as e:
        delivery_errors.append(f"mattermost_dm: {e}")
        print(f"ERROR: Failed to deliver Mattermost DM: {e}")

    print("\n==== Step 4: Deliver Archive Log ====")
    try:
        date_str = today_str
        status_word = "compiled and delivered successfully" if (durable_ok and email_ok and dm_ok) else "compiled with delivery errors"
        archive_msg = f"LifeOS Morning Briefing for {date_str} {status_word}."
        archive_cmd = [
            "python3", resolve_bridge_path(),
            "--action", "post",
            "--channel", "1ch9knt6w3bc3gkw4a7w33k6qh",
            "--message", archive_msg
        ]
        subprocess.run(archive_cmd, check=True)
        print("Archive log posted successfully.")
        archive_ok = True
    except Exception as e:
        delivery_errors.append(f"archive_log: {e}")
        print(f"ERROR: Failed to post archive log: {e}")

    # Cleanup ephemeral DM temp file while preserving briefing.md and reports/briefings/
    if os.path.exists(dm_file_path):
        try:
            os.remove(dm_file_path)
        except Exception as e:
            print(f"WARNING: Failed to remove temporary file {dm_file_path}: {e}")

    stale_gate_notes = _enforce_producer_freshness_gate(
        today_str,
        briefing_res_dict,
        briefing_file_path,
        durable_ok=durable_ok,
        email_ok=email_ok,
        dm_ok=dm_ok,
        archive_ok=archive_ok,
    )

    if not (durable_ok and email_ok and dm_ok):
        all_errors = [*delivery_errors, *stale_gate_notes]
        receipt = {
            "date": today_str,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "durable_ok": durable_ok,
            "email_ok": email_ok,
            "dm_ok": dm_ok,
            "archive_ok": archive_ok,
            "errors": all_errors,
            "briefing_file": briefing_file_path,
        }
        write_failure_receipt(today_str, receipt)
        raise RuntimeError(
            f"Morning briefing delivery incomplete (durable={durable_ok}, email={email_ok}, dm={dm_ok}): "
            + "; ".join(all_errors)
        )

    print("\n==== Step 5: Log Delivery to LifeOS MCP ====")
    try:
        mcp_lifeos.call("tools/lifeos_deliver", {"channel": "comms,smtp", "content": "delivered"})
        print("Delivery logged successfully.")
    except Exception as e:
        print(f"ERROR: Failed to log delivery back to LifeOS MCP: {e}")

    print("\n==== Morning Briefing Pipeline Finished ====")


ALERTS_CHANNEL_ID = os.environ.get("MATTERMOST_ALERTS_CHANNEL_ID", "j7ayoqd3ztf7iexsbi8x7rgiua")


def write_failure_receipt(date_str: str, receipt: dict[str, Any]) -> str | None:
    """Persist structured failure receipt alongside reports/briefings/ (Issue #1165)."""
    candidates = [
        os.path.join(resolve_workspace_file("reports/briefings"), f"briefing_{date_str}.failed.json"),
        resolve_workspace_file(f"briefing_{date_str}.failed.json"),
    ]
    for path in candidates:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(receipt, fh, indent=2)
            return path
        except Exception as exc:
            print(f"WARNING: Could not write failure receipt to {path}: {exc}")
    return None


def post_failure_alert(error_msg: str) -> None:
    """Deliver high-priority alert to Mattermost on fatal briefing pipeline failure (Issue #1120 / #1165)."""
    try:
        bridge_script = resolve_bridge_path()
        alert_text = (
            f"🚨 **[CRITICAL] LifeOS Morning Briefing Pipeline Failed**\n\n"
            f"Error: `{error_msg}`\n"
            f"Timestamp: `{datetime.now(timezone.utc).isoformat()}`\n"
            f"Immediate inspection required."
        )
        msg_file = resolve_workspace_file("briefing_failure_alert.txt")
        with open(msg_file, "w", encoding="utf-8") as f:
            f.write(alert_text)
        subprocess.run([
            "python3", bridge_script,
            "--action", "post",
            "--channel", ALERTS_CHANNEL_ID,
            "--message-file", msg_file
        ], check=False, timeout=15)
        if os.path.exists(msg_file):
            try:
                os.remove(msg_file)
            except Exception:
                pass
    except Exception as exc:
        print(f"Failed to deliver failure alert to Mattermost: {exc}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFATAL: Morning briefing pipeline unhandled exception: {exc}")
        post_failure_alert(str(exc))
        sys.exit(1)
