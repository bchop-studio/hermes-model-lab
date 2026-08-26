import asyncio
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def _load_backend_module(suffix: str):
    module_name = f"hermes_dashboard_plugin_model_lab_{suffix}"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "dashboard" / "plugin_api.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _fake_completion(text: str = "MODEL_LAB_OK"):
    return SimpleNamespace(
        text=text,
        provider="test-provider",
        model="test-model",
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=None,
        ),
    )


def test_agent_plugin_manifest_and_register_entrypoint_exist():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["name"] == "hermes-model-lab"
    assert manifest["version"] == "0.1.0"
    assert manifest["license"] == "MIT"
    assert "manifest_version" not in manifest
    assert "api_version" not in manifest

    spec = importlib.util.spec_from_file_location(
        "hermes_model_lab_plugin", ROOT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert callable(module.register)


def test_backend_manifest_mounts_plugin_scoped_health_route():
    dashboard = ROOT / "dashboard"
    manifest = json.loads((dashboard / "manifest.json").read_text(encoding="utf-8"))

    assert manifest == {
        "name": "hermes-model-lab",
        "label": "Model Lab",
        "version": "0.1.0",
        "description": "Stateless model playground for Hermes Desktop",
        "tab": {"hidden": True},
        "api": "plugin_api.py",
    }

    module_name = "hermes_dashboard_plugin_hermes-model-lab"
    spec = importlib.util.spec_from_file_location(
        module_name, dashboard / manifest["api"]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app).get("/api/plugins/hermes-model-lab/health")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "plugin": "hermes-model-lab",
        "version": "0.1.0",
    }


def test_completion_route_uses_one_bounded_async_call(monkeypatch):
    dashboard = ROOT / "dashboard"
    module_name = "hermes_dashboard_plugin_model_lab_completion"
    spec = importlib.util.spec_from_file_location(
        module_name, dashboard / "plugin_api.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    captured = {}

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return SimpleNamespace(
                text="MODEL_LAB_OK",
                provider="test-provider",
                model="test-model",
                usage=SimpleNamespace(
                    input_tokens=3,
                    output_tokens=2,
                    total_tokens=5,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=None,
                ),
            )

    monkeypatch.setattr(module, "_llm", FakeLlm(), raising=False)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app).post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "  Reply exactly MODEL_LAB_OK  "},
    )

    assert response.status_code == 200
    assert captured == {
        "messages": [{"role": "user", "content": "Reply exactly MODEL_LAB_OK"}],
        "kwargs": {
            "max_tokens": 512,
            "timeout": 60.0,
            "purpose": "model-lab",
        },
    }
    body = response.json()
    # T006: explicit completion state, authoritative attribution, and a
    # measured elapsed time ride along with the text and usage.
    assert body["state"] == "complete"
    assert body["provider"] == "test-provider"
    assert body["model"] == "test-model"
    assert isinstance(body["elapsed_ms"], int)
    assert body["elapsed_ms"] >= 0
    assert body["text"] == "MODEL_LAB_OK"
    assert body["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": None,
    }


def test_completion_route_rejects_blank_prompt(monkeypatch):
    module = _load_backend_module("blank_prompt")
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    monkeypatch.setattr(module, "_llm", FakeLlm())
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app).post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "   \n  "},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Prompt is required."}
    assert calls == 0


def test_completion_route_rejects_oversized_prompt(monkeypatch):
    module = _load_backend_module("oversized_prompt")
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    monkeypatch.setattr(module, "_llm", FakeLlm())
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app).post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "x" * 20_001},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Prompt is too large."}
    assert calls == 0


def test_completion_route_maps_timeout_to_safe_error(monkeypatch):
    module = _load_backend_module("timeout")

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            raise TimeoutError("provider timeout with SECRET_PROVIDER_DETAIL")

    monkeypatch.setattr(module, "_llm", FakeLlm())
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello"},
    )

    assert response.status_code == 504
    assert response.json() == {"detail": "Model request timed out."}
    assert "SECRET_PROVIDER_DETAIL" not in response.text


def test_completion_route_scrubs_provider_errors(monkeypatch, caplog):
    module = _load_backend_module("provider_error")
    secret_detail = "sk-secret-provider-detail prompt=private-text"

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            raise RuntimeError(secret_detail)

    monkeypatch.setattr(module, "_llm", FakeLlm())
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    with caplog.at_level(logging.WARNING):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/plugins/hermes-model-lab/complete",
            json={"prompt": "private-text"},
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Model request failed."}
    assert secret_detail not in response.text
    assert secret_detail not in caplog.text


def test_completion_cancellation_reaches_model_call(monkeypatch):
    module = _load_backend_module("cancellation")

    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        class FakeLlm:
            async def acomplete(self, messages, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        monkeypatch.setattr(module, "_llm", FakeLlm())
        task = asyncio.create_task(
            module.complete(module.CompletionRequest(prompt="hello"))
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_completion_route_rejects_unapproved_override_fields(monkeypatch):
    module = _load_backend_module("unapproved_override")
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    monkeypatch.setattr(module, "_llm", FakeLlm())
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    response = TestClient(app).post(
        "/api/plugins/hermes-model-lab/complete",
        json={
            "prompt": "hello",
            "provider": "unapproved-provider",
            "model": "unapproved-model",
        },
    )

    # T005 tightened this guard: overrides are validated against the
    # configured inventory before any model call, so an unapproved pair is
    # rejected as fail-closed 400 (not 422 schema noise). The invariant is
    # unchanged and stronger: no model call ever runs.
    assert response.status_code == 400
    assert calls == 0


# ─── Alias attribution: requested vs served identity ─────────────────────


def _alias_completion():
    return SimpleNamespace(
        text="ALIAS_OK",
        provider="nous",
        model="tencent/hy3:free",
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            total_tokens=5,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=None,
        ),
    )


def test_completion_reports_requested_and_served_identity_on_alias(monkeypatch):
    module = _load_backend_module("alias_identity")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return _alias_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={
            "prompt": "hello",
            "provider": "nous",
            "model": "stealth/ox-alpha",
        },
    )

    assert response.status_code == 200
    body = response.json()
    # Requested identity echoes the caller's selection; served identity is
    # authoritative provider facts. A Nous alias may serve a different model.
    assert body["requested_provider"] == "nous"
    assert body["requested_model"] == "stealth/ox-alpha"
    assert body["provider"] == "nous"
    assert body["model"] == "tencent/hy3:free"
    # Existing facts-only fields are preserved.
    assert body["state"] == "complete"
    assert body["text"] == "ALIAS_OK"
    assert body["usage"]["total_tokens"] == 5


def test_completion_without_selection_reports_null_requested_identity(monkeypatch):
    module = _load_backend_module("null_requested_identity")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return _fake_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["requested_provider"] is None
    assert body["requested_model"] is None
    assert body["provider"] == "test-provider"
    assert body["model"] == "test-model"


# ─── T005: allowed provider and model selection ──────────────────────────


def _t005_app(monkeypatch, module, llm=None):
    if llm is not None:
        monkeypatch.setattr(module, "_llm", llm, raising=False)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    return TestClient(app)


def _fake_inventory_payload():
    return {
        "providers": [
            {
                "slug": "nous",
                "name": "Nous Lab",
                "models": ["stealth/ox-alpha", "Hermes-4-405B"],
                # Host-only fields that must never reach the renderer:
                "key_env": "NOUS_API_KEY",
                "auth_type": "api_key",
                "base_url": "https://inference-api.nousresearch.com/v1",
                "pricing": {"prompt": 0.1},
                "authenticated": True,
            },
            {
                "slug": "openrouter",
                "name": "OpenRouter",
                "models": ["openai/gpt-4o-mini"],
                "key_env": "OPENROUTER_API_KEY",
            },
        ],
        "model": "stealth/ox-alpha",
        "provider": "nous",
    }


def _install_fake_inventory(monkeypatch, module):
    monkeypatch.setattr(
        module,
        "_build_model_inventory",
        lambda: _fake_inventory_payload(),
        raising=False,
    )


def test_models_route_returns_minimal_sanitized_projection(monkeypatch):
    module = _load_backend_module("models_projection")
    real_payload = _fake_inventory_payload()
    monkeypatch.setattr(
        module, "load_picker_context", lambda: object(), raising=False
    )
    monkeypatch.setattr(
        module,
        "build_model_options_payload",
        lambda ctx, **kwargs: real_payload,
        raising=False,
    )
    client = _t005_app(monkeypatch, module)

    response = client.get("/api/plugins/hermes-model-lab/models")

    assert response.status_code == 200
    body = response.json()
    assert body["active"] == {"provider": "nous", "model": "stealth/ox-alpha"}
    assert body["providers"] == [
        {
            "slug": "nous",
            "label": "Nous Lab",
            "models": ["Hermes-4-405B", "stealth/ox-alpha"],
        },
        {
            "slug": "openrouter",
            "label": "OpenRouter",
            "models": ["openai/gpt-4o-mini"],
        },
    ]
    text = response.text
    for forbidden in (
        "key_env",
        "NOUS_API_KEY",
        "OPENROUTER_API_KEY",
        "auth_type",
        "base_url",
        "inference-api.nousresearch.com",
        "pricing",
        "authenticated",
    ):
        assert forbidden not in text, f"leaked host-only field: {forbidden}"


def test_models_route_uses_safe_enumeration_arguments(monkeypatch):
    module = _load_backend_module("models_args")
    seen = {}

    def fake_build(ctx, **kwargs):
        seen.update(kwargs)
        return {"providers": [], "model": "", "provider": ""}

    monkeypatch.setattr(
        module, "load_picker_context", lambda: "CTX", raising=False
    )
    monkeypatch.setattr(
        module, "build_model_options_payload", fake_build, raising=False
    )

    client = _t005_app(monkeypatch, module)
    response = client.get("/api/plugins/hermes-model-lab/models")

    assert response.status_code == 200
    assert seen.get("explicit_only") is True
    assert seen.get("include_unconfigured") is False


def test_completion_with_allowed_selection_passes_provider_and_model(monkeypatch):
    module = _load_backend_module("allowed_selection")
    _install_fake_inventory(monkeypatch, module)
    captured = {}

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            captured.update(kwargs)
            return _fake_completion(text="SELECTED_OK")

    client = _t005_app(
        monkeypatch, module, FakeLlm()
    )
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={
            "prompt": "hello",
            "provider": "nous",
            "model": "stealth/ox-alpha",
        },
    )

    assert response.status_code == 200
    assert captured.get("provider") == "nous"
    assert captured.get("model") == "stealth/ox-alpha"
    assert captured.get("max_tokens") == 512


def test_completion_without_selection_keeps_active_default_path(monkeypatch):
    module = _load_backend_module("default_selection")
    _install_fake_inventory(monkeypatch, module)
    captured = {}

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            captured.update(kwargs)
            return _fake_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello"},
    )

    assert response.status_code == 200
    assert "provider" not in captured
    assert "model" not in captured


def test_completion_rejects_unknown_provider_before_model_call(monkeypatch):
    module = _load_backend_module("unknown_provider")
    _install_fake_inventory(monkeypatch, module)
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello", "provider": "ghost-provider", "model": "some-model"},
    )

    assert response.status_code == 400
    assert calls == 0


def test_completion_rejects_unconfigured_model_before_model_call(monkeypatch):
    module = _load_backend_module("unknown_model")
    _install_fake_inventory(monkeypatch, module)
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello", "provider": "nous", "model": "not-in-list"},
    )

    assert response.status_code == 400
    assert calls == 0


def test_completion_rejects_partial_selection(monkeypatch):
    module = _load_backend_module("partial_selection")
    _install_fake_inventory(monkeypatch, module)
    calls = 0

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello", "provider": "nous"},
    )

    assert response.status_code == 400
    assert calls == 0


def test_completion_maps_unconsented_override_to_fail_closed_403(monkeypatch):
    from agent.plugin_llm import PluginLlmTrustError

    module = _load_backend_module("unconsented_override")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            raise PluginLlmTrustError(
                "plugin 'hermes-model-lab' lacks llm.model_override trust"
            )

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={
            "prompt": "hello",
            "provider": "nous",
            "model": "stealth/ox-alpha",
        },
    )

    assert response.status_code == 403
    assert "llm.model_override" not in response.text
    assert "trust" not in response.text.lower()


def test_plugin_manifest_declares_override_capabilities():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert "llm.provider_override" in manifest["capabilities"]
    assert "llm.model_override" in manifest["capabilities"]


def test_models_route_filters_unavailable_and_unauthenticated_rows(monkeypatch):
    module = _load_backend_module("models_availability")
    monkeypatch.setattr(
        module,
        "_build_model_inventory",
        lambda: {
            "providers": [
                {
                    "slug": "nous",
                    "name": "Nous",
                    "authenticated": True,
                    "models": ["allowed-model", "blocked-model"],
                    "unavailable_models": ["blocked-model"],
                },
                {
                    "slug": "lost-auth",
                    "name": "Lost auth",
                    "authenticated": False,
                    "models": ["must-not-appear"],
                },
            ],
            "provider": "nous",
            "model": "blocked-model",
        },
    )
    client = _t005_app(monkeypatch, module)

    response = client.get("/api/plugins/hermes-model-lab/models")

    assert response.status_code == 200
    assert response.json() == {
        "active": {"provider": "", "model": ""},
        "providers": [
            {
                "slug": "nous",
                "label": "Nous",
                "models": ["allowed-model"],
            }
        ],
    }
    for provider, model in (
        ("nous", "blocked-model"),
        ("lost-auth", "must-not-appear"),
    ):
        rejected = client.post(
            "/api/plugins/hermes-model-lab/complete",
            json={"prompt": "hello", "provider": provider, "model": model},
        )
        assert rejected.status_code == 400


def test_completion_maps_inventory_failure_to_safe_503(monkeypatch):
    module = _load_backend_module("selection_inventory_failure")
    calls = 0

    def broken_inventory():
        raise RuntimeError("SECRET_INVENTORY_DETAIL")

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            nonlocal calls
            calls += 1
            return _fake_completion()

    monkeypatch.setattr(module, "_build_model_inventory", broken_inventory)
    client = _t005_app(monkeypatch, module, FakeLlm())

    response = client.post(
        "/api/plugins/hermes-model-lab/complete",
        json={"prompt": "hello", "provider": "nous", "model": "model"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Model options are unavailable."}
    assert "SECRET_INVENTORY_DETAIL" not in response.text
    assert calls == 0


# ─── T006: show response facts without guessing ──────────────────────────


def test_completion_with_no_usage_reports_unavailable_not_zero(monkeypatch):
    """A provider that returns no usage object must not be shown as zeros."""
    module = _load_backend_module("no_usage")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            result = SimpleNamespace(
                text="NO_USAGE_OK",
                provider="nous",
                model="stealth/ox-alpha",
                usage=None,
            )
            return result

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "complete"
    assert body["usage"] is None


def test_completion_with_zero_default_usage_reports_unavailable(monkeypatch):
    """The PluginLlm zero-default placeholder means 'not reported', not zero."""
    module = _load_backend_module("placeholder_usage")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return SimpleNamespace(
                text="PLACEHOLDER_OK",
                provider="nous",
                model="stealth/ox-alpha",
                usage=SimpleNamespace(
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=None,
                ),
            )

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
    )

    assert response.status_code == 200
    assert response.json()["usage"] is None


def test_completion_cost_only_when_host_returns_numeric_value(monkeypatch):
    module = _load_backend_module("cost_numeric")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return SimpleNamespace(
                text="COST_OK",
                provider="nous",
                model="stealth/ox-alpha",
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0021,
                ),
            )

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
    )

    assert response.status_code == 200
    usage = response.json()["usage"]
    assert usage["cost_usd"] == 0.0021
    assert usage["input_tokens"] == 10


def test_completion_preserves_authoritative_cost_with_zero_token_placeholder(monkeypatch):
    module = _load_backend_module("cost_only")
    _install_fake_inventory(monkeypatch, module)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            return SimpleNamespace(
                text="FREE_COST_OK",
                provider="nous",
                model="stealth/ox-alpha",
                usage=SimpleNamespace(
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=0.0,
                ),
            )

    client = _t005_app(monkeypatch, module, FakeLlm())
    response = client.post(
        "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
    )

    assert response.status_code == 200
    assert response.json()["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
    }


def test_completion_maps_rate_limit_to_safe_429_without_detail(monkeypatch, caplog):
    module = _load_backend_module("rate_limit")
    _install_fake_inventory(monkeypatch, module)
    secret_detail = "rate limited: account sk-secret-123 over quota"

    class RateLimitError(RuntimeError):
        # Metadata-only carrier: the message text is never trusted.
        status_code = 429
        error_class = "rate_limit"

        def __init__(self):
            super().__init__(secret_detail)

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            raise RateLimitError()

    monkeypatch.setattr(module, "_llm", FakeLlm(), raising=False)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    with caplog.at_level(logging.WARNING):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
        )

    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "Model request was rate limited."
    # Detection used status/class metadata only; raw detail never escapes.
    assert secret_detail not in response.text
    assert secret_detail not in caplog.text


def test_rate_limit_detection_uses_metadata_only(monkeypatch, caplog):
    """Even without metadata, a rate-limit-looking error stays scrubbed."""
    module = _load_backend_module("rate_limit_metadata")
    secret_detail = "429 Too Many Requests for org sk-live-xyz"

    class FakeLlm:
        async def acomplete(self, messages, **kwargs):
            raise RuntimeError(secret_detail)

    monkeypatch.setattr(module, "_llm", FakeLlm(), raising=False)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes-model-lab")
    with caplog.at_level(logging.WARNING):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/plugins/hermes-model-lab/complete", json={"prompt": "hello"}
        )

    # No trustworthy metadata: generic failure path, still fully scrubbed.
    assert response.status_code == 502
    assert secret_detail not in response.text
    assert secret_detail not in caplog.text
