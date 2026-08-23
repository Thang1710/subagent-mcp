# Subagent MCP 1.0.4 Local Activity Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Subagent MCP 1.0.4 with a truthful, secure, automatically refreshed per-execution detail panel at `127.0.0.1:8765`.

**Architecture:** Extend the existing persisted `AgentDescriptor` and execution request metadata rather than adding a parallel activity store. Add one strictly whitelisted, session-authenticated read endpoint over SQLite, then render it as a responsive list/detail split view using the existing dependency-free HTML/CSS/JavaScript. Provider refresh, native-harness execution, and Codex private state remain outside the UI read path.

**Tech Stack:** Python 3.10+, stdlib SQLite/HTTP server, existing `mcp` SDK, plain HTML/CSS/JavaScript, pytest, uv, GitHub Actions.

---

## Authority and execution rules

- Read `AGENTS.md`, then `docs/superpowers/specs/2026-08-17-subagent-mcp-design.md`, then `docs/superpowers/specs/2026-08-23-subagent-mcp-local-activity-detail-design.md`, then this plan.
- Release from public `v1.0.3`; do not rewrite or republish that tag/version.
- Keep `native_host_panel=unsupported`; do not call `thread/inject_items` or mutate Codex app-server/session/UI state.
- Use one implementation writer at a time. Claude and OX Alpha may review in parallel only after the writer's diff is stable.
- Never enable Claude usage credits/overage.
- Stop repeated review/fix loops. Fix only verified Critical findings or repeatedly reproduced Major findings; defer minor polish.
- Every commit uses `Thang1710 <50268205+Thang1710@users.noreply.github.com>` through per-command Git options, never global config.

## File map

- `src/subagent_harness_mcp/contracts.py`: deterministic icon monogram/tone in the normalized descriptor.
- `src/subagent_harness_mcp/service.py`: persist one redacted, bounded `task_title` presentation field.
- `src/subagent_harness_mcp/ui.py`: activity summary/detail projections, authenticated detail route, and no-provider read boundary.
- `src/subagent_harness_mcp/static/index.html`: activity list/detail semantic structure.
- `src/subagent_harness_mcp/static/app.js`: safe DOM rendering, selection, local polling, and stale state.
- `src/subagent_harness_mcp/static/app.css`: responsive split-view styling.
- `tests/unit/test_contracts.py`: descriptor icon contract.
- `tests/unit/test_service.py`: bounded title persistence and prompt non-persistence.
- `tests/integration/test_ui_service.py`: real-store summary/detail projection and no-provider behavior.
- `tests/unit/test_ui_security.py`: route authentication, traversal/unknown-id behavior, field non-leakage, and static frontend contract.
- `tests/unit/test_package_contract.py`: version/readme/workflow release contract.
- `tests/integration/test_wheel_e2e.py`: installed artifact version.
- `README.md`: concise localhost detail usage and exact 1.0.4 install commands.
- `pyproject.toml`, `src/subagent_harness_mcp/__init__.py`, `src/subagent_harness_mcp/adapters/deepseek_harness.py`, `server.json`: synchronized 1.0.4 identity.
- `.github/workflows/release.yml`: explicitly dispatch registry publishing after a token-created GitHub release.

### Task 1: Persist presentation identity without prompts

**Files:**
- Modify: `src/subagent_harness_mcp/contracts.py`
- Modify: `src/subagent_harness_mcp/service.py`
- Test: `tests/unit/test_contracts.py`
- Test: `tests/unit/test_service.py`

- [ ] **Step 1: Write failing descriptor and persistence tests**

Add a descriptor test that asserts the icon is a validated two-field presentation object and is deterministic:

```python
def test_descriptor_icon_has_deterministic_monogram_and_tone() -> None:
    first = AgentDescriptor.from_manifest(
        _manifest(), model="vendor/model", transport="managed-sdk"
    ).to_dict()["icon"]
    second = AgentDescriptor.from_manifest(
        _manifest(), model="vendor/model", transport="managed-sdk"
    ).to_dict()["icon"]

    assert first == second
    assert first["kind"] == "monogram"
    assert first["text"] == "F"
    assert first["tone"] in {"blue", "green", "purple", "teal"}
```

Add a service test that spawns a fake execution whose title contains an email/token sentinel, then reads `requested_json`:

```python
def test_task_title_is_redacted_bounded_and_prompt_is_not_persisted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    harness = FakeHarness()
    harness.enqueue("done", result="finished")
    service, store = _service(tmp_path, harness)
    request = _spawn_request(workspace, prompt="prompt-secret-marker")
    request = replace(
        request,
        task=replace(
            request.task,
            title="Review user@example.com Bearer abcdefghijklmnopqrstuvwxyz " + "x" * 400,
        ),
    )

    asyncio.run(service.agent_spawn(request))

    with store.transaction() as database:
        requested = json.loads(database.execute("SELECT requested_json FROM executions").fetchone()[0])
    assert requested["task_title"].startswith("Review [REDACTED_EMAIL] Bearer [REDACTED]")
    assert len(requested["task_title"]) <= 240
    assert "prompt-secret-marker" not in json.dumps(requested)
```

- [ ] **Step 2: Run the two tests and verify RED**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/unit/test_contracts.py::test_descriptor_icon_has_deterministic_monogram_and_tone `
  tests/unit/test_service.py::test_task_title_is_redacted_bounded_and_prompt_is_not_persisted
```

Expected: both fail because `tone` and `task_title` do not exist.

- [ ] **Step 3: Implement deterministic icon tone**

In `contracts.py`, keep the current derived monogram and add a fixed palette selected from the stable SHA-256 of `runtime_id`:

```python
_ICON_TONES = ("blue", "green", "purple", "teal")


def _descriptor_icon(runtime_id: str, display_name: str) -> dict[str, str]:
    monogram = next(
        (character.upper() for character in display_name if character.isalnum()),
        "S",
    )
    tone_index = hashlib.sha256(runtime_id.encode("utf-8")).digest()[0] % len(_ICON_TONES)
    return {
        "kind": "monogram",
        "text": monogram,
        "tone": _ICON_TONES[tone_index],
    }
```

Use `_descriptor_icon(manifest.runtime_id, manifest.display_name)` in `AgentDescriptor.from_manifest`. Do not add URLs, paths, markup, or provider trademarks.

- [ ] **Step 4: Persist only the bounded redacted task title**

In `_requested_metadata(...)`, add exactly one task presentation field:

```python
"task_title": _redact_text(request.task.title)[:240],
```

Do not add `prompt`, `acceptance_criteria`, `role`, or authority text.

- [ ] **Step 5: Run focused tests and existing privacy regression**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/unit/test_contracts.py `
  tests/unit/test_service.py::test_task_title_is_redacted_bounded_and_prompt_is_not_persisted `
  tests/unit/test_service.py::test_prompt_credentials_and_pii_are_not_persisted
```

Expected: all selected tests pass; the prompt marker is absent from durable requested metadata.

- [ ] **Step 6: Commit Task 1**

```powershell
git add src/subagent_harness_mcp/contracts.py src/subagent_harness_mcp/service.py tests/unit/test_contracts.py tests/unit/test_service.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "feat: persist safe activity identity"
```

### Task 2: Add the whitelisted local activity-detail API

**Files:**
- Modify: `src/subagent_harness_mcp/ui.py`
- Test: `tests/integration/test_ui_service.py`
- Test: `tests/unit/test_ui_security.py`

- [ ] **Step 1: Write a real-store projection test**

Import `asyncio`, `FakeHarness`, `SpawnRequest`, and `TaskPacket`, then add a complete local helper which uses only the deterministic fake adapter:

```python
def _activity_backend(home: Path):
    paths = resolve_paths(
        {"SUBAGENT_MCP_HOME": str(home.resolve())},
        os_name="nt",
    )
    config = ConfigStore(paths)
    config.save(
        {
            "schema_version": 1,
            "revision": 0,
            "runtimes": {
                "fake": {
                    "enabled": True,
                    "selection_mode": "fixed",
                    "fallback": False,
                    "variants": [
                        {
                            "id": "configured",
                            "model": "future/model-v9",
                            "reasoning": {"mode": "provider-native"},
                        }
                    ],
                }
            },
        },
        expected_revision=0,
    )
    harness = FakeHarness()
    harness.enqueue("done", result="safe terminal result")
    store = StateStore.open(paths)
    service = SubagentMcpService(
        config=config,
        store=store,
        registry=AdapterRegistry(
            builtin_factories=(lambda: FakeAdapter(harness),)
        ),
    )
    backend = LocalUiBackend(config=config, service=service, store=store)
    return backend, service


def _activity_spawn(workspace: Path) -> SpawnRequest:
    return SpawnRequest(
        request_id="activity-spawn-1",
        runtime_id="fake",
        variant_id="configured",
        task=TaskPacket(
            title="Bounded activity task",
            prompt="Return one deterministic result.",
            acceptance_criteria=("Return normalized status.",),
            role="sub-agent",
        ),
        cwd=str(workspace.resolve()),
        mode="review",
        transport="managed-sdk",
        permissions=("repo_read",),
    )
```

Create one fake execution through `SubagentMcpService`, then assert `LocalUiBackend.snapshot()` and `activity_detail(...)` expose the approved fields:

```python
def test_ui_activity_detail_projects_persisted_execution_without_raw_events(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    backend, service = _activity_backend(tmp_path / "home")
    status = asyncio.run(service.agent_spawn(_activity_spawn(workspace)))

    snapshot = backend.snapshot()
    detail = backend.activity_detail(status.execution_id)

    assert snapshot["activity"][0]["id"] == status.execution_id
    assert snapshot["activity"][0]["title"] == "Bounded activity task"
    assert snapshot["activity"][0]["icon"]["kind"] == "monogram"
    assert detail is not None
    assert detail["conversationId"] == status.conversation_id
    assert detail["workspace"] == workspace.name
    assert detail["writeSet"] == []
    assert detail["steps"] == [
        {"cursor": 1, "kind": "started", "at": detail["steps"][0]["at"]},
        {"cursor": 2, "kind": "completed", "at": detail["steps"][1]["at"]},
    ]
    assert detail["result"]["text"] == "safe terminal result"
    assert "payload" not in json.dumps(detail)
    assert "external_session" not in json.dumps(detail)
```

The helper must use the existing fake adapter/service/store fixtures and must not call a live provider.

- [ ] **Step 2: Write route authentication and leakage tests**

Extend the unit `_server(...)` helper with an optional detail callback. Add tests for:

```python
def test_activity_detail_requires_session_and_returns_normalized_404() -> None:
    server = _server(activity_detail=lambda execution_id: None)
    server.start()
    try:
        anonymous, _, _ = _request(server, "GET", "/api/v1/activity/execution-1")
        cookie, _ = _open_session(server)
        missing, _, body = _request(
            server,
            "GET",
            "/api/v1/activity/execution-does-not-exist",
            headers={"Cookie": cookie},
        )
    finally:
        server.close()

    assert anonymous == 401
    assert missing == 404
    assert json.loads(body)["error"] == "NOT_FOUND"
```

Add a detail callback containing safe `result.text` plus prompt/raw-event/hidden-thinking sentinels. Assert the safe redacted result is present but the private sentinels and event payload are absent from the response body.

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/integration/test_ui_service.py -k activity_detail `
  tests/unit/test_ui_security.py -k activity_detail
```

Expected: fail because `activity_detail` and the route do not exist.

- [ ] **Step 4: Add the backend/provider boundary**

Add:

```python
ActivityDetailProvider = Callable[[str], Mapping[str, Any] | None]
```

`LocalUiBackend.activity_detail(execution_id)` delegates to a private read helper. Thread this optional callback through `_UiState` and `LoopbackUiServer`, and pass `backend.activity_detail` from `run_ui`.

Keep existing constructors source-compatible by making it keyword-only and optional:

```python
def __init__(
    self,
    snapshot_provider: SnapshotProvider,
    config_patcher: ConfigPatcher,
    *,
    activity_detail_provider: ActivityDetailProvider | None = None,
    provider_refresher: ProviderRefresher | None = None,
    ...,
) -> None:
```

- [ ] **Step 5: Extend the activity summary projection**

Update `_read_ui_state` to select `conversation_id`, `descriptor_json`, `requested_json`, and `updated_at_utc` in addition to existing fields. Parse JSON only through small defensive helpers that return `{}` for malformed/non-object values.

Each summary row must be built from a fixed literal dict. Never pass whole decoded JSON objects through:

```python
{
    "id": execution_id,
    "conversationId": conversation_id,
    "title": _activity_title(requested, descriptor, runtime_id),
    "runtime": runtime_id,
    "displayName": _descriptor_text(descriptor, "display_name", runtime_id),
    "modelDisplayName": _descriptor_text(descriptor, "model_display_name", ""),
    "transport": _descriptor_text(descriptor, "transport", ""),
    "icon": _public_icon(descriptor.get("icon"), runtime_id, display_name),
    "state": execution_state,
    "startedAt": created_at,
    "updatedAt": updated_at,
    "finishedAt": terminal_at,
    "durationMs": _activity_duration_ms(created_at, terminal_at),
}
```

Add the new names to `_ACTIVITY_FIELDS`; keep all prompt/event/raw/transcript keys excluded.

- [ ] **Step 6: Implement the detail projection**

Implement `_read_activity_detail(store, execution_id) -> dict[str, Any] | None` with two bounded SQL reads in one read transaction:

```sql
SELECT e.execution_id, e.conversation_id, c.runtime_id,
       c.state, c.state_revision, e.state, e.state_revision,
       c.descriptor_json, e.requested_json, e.observed_json, e.result_json,
       e.created_at_utc, e.updated_at_utc, e.terminal_at_utc
FROM executions AS e
JOIN conversations AS c ON c.conversation_id = e.conversation_id
WHERE e.execution_id = ?
```

```sql
SELECT cursor, kind, created_at_utc
FROM events
WHERE execution_id = ?
ORDER BY cursor
LIMIT 128
```

Validate `execution_id` as an ASCII product identifier before SQL. Build the response from a fixed allowlist. Event `payload_json` must not be selected. Use `result_artifact_metadata` for terminal result metadata and expose `result["text"]` only when it is a string already stored by the service.

Import `datetime` and `timezone` and compute `durationMs` from ISO timestamps with a fail-closed helper. A terminal execution ends at `terminal_at_utc`; an active execution ends at the current UTC time. Invalid/missing timestamps return `None`, and negative durations clamp to zero:

```python
def _activity_duration_ms(started_at: object, finished_at: object) -> int | None:
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        finished = (
            datetime.now(timezone.utc)
            if finished_at is None
            else datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
        )
        elapsed = finished - started
    except (TypeError, ValueError):
        return None
    return max(0, int(elapsed.total_seconds() * 1000))
```

Map state to `currentStage` with a fixed dict:

```python
_CURRENT_STAGE = {
    "queued": "queued",
    "starting": "starting_native_harness",
    "running": "external_harness_working",
    "needs_input": "waiting_for_input",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "interrupted": "interrupted",
}
```

Override `currentStage` to `recovery_required` only when the persisted public result error code is exactly `RECOVERY_REQUIRED`.

- [ ] **Step 7: Add the authenticated GET route**

In `do_GET`, after snapshot handling and before static assets:

```python
prefix = "/api/v1/activity/"
if path.startswith(prefix):
    if self._session(require_csrf=False) is None:
        return
    execution_id = path.removeprefix(prefix)
    if not execution_id or "/" in execution_id or state.activity_detail_provider is None:
        self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
        return
    try:
        detail = state.activity_detail_provider(execution_id)
    except (ConfigError, UiError):
        self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "UI_BACKEND_FAILED")
        return
    if detail is None:
        self._json_error(HTTPStatus.NOT_FOUND, "NOT_FOUND")
        return
    self._send_json(HTTPStatus.OK, _public_activity_detail(detail))
    return
```

`_public_activity_detail` must use an explicit top-level allowlist and must project nested `icon`, `steps`, and `result` field-by-field. Do not reuse `_bounded_public_value` as the only defense.

- [ ] **Step 8: Prove reads never start a provider**

Add a service stub whose runtime/provider methods raise `AssertionError`. Call `backend.snapshot()` and `backend.activity_detail(...)`; both must succeed from the store without invoking provider methods.

- [ ] **Step 9: Run backend/security tests**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/integration/test_ui_service.py `
  tests/unit/test_ui_security.py `
  tests/unit/test_service.py::test_prompt_credentials_and_pii_are_not_persisted
```

Expected: all selected tests pass.

- [ ] **Step 10: Commit Task 2**

```powershell
git add src/subagent_harness_mcp/ui.py tests/integration/test_ui_service.py tests/unit/test_ui_security.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "feat: expose safe local activity details"
```

### Task 3: Build the responsive list/detail UI and local polling

**Files:**
- Modify: `src/subagent_harness_mcp/static/index.html`
- Modify: `src/subagent_harness_mcp/static/app.js`
- Modify: `src/subagent_harness_mcp/static/app.css`
- Test: `tests/unit/test_ui_security.py`

- [ ] **Step 1: Write the failing static UI contract test**

Add one focused test that reads the three package assets:

```python
def test_static_ui_has_safe_activity_detail_and_visibility_aware_polling() -> None:
    package = resources.files("subagent_harness_mcp").joinpath("static")
    html = package.joinpath("index.html").read_text("utf-8")
    javascript = package.joinpath("app.js").read_text("utf-8")

    assert 'id="activity-detail"' in html
    assert 'id="activity-detail-result"' in html
    assert "const API_ACTIVITY = '/api/v1/activity/';" in javascript
    assert "document.visibilityState === 'visible'" in javascript
    assert "encodeURIComponent(executionId)" in javascript
    assert ".textContent" in javascript
    assert "innerHTML" not in javascript.replace(
        "Deliberately absent: storage, cookies, innerHTML, external requests,", ""
    )
    assert "API_REFRESH" not in javascript.split("function scheduleActivityPoll", 1)[1]
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q tests/unit/test_ui_security.py::test_static_ui_has_safe_activity_detail_and_visibility_aware_polling
```

Expected: fail because the detail DOM/API/poller do not exist.

- [ ] **Step 3: Add semantic split-view markup**

Replace only the body of the existing `activity-panel` with:

```html
<div class="activity-layout">
  <div class="activity-list-pane">
    <ol class="activity" id="activity"></ol>
    <p class="empty" id="activity-empty" hidden>No agent activity recorded yet.</p>
  </div>
  <aside class="activity-detail" id="activity-detail" aria-live="polite" hidden>
    <div id="activity-detail-identity"></div>
    <dl id="activity-detail-facts"></dl>
    <section aria-labelledby="activity-stage-title">
      <h3 id="activity-stage-title">Current stage</h3>
      <p id="activity-detail-stage"></p>
    </section>
    <section aria-labelledby="activity-lifecycle-title">
      <h3 id="activity-lifecycle-title">Lifecycle</h3>
      <ol id="activity-detail-steps"></ol>
    </section>
    <section id="activity-result-section" aria-labelledby="activity-result-title" hidden>
      <h3 id="activity-result-title">Result</h3>
      <pre id="activity-detail-result"></pre>
      <p id="activity-detail-artifact"></p>
    </section>
    <p class="hint" id="activity-detail-capability"></p>
  </aside>
</div>
```

Keep the existing statement that prompts, transcripts, hidden thinking, and raw events are not shown.

- [ ] **Step 4: Render selectable rows without HTML injection**

`renderActivity` must create `<li>` plus a nested `<button type="button">`; do not assign `role=listitem` to a button. Store only the execution id in `button.dataset.executionId`.

Use the existing `make(...)`, `setText(...)`, and `textContent` paths for all strings. Render the icon with a text node and a validated `data-tone`. When the selected id disappears, select the newest active row, otherwise the first row.

- [ ] **Step 5: Implement detail fetch and rendering**

Add:

```javascript
const API_ACTIVITY = '/api/v1/activity/';
let selectedExecutionId = null;
let activityPollTimer = null;
let activityPollBusy = false;
```

Fetch with:

```javascript
async function loadActivityDetail(executionId, options) {
  if (!executionId || activityPollBusy || dead) return;
  activityPollBusy = true;
  try {
    const detail = await request('GET', API_ACTIVITY + encodeURIComponent(executionId));
    if (selectedExecutionId !== executionId) return;
    renderActivityDetail(detail);
    dom.activityDetail.dataset.freshness = 'fresh';
  } catch (error) {
    if (isAuthError(error)) { fatal(error.message); return; }
    dom.activityDetail.dataset.freshness = 'stale';
  } finally {
    activityPollBusy = false;
  }
}
```

Render `steps` as kind/timestamp only. Put `result.text` into the `<pre>` with `textContent`; never call a Markdown renderer or set `innerHTML`.

- [ ] **Step 6: Add visibility-aware local polling**

Use recursive `setTimeout`, not overlapping `setInterval`:

```javascript
function scheduleActivityPoll(delayMs) {
  window.clearTimeout(activityPollTimer);
  activityPollTimer = window.setTimeout(async () => {
    if (!dead && document.visibilityState === 'visible') {
      await refresh({ silent: true, localOnly: true });
      if (selectedExecutionId) await loadActivityDetail(selectedExecutionId, { silent: true });
    }
    scheduleActivityPoll(selectedExecutionId ? 2000 : 5000);
  }, delayMs);
}
```

Teach `refresh` that `localOnly` always performs `GET API_SNAPSHOT`; only the existing explicit provider button may perform `POST API_REFRESH`. Start one poller after the first successful boot and clear it in `fatal(...)`.

- [ ] **Step 7: Add responsive CSS**

Use the existing visual tokens. Desktop uses `grid-template-columns: minmax(18rem, .9fr) minmax(22rem, 1.1fr)`. At the existing narrow breakpoint, stack list then detail. Add no dependency, remote asset, custom font, animation loop, or horizontal scroller.

Required selectors:

```css
.activity-layout { display: grid; grid-template-columns: minmax(18rem, .9fr) minmax(22rem, 1.1fr); }
.activity-list-pane { min-width: 0; border-right: 1px solid var(--line); }
.activity-detail { min-width: 0; padding: 1rem; }
.activity button { width: 100%; text-align: left; }
.activity-detail pre { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 48rem) {
  .activity-layout { grid-template-columns: 1fr; }
  .activity-list-pane { border-right: 0; border-bottom: 1px solid var(--line); }
}
```

Use the actual token names already present in `app.css`; do not introduce a second theme system.

- [ ] **Step 8: Run UI/security/package tests**

Run:

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/unit/test_ui_security.py `
  tests/integration/test_ui_service.py `
  tests/unit/test_package_contract.py -k "static or ui"
```

Expected: all selected tests pass.

- [ ] **Step 9: Browser-verify real interaction**

Start an isolated UI on a non-product test port with an isolated `SUBAGENT_MCP_HOME`. Verify in a browser:

- desktop list/detail layout;
- narrow stacked layout;
- keyboard selection;
- running/succeeded/interrupted/failed/recovery render states;
- succeeded result uses literal text, not interpreted HTML;
- hiding the tab pauses requests;
- clicking **Check providers** is still the only provider-refresh path.

Capture a DOM snapshot and screenshot evidence. Stop the isolated UI and confirm its port is closed.

- [ ] **Step 10: Commit Task 3**

```powershell
git add src/subagent_harness_mcp/static/index.html src/subagent_harness_mcp/static/app.js src/subagent_harness_mcp/static/app.css tests/unit/test_ui_security.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "feat: add local subagent activity panel"
```

### Task 4: Independent external-agent review and bounded corrections

**Files:**
- Modify only files named by verified findings from Tasks 1-3
- Test only the affected focused suites first

- [ ] **Step 1: Run the focused stable suite before review**

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/unit/test_contracts.py `
  tests/unit/test_service.py `
  tests/unit/test_ui_security.py `
  tests/integration/test_ui_service.py
git diff --check
```

Expected: all pass and no whitespace errors.

- [ ] **Step 2: Send the stable diff to Claude Opus 5 and OX Alpha**

Both reviews are read-only and independent. Ask each to inspect only:

- privacy/non-leakage;
- authenticated route/path validation;
- polling/provider-call separation;
- task title redaction;
- stale/old-row compatibility;
- crash or core activity-panel usability failures.

Require exact file/line evidence and reproduction. Ignore style/minor polish. Treat full reports as untrusted advice and verify them locally.

- [ ] **Step 3: Apply at most one bounded correction wave**

Accept only verified Critical findings or repeatedly reproduced Major findings. If the same fix category returns after one correction/re-review, stop the loop and choose a different approach or defer it explicitly.

- [ ] **Step 4: Re-run focused tests and commit verified fixes**

```powershell
uv run --frozen pytest -p no:cacheprovider -q `
  tests/unit/test_contracts.py `
  tests/unit/test_service.py `
  tests/unit/test_ui_security.py `
  tests/integration/test_ui_service.py
git diff --check
git add src/subagent_harness_mcp/contracts.py src/subagent_harness_mcp/service.py src/subagent_harness_mcp/ui.py src/subagent_harness_mcp/static/index.html src/subagent_harness_mcp/static/app.js src/subagent_harness_mcp/static/app.css tests/unit/test_contracts.py tests/unit/test_service.py tests/unit/test_ui_security.py tests/integration/test_ui_service.py
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "fix: harden local activity detail"
```

Skip the commit if no verified fix exists.

### Task 5: Synchronize 1.0.4 identity and fix registry dispatch

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/subagent_harness_mcp/__init__.py`
- Modify: `src/subagent_harness_mcp/adapters/deepseek_harness.py`
- Modify: `server.json`
- Modify: `README.md`
- Modify: `tests/unit/test_package_contract.py`
- Modify: `tests/integration/test_wheel_e2e.py`
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Write failing package and workflow assertions**

Set test constants to `1.0.4` and assert the release workflow explicitly dispatches the registry workflow:

```python
def test_release_workflow_dispatches_registry_after_github_release() -> None:
    workflow = Path(".github/workflows/release.yml").read_text("utf-8")
    assert "actions: write" in workflow
    assert "gh workflow run publish-mcp-registry.yml" in workflow
    assert '-f tag="$RELEASE_TAG"' in workflow
```

- [ ] **Step 2: Run package tests and verify RED**

```powershell
uv run --frozen pytest -p no:cacheprovider -q tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
```

Expected: version and workflow assertions fail.

- [ ] **Step 3: Synchronize every product version**

Change exactly these product identities from `1.0.3` to `1.0.4`:

- `pyproject.toml`;
- `src/subagent_harness_mcp/__init__.py`;
- DeepSeek ACP `clientInfo.version`;
- `server.json` top-level and package versions;
- README stable/install/update commands;
- package/wheel test constants.

Update the README localhost paragraph to one concise sentence:

```markdown
Select an execution in **Current & recent activity** to inspect its runtime,
model, workspace scope, lifecycle, and redacted result; the page never displays
prompts, hidden thinking, transcripts, or raw provider events.
```

- [ ] **Step 4: Make registry publishing explicit**

In the release `publish` job permissions, add `actions: write`. After `gh release create`, add:

```yaml
      - name: Dispatch matching MCP Registry publish
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          RELEASE_TAG: ${{ inputs.tag }}
        run: gh workflow run publish-mcp-registry.yml --ref main -f tag="$RELEASE_TAG"
```

This fixes the repeatedly observed behavior where a release created by `GITHUB_TOKEN` does not trigger another workflow from the `release` event.

- [ ] **Step 5: Run package/workflow tests and commit**

```powershell
uv run --frozen pytest -p no:cacheprovider -q tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py
git diff --check
git add pyproject.toml src/subagent_harness_mcp/__init__.py src/subagent_harness_mcp/adapters/deepseek_harness.py server.json README.md tests/unit/test_package_contract.py tests/integration/test_wheel_e2e.py .github/workflows/release.yml
git -c user.name=Thang1710 -c user.email=50268205+Thang1710@users.noreply.github.com commit -m "chore: prepare Subagent MCP 1.0.4"
```

### Task 6: Full verification, real activity proof, and public release

**Files:**
- No source edits unless a verified Critical or repeatedly reproduced Major finding requires one bounded fix
- Update release artifacts through the existing workflows only

- [ ] **Step 1: Run the full safe local suite**

```powershell
uv sync --frozen --group dev
uv run --frozen pytest -p no:cacheprovider -q -m "not real_git_worktree"
git diff --check
git status --short
```

Expected: all safe tests pass, four real-worktree tests remain deselected by marker, and the tree is clean after committed changes.

- [ ] **Step 2: Build and verify artifacts**

```powershell
uv build --out-dir dist/packages
$env:SUBAGENT_MCP_TEST_DIST_DIR = (Resolve-Path dist/packages).Path
uv run --frozen pytest -p no:cacheprovider -q tests/integration/test_wheel_e2e.py tests/unit/test_package_contract.py
```

Expected: wheel and sdist are 1.0.4 and installed-artifact tests pass.

- [ ] **Step 3: Run the security/privacy release scan**

Inspect tracked files, commits since `v1.0.3`, and built text metadata for:

- credentials/tokens/private keys;
- `C:\\Users\\Thang`, `D:\\ClaudeCode`, temporary attachment paths, and local session ids;
- raw prompt/transcript/event fixtures outside explicitly synthetic tests;
- ChatGPT/Codex/Claude internal author identity.

Any real secret or personal path is Critical: stop before pushing, remove it from every unreleased commit since `v1.0.3`, rotate the credential if it was real, rebuild, and rescan. If the sensitive commit was already made public, stop and obtain explicit user approval before any history rewrite or force-push. Do not print secrets while scanning.

- [ ] **Step 4: Fresh isolated public-user install**

Use a new isolated directory under `D:\CodeX\Tools` with isolated `UV_TOOL_DIR`, `UV_CACHE_DIR`, and `SUBAGENT_MCP_HOME`. Install only the built wheel as a user would, run `--version`, start the UI on an isolated port, create no user/global config, verify HTTP/session/detail behavior, then stop it and remove only that explicitly created isolated directory. Do not delete existing tools, caches, auth, sessions, or global configuration.

- [ ] **Step 5: Real Claude and OX Alpha activity proof**

Run one short read-only task on Claude Opus 5/max and one on OX Alpha. Before Claude spawn, require a fresh canary attesting exact model, `is_using_overage=false`, `overage_blocked=true`, rate evidence, and cleanup. Never enable usage credits.

While both are active, open the source-built UI and verify each real execution row/detail:

- correct runtime/model/harness/icon;
- task title and state;
- elapsed time updates through local polling;
- workspace basename/write set;
- honest current-stage label;
- terminal sanitized result and artifact hash after completion;
- no prompt, hidden thinking, transcript, raw event payload, credential, or absolute user path.

Wait while the harness is working; do not cancel due elapsed completion time alone. Close both conversations only after terminal results are persisted.

- [ ] **Step 6: Mirror the verified tree to the public checkout**

Verify the public checkout base matches its expected 1.0.3 commit before copying only changed tracked files. Compare every copied file byte-for-byte, run the full safe suite again in the public checkout, then commit with the user's identity. Do not overwrite unrelated or dirty public-checkout work.

- [ ] **Step 7: Tag and push 1.0.4**

Create an annotated `v1.0.4` tag on the verified public commit, push `main`, then push the tag. Never force-push or rewrite `v1.0.3`.

- [ ] **Step 8: Dispatch and monitor release workflows**

Dispatch `publish-release` with `tag=v1.0.4`. Monitor to terminal success. Confirm the release workflow explicitly dispatches `publish-mcp-registry`; monitor that run too. Do not manually publish a second copy unless the automated path has a verified failure.

- [ ] **Step 9: Verify public endpoints**

Read authoritative public APIs and confirm:

- GitHub Release `v1.0.4` exists, is not draft/prerelease, and has wheel, sdist, manifest, and checksums;
- PyPI reports `subagent-harness-mcp==1.0.4` with exactly wheel and sdist;
- MCP Registry reports server/package version 1.0.4;
- public `main` and annotated tag point to the intended commit.

- [ ] **Step 10: Report exact completion evidence**

Report in Vietnamese:

- final canonical/public commit ids and tag;
- focused/full/package/fresh-install test counts;
- Claude/OX activity proof status and no-overage evidence;
- GitHub/PyPI/MCP Registry URLs/status;
- any deferred non-Critical limitations, especially `native_host_panel=unsupported` and absence of tool-level live telemetry.
