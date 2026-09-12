"""Hermes Model Lab plugin-scoped backend routes."""

import asyncio
import logging
import time
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

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
CANCEL_RACE_TTL_SECONDS = 120.0
MAX_PENDING_CANCELLATIONS = 256

logger = logging.getLogger(__name__)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    provider: str | None = None
    model: str | None = None
    run_id: str = Field(
        default_factory=lambda: uuid4().hex,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class CancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")


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
_active_runs: dict[str, asyncio.Task] = {}
_pending_cancellations: dict[str, float] = {}


def _prune_pending_cancellations() -> None:
    cutoff = time.monotonic() - CANCEL_RACE_TTL_SECONDS
    expired = [run_id for run_id, seen_at in _pending_cancellations.items() if seen_at < cutoff]
    for run_id in expired:
        _pending_cancellations.pop(run_id, None)
    while len(_pending_cancellations) >= MAX_PENDING_CANCELLATIONS:
        _pending_cancellations.pop(next(iter(_pending_cancellations)))


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
    _prune_pending_cancellations()
    if _pending_cancellations.pop(request.run_id, None) is not None:
        raise HTTPException(status_code=409, detail="Model request cancelled.")
    if request.run_id in _active_runs:
        raise HTTPException(status_code=409, detail="That model run is already active.")
    model_task = asyncio.create_task(
        _llm.acomplete(
            [{"role": "user", "content": prompt}],
            **call_kwargs,
        )
    )
    _active_runs[request.run_id] = model_task
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(
            model_task,
            timeout=MODEL_TIMEOUT_SECONDS + 5.0,
        )
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
        raise HTTPException(status_code=409, detail="Model request cancelled.") from None
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
        if _active_runs.get(request.run_id) is model_task:
            _active_runs.pop(request.run_id, None)
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


@router.post("/cancel")
async def cancel(request: CancelRequest) -> dict:
    task = _active_runs.get(request.run_id)
    if task is None or task.done():
        _prune_pending_cancellations()
        _pending_cancellations[request.run_id] = time.monotonic()
        return {"cancelled": True}
    task.cancel()
    return {"cancelled": True}
