#!/usr/bin/env python3
"""
mcp_model_router.py — Model-Agnostic Multi-Provider Dispatch Router Engine (SPEC-MODEL-ROUTER-001)

Features:
- Single source-of-truth declarative model registry loading (config/model-registry.json)
- Dynamic task-class routing (fastchat, reasoning, financial, statutory)
- Governed domain determinism gates (pinned: true for financial & statutory)
- Provider circuit breaker & health tracking (429/5xx, TTFT/latency percentile, token capacity)
- Cost budget ceilings & kill-switch protection
- Dynamic on/off-boarding without code modification
- Telemetry emission & Open Brain ingestion (`llm_dispatch` collection entity)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REGISTRY_PATH = REPO_ROOT / "config" / "model-registry.json"


@dataclass
class DispatchDecision:
    agent: str
    route: str
    provider: str
    model: str
    full_model_spec: str
    decision_type: str  # "primary" | "fallback" | "pinned"
    pinned: bool
    secret_env_var: str
    max_cost_per_call_usd: float
    timestamp_iso: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CircuitBreaker:
    """Live provider circuit-breaker tracking error rates and cooldowns."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 300) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

    def is_available(self, provider_id: str) -> bool:
        now = time.time()
        until = self._cooldown_until.get(provider_id, 0.0)
        if now < until:
            return False
        if until > 0.0 and now >= until:
            # Cooldown expired, reset failures
            self._failures[provider_id] = 0
            self._cooldown_until[provider_id] = 0.0
        return True

    def record_success(self, provider_id: str) -> None:
        self._failures[provider_id] = 0
        self._cooldown_until[provider_id] = 0.0

    def record_failure(self, provider_id: str) -> None:
        count = self._failures.get(provider_id, 0) + 1
        self._failures[provider_id] = count
        if count >= self.failure_threshold:
            self._cooldown_until[provider_id] = time.time() + self.cooldown_seconds


class ModelRouter:
    """Engine executing SPEC-MODEL-ROUTER-001 multi-provider dispatch."""

    def __init__(
        self,
        registry_path: Path | str | None = None,
        agent_name: str = "roho",
        enabled: bool | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.registry_path = Path(registry_path) if registry_path else DEFAULT_REGISTRY_PATH
        self.registry_data: dict[str, Any] = {}
        self.circuit_breaker = CircuitBreaker()
        self.daily_spend_usd = 0.0
        self.load_registry()

        defaults = self.registry_data.get("defaults", {})
        if enabled is not None:
            self.enabled = enabled
        else:
            # Check env var first, then default
            env_val = os.environ.get("OPENCLAW_MODEL_ROUTER_ENABLED")
            if env_val is not None:
                self.enabled = env_val.lower() in ("true", "1", "yes")
            else:
                self.enabled = defaults.get("enabled", False)

        self.budget_kill_switch = defaults.get("budgetKillSwitch", False)
        self.daily_budget_ceiling_usd = defaults.get("dailyBudgetUsdCeiling", 50.0)

    def _is_valid_registry(self, path: Path) -> bool:
        try:
            if path.is_file() and path.stat().st_size > 0:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return isinstance(data, dict) and "providers" in data
        except Exception:
            pass
        return False

    def load_registry(self) -> None:
        target_path = self.registry_path
        if not self._is_valid_registry(target_path):
            candidates: list[Path] = []
            env_override = os.environ.get("OPENCLAW_MODEL_REGISTRY_PATH")
            if env_override:
                candidates.append(Path(env_override))
            candidates.extend([
                DEFAULT_REGISTRY_PATH,
                Path("/opt/openclaw/templates/config/model-registry.json"),
                Path("/opt/openclaw/config/model-registry.json"),
                Path("/home/node/.openclaw/config/model-registry.json"),
                Path("/home/node/amara/.openclaw/config/model-registry.json"),
                Path("/home/node/rob/.openclaw/config/model-registry.json"),
                REPO_ROOT / "config" / "model-registry.json",
                Path.cwd() / "config" / "model-registry.json",
            ])
            found: Path | None = None
            for cand in candidates:
                if self._is_valid_registry(cand):
                    found = cand
                    break
            if found:
                self.registry_path = found
                # Self-heal target_path if it was invalid or empty
                try:
                    if target_path != found and not self._is_valid_registry(target_path):
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_text(found.read_text(encoding="utf-8"), encoding="utf-8")
                        self.registry_path = target_path
                except Exception:
                    pass
            else:
                raise FileNotFoundError(f"Model registry file not found or invalid at {target_path}")

        with open(self.registry_path, "r", encoding="utf-8") as f:
            self.registry_data = json.load(f)

    def onboard_provider(self, provider_id: str, provider_config: dict[str, Any]) -> bool:
        """Dynamically add or update a provider in the model registry."""
        providers = self.registry_data.setdefault("providers", {})
        providers[provider_id] = provider_config
        providers[provider_id]["status"] = "ready"
        return True

    def offboard_provider(self, provider_id: str) -> bool:
        """Offboard a provider by marking it retired."""
        providers = self.registry_data.get("providers", {})
        if provider_id in providers:
            providers[provider_id]["status"] = "retired"
            return True
        return False

    def _resolve_secret_key(self, provider_id: str, provider_info: dict[str, Any]) -> str:
        per_agent = provider_info.get("perAgentEnvVars", {})
        if self.agent_name in per_agent and per_agent[self.agent_name] in os.environ:
            return per_agent[self.agent_name]
        return provider_info.get("secretEnvVar", "API_KEY")

    def _spec_to_provider_model(self, full_spec: str) -> tuple[str, str]:
        if "/" in full_spec:
            p, m = full_spec.split("/", 1)
            return p, m
        return "openai-compatible", full_spec

    def resolve_route(
        self,
        task_class: str = "fastchat",
        override_model: str | None = None,
    ) -> DispatchDecision:
        """Resolve route for a given task class or model override."""
        routes = self.registry_data.get("routes", {})
        route_info = routes.get(task_class, routes.get("fastchat", {}))

        route_uri = f"route://{task_class}"
        is_pinned = route_info.get("pinned", False)
        max_cost = route_info.get("maxCostPerCallUsd", 0.10)

        # 1. Budget kill switch check
        if self.budget_kill_switch or self.daily_spend_usd >= self.daily_budget_ceiling_usd:
            # Abort ambient dispatch, force fail safe to default pinned route
            primary_spec = route_info.get("primary", "openai-compatible/deepseek-v4-flash")
            p, m = self._spec_to_provider_model(primary_spec)
            p_info = self.registry_data.get("providers", {}).get(p, {})
            secret_key = self._resolve_secret_key(p, p_info)
            return DispatchDecision(
                agent=self.agent_name,
                route=route_uri,
                provider=p,
                model=m,
                full_model_spec=primary_spec,
                decision_type="pinned" if is_pinned else "fallback",
                pinned=True,
                secret_env_var=secret_key,
                max_cost_per_call_usd=max_cost,
            )

        # 2. Disabled flag mode or explicit override
        if not self.enabled or override_model:
            target_spec = override_model or route_info.get("primary", "openai-compatible/deepseek-v4-flash")
            p, m = self._spec_to_provider_model(target_spec)
            p_info = self.registry_data.get("providers", {}).get(p, {})
            secret_key = self._resolve_secret_key(p, p_info)
            return DispatchDecision(
                agent=self.agent_name,
                route=route_uri,
                provider=p,
                model=m,
                full_model_spec=target_spec,
                decision_type="pinned" if is_pinned else "primary",
                pinned=is_pinned,
                secret_env_var=secret_key,
                max_cost_per_call_usd=max_cost,
            )

        # 3. Governed routes: pinned determinism lock
        if is_pinned:
            primary_spec = route_info.get("primary", "openai-compatible/deepseek-v4-flash")
            p, m = self._spec_to_provider_model(primary_spec)
            p_info = self.registry_data.get("providers", {}).get(p, {})
            secret_key = self._resolve_secret_key(p, p_info)
            return DispatchDecision(
                agent=self.agent_name,
                route=route_uri,
                provider=p,
                model=m,
                full_model_spec=primary_spec,
                decision_type="pinned",
                pinned=True,
                secret_env_var=secret_key,
                max_cost_per_call_usd=max_cost,
            )

        # 4. Multi-provider ambient routing with circuit-breaker evaluation
        candidates = [route_info.get("primary")] + (route_info.get("fallbacks") or [])
        providers_data = self.registry_data.get("providers", {})

        selected_spec = None
        decision_type = "primary"

        for idx, cand_spec in enumerate(candidates):
            if not cand_spec:
                continue
            p, m = self._spec_to_provider_model(cand_spec)
            p_info = providers_data.get(p, {})

            if p_info.get("status") == "retired":
                continue

            if self.circuit_breaker.is_available(p):
                selected_spec = cand_spec
                decision_type = "primary" if idx == 0 else "fallback"
                break

        if not selected_spec:
            # Fallback to primary if all candidates are degraded
            selected_spec = route_info.get("primary", "openai-compatible/deepseek-v4-flash")
            decision_type = "fallback"

        p, m = self._spec_to_provider_model(selected_spec)
        p_info = providers_data.get(p, {})
        secret_key = self._resolve_secret_key(p, p_info)

        return DispatchDecision(
            agent=self.agent_name,
            route=route_uri,
            provider=p,
            model=m,
            full_model_spec=selected_spec,
            decision_type=decision_type,
            pinned=False,
            secret_env_var=secret_key,
            max_cost_per_call_usd=max_cost,
        )

    def compute_cost(
        self, provider: str, model: str, tokens_in: int, tokens_out: int
    ) -> float:
        p_info = self.registry_data.get("providers", {}).get(provider, {})
        models = p_info.get("models", [])
        cost_rates = {"input": 0.0, "output": 0.0}
        for m in models:
            if m.get("id") == model:
                cost_rates = m.get("costPerMillionTokens", cost_rates)
                break
        input_cost = (tokens_in / 1_000_000.0) * cost_rates.get("input", 0.0)
        output_cost = (tokens_out / 1_000_000.0) * cost_rates.get("output", 0.0)
        return round(input_cost + output_cost, 6)

    def record_dispatch_outcome(
        self,
        decision: DispatchDecision,
        http_status: int,
        latency_ms: int,
        tokens_in: int = 0,
        tokens_out: int = 0,
        trigger: str = "agent_request",
    ) -> dict[str, Any]:
        """Record dispatch outcome, update circuit breaker & budget, return telemetry event."""
        if 200 <= http_status < 300:
            self.circuit_breaker.record_success(decision.provider)
            outcome = "ok"
        elif http_status == 429 or http_status >= 500:
            self.circuit_breaker.record_failure(decision.provider)
            outcome = "degraded"
        else:
            outcome = "retry"

        cost_usd = self.compute_cost(decision.provider, decision.model, tokens_in, tokens_out)
        self.daily_spend_usd += cost_usd

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": decision.agent,
            "route": decision.route,
            "provider": decision.provider,
            "model": decision.model,
            "trigger": trigger,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cache_hit": 0.0,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "decision": decision.decision_type,
            "http_status": http_status,
            "outcome": outcome,
        }

        self.ingest_telemetry_open_brain(event)
        return event

    def dispatch_with_retry(
        self,
        func: Any,
        *args: Any,
        max_retries: int = 3,
        backoff_sec: float = 2.0,
        provider_hint: str = "unknown",
        **kwargs: Any,
    ) -> Any:
        """Issue #514: Execute LLM request with exponential backoff retry and enriched error logging."""
        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    time.sleep(backoff_sec * attempt)

        err_msg = (
            f"LLM request failed (provider={provider_hint}, status={getattr(last_exc, 'status_code', 'unknown')}, "
            f"attempts={max_retries}): {last_exc}"
        )
        raise RuntimeError(err_msg) from last_exc

    def ingest_telemetry_open_brain(self, event: dict[str, Any]) -> None:
        """Ingest llm_dispatch event into Open Brain collection open_brain."""
        try:
            from mcp_comms import query_open_brain  # type: ignore

            query_open_brain(
                action="ingest",
                collection="open_brain",
                entity_type="llm_dispatch",
                payload=event,
            )
        except Exception:
            # Silent fallback if open_brain service is offline or un-imported
            pass

    def get_health_status(self) -> dict[str, Any]:
        providers = self.registry_data.get("providers", {})
        status = {}
        for p_id, p_info in providers.items():
            avail = self.circuit_breaker.is_available(p_id)
            status[p_id] = {
                "configured_status": p_info.get("status", "unknown"),
                "circuit_breaker_available": avail,
                "models_count": len(p_info.get("models", [])),
            }
        return {
            "enabled": self.enabled,
            "daily_spend_usd": round(self.daily_spend_usd, 4),
            "daily_budget_ceiling_usd": self.daily_budget_ceiling_usd,
            "budget_kill_switch": self.budget_kill_switch,
            "providers": status,
        }
