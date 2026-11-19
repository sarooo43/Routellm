# Fake audit: every test double, what it imitates, and what covers the real thing

A double that drifts from the real shape is this project's known failure mode
(a fake returning HTTP 201 where the real API returns 200 hid a real bug once).
For each double below: what it stands in for, and whether a test that uses a
**real socket / real client** also covers the same behaviour.

| Double | File | Imitates | Real-shape risk | Covered by a real-wire test? |
|---|---|---|---|---|
| `StubProvider` | `tests/integration/conftest.py:40` | a `ModelProvider.generate()` returning a `ProviderResponse` | It returns the *already-parsed* object, so any error in JSON parsing is bypassed. It exhausts to `no_stub_response`, a string no real provider returns. | YES — `test_discovery_wire_contract.py` drives `GroqProvider` against a `ThreadingHTTPServer` and asserts on the parsed result. |
| `ScriptedGemini` | `test_provider_diagnostics.py:59` | the Gemini provider | Same bypass; also fixes the model list. | PARTIAL — the diagnostics tests probe the walk adapter; the Gemini list shape itself is pinned in `test_discovery_wire_contract.py::test_the_gemini_list_shape_over_a_real_socket`. |
| `ModelAwareGeminiStub` | `test_model_explorer.py:103` | a provider that answers per-model | Per-model behaviour is what the test is about; the stub is the subject, not a stand-in. | N/A. |
| `RateLimitedStub` | `test_model_explorer.py:280` | a 429 with Retry-After | Retry-After parsing is bypassed. | YES — real 429 body is served by the wire-contract stub. |
| `PolicyStub` | `test_walk_policy.py:59` | arbitrary walk outcomes | Outcomes are injected, so the classification that produces them is bypassed. | PARTIAL — classification has its own real-wire tests. |
| `_Upstream` | `test_discovery_wire_contract.py:36` | a real provider HTTP server | None by design: it is a real socket with real status/headers/bodies. | It IS the real-wire test. **It had a bug this session** — a leaked class attribute made it order-dependent (fixed, see the flakiness note). |
| `FakeConn` / `FakeEngine` | `test_deployment_resilience.py:210` | an async SQLAlchemy engine | Connection/session semantics are faked, so pool behaviour is NOT modelled. | YES — `test_concurrent_walk_pool.py` runs a real uvicorn against real SQLite, and the full suite runs against real Postgres. |
| `RecordingBreaker` | `tests/integration/conftest.py:63` | a circuit breaker | It delegates to the real class and only counts calls, so it cannot mask breaker logic. | N/A — delegation by design. |
| `respx` routes | several integration tests | HTTP at the transport layer | **Highest risk**: respx installs a *global* transport, so a test that forgets a route gets `AllMockedAssertionError` rather than a network call. It also leaks across tests if not stopped. | YES for the shapes it fakes — the wire-contract module exists precisely to re-run those assertions over a socket, and it calls `respx_mock.stop()` in an autouse fixture. |
| `FakeAsyncRedis` | `conftest.py:153` | Redis | Command coverage, not server semantics (no real eviction, no real expiry clock). | YES — the same suite is run against a real Redis (`fakeredis` TcpFakeServer) and the whole suite passes (1007 tests). |
| `FakeUi` / `FakeGr` | `test_hf_app.py:168` | gradio | It only checks the app wiring, not rendering. | NO — gradio is an optional dependency (9 skips). Declared here rather than claimed green. |

## What this audit did not do

* It did not test the gradio/Hugging Face Space path: `gradio` and `spaces` are
  not installed, so 9 tests skip. Those skips are honest, not hidden.
* It did not replace `respx` with sockets anywhere. The wire-contract module
  already re-asserts the important shapes over a real socket, and rewriting the
  rest would be a large, risky refactor rather than a defect fix.
