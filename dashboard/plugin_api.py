"""Hermes Model Lab plugin-scoped backend routes."""

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from hermes_cli.inventory import (
    build_model_options_payload,
    load_picker_context,
)
from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager


PLUGIN_ID = "hermes-model-lab"
PLUGIN_VERSION = "0.1.0"
MAX_PROMPT_CHARS = 20_000
MAX_OUTPUT_TOKENS = 512
MODEL_TIMEOUT_SECONDS = 60.0

logger = logging.getLogger(__name__)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    provider: str | None = None
    model: str | None = None


def _create_llm():
    manifest = PluginManifest(name=PLUGIN_ID, key=PLUGIN_ID)
    return PluginContext(manifest, get_plugin_manager()).llm


def _build_model_inventory() -> dict:
    return build_model_options_payload(
        load_picker_context(),
        explicit_only=True,
        include_unconfigured=False,
    )


def _sanitize_model_inventory(
    payload: dict,
) -> tuple[dict[str, set[str]], dict[str, str], list[dict], dict[str, str]]:
    """Return callable model allowlists and the minimal renderer projection."""
    catalog: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    provider_rows: dict[str, dict] = {}
    for row in payload.get("providers") or []:
        if row.get("authenticated") is False:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        raw_unavailable = row.get("unavailable_models") or []
        unavailable = (
            {str(model).strip() for model in raw_unavailable}
            if isinstance(raw_unavailable, (list, tuple, set))
            else set()
        )
        models = {
            str(model).strip()
            for model in (row.get("models") or [])
            if str(model).strip() and str(model).strip() not in unavailable
        }
        if not models:
            continue
        label = str(row.get("name") or row.get("label") or slug)
        catalog[slug] = models
        labels[slug] = label
        provider_rows[slug] = {
            "slug": slug,
            "label": label,
            "models": sorted(models),
        }

    active_provider = str(payload.get("provider") or "")
    active_model = str(payload.get("model") or "")
    if active_model not in catalog.get(active_provider, set()):
        active_provider = ""
        active_model = ""
    return (
        catalog,
        labels,
        list(provider_rows.values()),
        {"provider": active_provider, "model": active_model},
    )


def _model_catalog() -> tuple[dict[str, set[str]], dict[str, str]]:
    """Sanitized allowlists derived from the safe host inventory."""
    catalog, labels, _providers, _active = _sanitize_model_inventory(
        _build_model_inventory()
    )
    return catalog, labels


def _validate_selection(provider: str | None, model: str | None) -> None:
    """Fail closed on any unknown, unconfigured, or mismatched pair."""
    if provider is None and model is None:
        return
    if provider is None or model is None:
        raise HTTPException(
            status_code=400,
            detail="Provider and model must be selected together.",
        )
    try:
        catalog, _labels = _model_catalog()
    except Exception:
        logger.warning("Model Lab inventory unavailable during selection")
        raise HTTPException(
            status_code=503, detail="Model options are unavailable."
        ) from None
    allowed_models = catalog.get(provider)
    if allowed_models is None:
        raise HTTPException(
            status_code=400, detail="That provider is not configured."
        )
    if model not in allowed_models:
        raise HTTPException(
            status_code=400, detail="That model is not configured."
        )


router = APIRouter()
_llm = _create_llm()


@router.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "plugin": PLUGIN_ID,
        "version": PLUGIN_VERSION,
    }


@router.get("/models")
async def models() -> dict:
    try:
        payload = _build_model_inventory()
    except Exception:
        logger.warning("Model Lab inventory unavailable")
        raise HTTPException(
            status_code=503, detail="Model options are unavailable."
        ) from None
    _catalog, _labels, providers, active = _sanitize_model_inventory(payload)
    return {"active": active, "providers": providers}


def _serialize_usage(usage) -> dict | None:
    """Serialize only provider-reported facts. Missing or placeholder usage
    (the PluginLlm zero-default means 'not reported') serializes as None so
    the renderer can show Unavailable instead of pretending zero tokens."""
    if usage is None:
        return None
    fields = (
        ("input_tokens", getattr(usage, "input_tokens", 0)),
        ("output_tokens", getattr(usage, "output_tokens", 0)),
        ("total_tokens", getattr(usage, "total_tokens", 0)),
        ("cache_read_tokens", getattr(usage, "cache_read_tokens", 0)),
        ("cache_write_tokens", getattr(usage, "cache_write_tokens", 0)),
    )
    cost_usd = getattr(usage, "cost_usd", None)
    cost_is_reported = isinstance(cost_usd, (int, float)) and not isinstance(
        cost_usd, bool
    )
    if not any(value for _name, value in fields) and not cost_is_reported:
        # All-zero usage with no cost is the PluginLlm default placeholder,
        # not a report. A numeric cost is authoritative even at 0.0.
        return None
    return {
        **{name: value for name, value in fields},
        "cost_usd": float(cost_usd) if cost_is_reported else None,
    }


@router.post("/complete")
async def complete(request: CompletionRequest) -> dict:
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required.")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="Prompt is too large.")
    _validate_selection(request.provider, request.model)
    call_kwargs: dict = {
        "max_tokens": MAX_OUTPUT_TOKENS,
        "timeout": MODEL_TIMEOUT_SECONDS,
        "purpose": "model-lab",
    }
    if request.provider is not None and request.model is not None:
        call_kwargs["provider"] = request.provider
        call_kwargs["model"] = request.model
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            _llm.acomplete(
                [{"role": "user", "content": prompt}],
                **call_kwargs,
            ),
            timeout=MODEL_TIMEOUT_SECONDS + 5.0,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise HTTPException(
            status_code=504, detail="Model request timed out."
        ) from None
    except PermissionError:
        logger.warning("Model Lab override rejected by host trust gate")
        raise HTTPException(
            status_code=403,
            detail="Provider and model selection is not enabled.",
        ) from None
    except HTTPException:
        raise
    except Exception as exc:
        # Rate-limit detection uses exception metadata only (a numeric
        # status_code or an explicit error_class). Message text is never
        # trusted, because it may carry provider secrets.
        exc_status = getattr(exc, "status_code", None)
        error_class = getattr(exc, "error_class", None)
        if exc_status == 429 or (
            isinstance(error_class, str) and error_class == "rate_limit"
        ):
            logger.warning("Model Lab completion rate limited")
            raise HTTPException(
                status_code=429, detail="Model request was rate limited."
            ) from None
        logger.warning("Model Lab completion failed type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Model request failed.") from None
    finally:
        elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "state": "complete",
        "text": result.text,
        # Requested identity echoes the caller's explicit selection (None
        # when none was sent); provider/model are authoritative served facts.
        # A host alias may serve a different model than requested.
        "requested_provider": request.provider,
        "requested_model": request.model,
        "provider": result.provider,
        "model": result.model,
        "elapsed_ms": elapsed_ms,
        "usage": _serialize_usage(result.usage),
    }
