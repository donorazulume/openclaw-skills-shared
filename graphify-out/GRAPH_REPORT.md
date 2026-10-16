# Graph Report - openclaw-skills-shared  (2026-10-16)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1052 nodes · 2441 edges · 38 communities (31 shown, 7 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 66 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ef49e2b4`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- clickup-manager/manager.py
- os
- json
- clickup_client.py
- triage.py
- ecosystem_review.py
- mcp_m365.py
- google_token_store.py
- email_ops.py
- ModelRouter
- trading_212_client.py
- firefly_extractor.py
- TestGooglePrimaryInboxTriage
- _api
- mattermost_formatter.py
- sanitize_text
- unittest
- resolve_secret
- bridge.py
- test_email_ops.py
- .test_refactor_workspace_creates_everything_fresh
- test_triage.py
- cmd_post
- mcp_comms.py
- mcp_orchestrator.py
- google-manager/test_manager.py
- datetime
- logging
- mcp_bible.py
- mcp_research.py
- TestTriageCLIWiring
- mcp_client_cli.py
- TestExpertJudgment
- resolve_github_pat
- TestClassification
- cmd_react
- cmd_thread

## God Nodes (most connected - your core abstractions)
1. `resolve_secret()` - 31 edges
2. `_run_sync()` - 30 edges
3. `_ensure_async()` - 27 edges
4. `request_json()` - 27 edges
5. `run_ecosystem_review()` - 27 edges
6. `_get()` - 22 edges
7. `main()` - 22 edges
8. `_call()` - 19 edges
9. `ModelRouter` - 18 edges
10. `_api()` - 17 edges

## Surprising Connections (you probably didn't know these)
- `cmd_dm()` --calls--> `format_agent_response()`  [INFERRED]
  mattermost-bridge/bridge.py → lib/mattermost_formatter.py
- `cmd_post()` --calls--> `format_agent_response()`  [INFERRED]
  mattermost-bridge/bridge.py → lib/mattermost_formatter.py
- `cmd_thread()` --calls--> `format_agent_response()`  [INFERRED]
  mattermost-bridge/bridge.py → lib/mattermost_formatter.py
- `finalize()` --calls--> `gmail_send()`  [INFERRED]
  gmail-executive/email_ops.py → google-manager/manager.py
- `send_gated()` --calls--> `gmail_send()`  [INFERRED]
  gmail-executive/email_ops.py → google-manager/manager.py

## Import Cycles
- None detected.

## Communities (38 total, 7 thin omitted)

### Community 0 - "clickup-manager/manager.py"
Cohesion: 0.05
Nodes (48): _api_key(), claim_task(), create_task(), _delete(), delete_task(), _ensure_folder(), _ensure_list(), _ensure_space() (+40 more)

### Community 1 - "os"
Cohesion: 0.06
Nodes (39): check_and_update_heartbeat_state(), delete_job(), get_all_store_keys(), get_effective_jobs_path(), _get_openclaw_home(), _get_sqlite_path(), _get_table_columns(), load_jobs() (+31 more)

### Community 2 - "json"
Cohesion: 0.07
Nodes (49): calendar_add(), calendar_list(), calendar_update(), _call(), drive_download(), drive_find(), drive_init_para(), drive_list_folder() (+41 more)

### Community 3 - "clickup_client.py"
Cohesion: 0.08
Nodes (56): add_comment(), _impl(), _async_request_json_impl(), ClickUpError, comment_text(), create_task(), _impl(), create_webhook() (+48 more)

### Community 4 - "triage.py"
Cohesion: 0.06
Nodes (30): _call(), digest(), dispatch_to_clickup_orchestrator(), download_attachment(), draft_reply(), event_triage(), expert_judgment_from_headers(), _extract_email_address() (+22 more)

### Community 5 - "ecosystem_review.py"
Cohesion: 0.08
Nodes (47): _append_redetect(), _atomic_write(), _brain_upsert_finding(), _build_tree_summary(), _walk(), _cache_root(), _chat(), call_with_retries() (+39 more)

### Community 6 - "mcp_m365.py"
Cohesion: 0.06
Nodes (28): admin(), _base_url(), _bearer(), call(), _decode_response(), _extract_tool_result(), GoogleMCPError, _headers() (+20 more)

### Community 7 - "google_token_store.py"
Cohesion: 0.08
Nodes (30): atomic_write_token_file(), _check_secure_token_file(), _client_config_from_env(), _credential_fresh_enough(), _credentials_valid_enough(), cron_refresh_and_sync_doppler(), _dir_writable(), _ExclusiveRefreshLock (+22 more)

### Community 8 - "email_ops.py"
Cohesion: 0.08
Nodes (23): check_loop(), classify_recipients(), create_transaction(), finalize(), _HTMLTextExtractor, ingest_emails(), is_whitelisted(), _load_transactions() (+15 more)

### Community 9 - "ModelRouter"
Cohesion: 0.07
Nodes (5): CircuitBreaker, CompletionTruncatedError, DispatchDecision, ModelRouter, _do_gateway()

### Community 10 - "trading_212_client.py"
Cohesion: 0.10
Nodes (16): _base_url(), _bearer(), call(), _headers(), Trade212MCPError, get_account_summary(), get_funding_transactions(), get_live_portfolio_snapshot() (+8 more)

### Community 11 - "firefly_extractor.py"
Cohesion: 0.11
Nodes (12): build_entity_yaml(), build_trial_balance_csv(), extract_firefly_data(), generate_from_firefly(), is_non_pnl_transaction(), map_category_to_frs105(), _safe_decimal(), _base_url() (+4 more)

### Community 12 - "TestGooglePrimaryInboxTriage"
Cohesion: 0.11
Nodes (3): TestAttachmentAndThreadTracking, _side_effect(), TestGooglePrimaryInboxTriage

### Community 13 - "_api"
Cohesion: 0.14
Nodes (13): _api(), cmd_channels(), cmd_dispatch(), cmd_edit(), cmd_health(), cmd_join(), cmd_read(), cmd_resolve_user() (+5 more)

### Community 14 - "mattermost_formatter.py"
Cohesion: 0.12
Nodes (10): AgentResponsePayload, _chunk_text_by_paragraphs(), ensure_code_language_tags(), format_agent_response(), _handle_raw_json_or_suppression(), inject_a2a_mention(), MattermostPostPayload, normalize_headers() (+2 more)

### Community 15 - "sanitize_text"
Cohesion: 0.16
Nodes (4): sanitize_text(), strip_invisible_chars(), wrap_content(), TestPromptInjection

### Community 16 - "unittest"
Cohesion: 0.12
Nodes (4): TestExpertJudgment, TestExpertJudgment, TestDriveOrganizeForwardsTarget, TestSourceHasNoOAuthImports

### Community 17 - "resolve_secret"
Cohesion: 0.16
Nodes (5): TestResolveSecret, _candidate_secret_files(), _dedupe_paths(), _normalize_secret(), resolve_secret()

### Community 18 - "bridge.py"
Cohesion: 0.14
Nodes (6): cmd_join_all(), _is_raw_cli_json_dump(), _join_token_to_channel(), _upload_file(), _upload_headers(), _validate_file_path()

### Community 19 - "test_email_ops.py"
Cohesion: 0.14
Nodes (6): _fake_mg_call(), TestApprovalToken, TestIngestRoutesViaMCP, TestPreprocess, TestSendGatedRoutesViaMCP, TestTransactionStateMachine

### Community 20 - ".test_refactor_workspace_creates_everything_fresh"
Cohesion: 0.16
Nodes (3): TestAssigneeAndGetTask, TestRefactorWorkspace, side_effect_get()

### Community 21 - "test_triage.py"
Cohesion: 0.13
Nodes (4): TestEmailValidator, TestForcedCC, TestRuleTarget, TestSendEmailValidation

### Community 22 - "cmd_post"
Cohesion: 0.25
Nodes (7): cmd_dm(), cmd_post(), _handle_file_uploads(), _is_http_error(), _join_self_to_channel(), _save_reaction(), _upload_files()

### Community 23 - "mcp_comms.py"
Cohesion: 0.24
Nodes (6): _base_url(), _bearer(), call(), CommsMCPError, _headers(), request()

### Community 24 - "mcp_orchestrator.py"
Cohesion: 0.23
Nodes (6): _base_url(), _bearer(), call(), _headers(), orch_peer_status(), OrchestratorMCPError

### Community 25 - "google-manager/test_manager.py"
Cohesion: 0.23
Nodes (5): _fake_call(), TestDriveWiring, cap(), TestGmailWiring, TestNoOAuthImports

### Community 26 - "datetime"
Cohesion: 0.24
Nodes (3): construct_research_block(), ResearchGateError, validate_research_gate()

### Community 27 - "logging"
Cohesion: 0.22
Nodes (3): call_mcp_tool(), is_langchain_adapter_available(), resolve_service_config()

### Community 28 - "mcp_bible.py"
Cohesion: 0.25
Nodes (5): _base_url(), _bearer(), BibleMCPError, call(), _headers()

### Community 29 - "mcp_research.py"
Cohesion: 0.27
Nodes (5): _base_url(), _bearer(), call(), _headers(), ResearchMCPError

### Community 30 - "TestTriageCLIWiring"
Cohesion: 0.31
Nodes (4): _fake_call(), TestTriageCLIWiring, custom_fake_call(), custom_fake_call()

## Knowledge Gaps
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `resolve_secret()` connect `resolve_secret` to `resolve_github_pat`, `os`, `mcp_m365.py`, `ModelRouter`, `trading_212_client.py`, `firefly_extractor.py`, `mcp_comms.py`, `mcp_orchestrator.py`, `logging`, `mcp_bible.py`, `mcp_research.py`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Should `clickup-manager/manager.py` be split into smaller, more focused modules?**
  _Cohesion score 0.05499316005471956 - nodes in this community are weakly interconnected._
- **Why does `TestGooglePrimaryInboxTriage` connect `TestGooglePrimaryInboxTriage` to `test_triage.py`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **Should `os` be split into smaller, more focused modules?**
  _Cohesion score 0.06070175438596491 - nodes in this community are weakly interconnected._
- **Why does `sanitize_text()` connect `sanitize_text` to `triage.py`?**
  _High betweenness centrality (0.029) - this node is a cross-community bridge._
- **Should `json` be split into smaller, more focused modules?**
  _Cohesion score 0.07071455016660497 - nodes in this community are weakly interconnected._
- **Should `clickup_client.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08105022831050228 - nodes in this community are weakly interconnected._