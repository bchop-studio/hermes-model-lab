import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const REQUEST_TIMEOUT_MS = 70000
const SAFE_ERROR_MESSAGE = 'Something went wrong. Try again.'
const RATE_LIMIT_MESSAGE = 'Rate limited. Wait a moment and try again.'
const THEMED_OPTION_STYLE = {
  backgroundColor: 'var(--ui-bg-elevated)',
  color: 'var(--ui-text-primary)'
}

function formatElapsed(ms) {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null
  return `${(ms / 1000).toFixed(1)}s`
}

function formatTokens(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? String(value)
    : 'Unavailable'
}

function formatCost(value) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `$${value.toFixed(4)}`
    : null
}

// The backend rejects with a status code; ctx.rest surfaces it on the error.
// Rate limit is recognized by metadata only, never by message text.
function isRateLimitError(error) {
  const status =
    error?.statusCode ?? error?.status ?? error?.response?.status ?? null
  return status === 429
}

function ModelLabPane({ callbacks }) {
  const { loadHealth, loadModels, completePrompt } = callbacks
  const [health, setHealth] = useState({ state: 'checking' })
  const [prompt, setPrompt] = useState('')
  const [run, setRun] = useState({ state: 'idle', result: null })
  const [catalog, setCatalog] = useState(null)
  const [selection, setSelection] = useState({
    provider: '',
    model: ''
  })

  useEffect(() => {
    let active = true
    loadHealth()
      .then(result => {
        if (active) setHealth({ state: result?.ok ? 'ready' : 'unavailable' })
      })
      .catch(() => {
        if (active) setHealth({ state: 'unavailable' })
      })
    loadModels()
      .then(result => {
        if (!active || !result) return
        setCatalog(result)
        setSelection({
          provider: result.active?.provider || '',
          model: result.active?.model || ''
        })
      })
      .catch(() => {
        if (active) setCatalog({ providers: [], active: {} })
      })
    return () => {
      active = false
    }
  }, [callbacks])

  const providers = catalog?.providers || []
  const selectedProvider =
    providers.find(p => p.slug === selection.provider) || null
  const providerModels = selectedProvider?.models || []

  const status =
    health.state === 'ready'
      ? 'Backend ready'
      : health.state === 'unavailable'
        ? 'Backend unavailable'
        : 'Checking backend'

  const running = run.state === 'running'
  const canRun = !running && prompt.trim().length > 0
  const onRun = () => {
    if (!canRun) return
    // Generation guard: a monotonically increasing run id makes late
    // responses from a cancelled or cleared run inert, like an
    // AbortController signal checked before applying state.
    const runId = Symbol('run')
    setRun({ state: 'running', result: null, runId })
    const body = { prompt: prompt.trim() }
    if (selection.provider && selection.model) {
      body.provider = selection.provider
      body.model = selection.model
    }
    completePrompt(body)
      .then(result => {
        setRun(current =>
          current.runId === runId ? { state: 'success', result } : current
        )
      })
      .catch(error => {
        setRun(current => {
          if (current.runId !== runId) return current
          return {
            state: isRateLimitError(error) ? 'rate_limited' : 'error',
            result: null
          }
        })
      })
  }

  const onCancel = () => {
    if (running) setRun({ state: 'idle', result: null })
  }

  const onClear = () => {
    setPrompt('')
    setRun({ state: 'idle', result: null })
    setSelection({
      provider: catalog?.active?.provider || '',
      model: catalog?.active?.model || ''
    })
  }

  const result = run.state === 'success' ? run.result : null
  // Requested identity is the caller's explicit selection; served identity
  // is the authoritative provider fact. An alias may serve a different
  // model, so both are always labeled and served is never called selected.
  const requestedIdentity =
    result && (result.requested_provider || result.requested_model)
      ? [result.requested_provider, result.requested_model]
          .filter(Boolean)
          .join(' / ')
      : null
  const servedIdentity =
    result && (result.provider || result.model)
      ? [result.provider, result.model].filter(Boolean).join(' / ')
      : null

  return jsxs('div', {
    className: 'flex h-full flex-col gap-3 p-4 text-sm',
    children: [
      jsxs('div', {
        className: 'flex flex-col gap-1',
        children: [
          jsx('div', {
            className: 'font-medium text-(--ui-text-primary)',
            children: 'Model Lab'
          }),
          jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: 'Stateless model testing, separate from Hermes chat.'
          })
        ]
      }),
      jsx('div', {
        className: 'rounded border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
        children: status
      }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          jsx('select', {
            'data-testid': 'provider-select',
            className:
              'flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs text-(--ui-text-primary)',
            value: selection.provider,
            onChange: event => {
              const slug = event.target.value
              const next = providers.find(p => p.slug === slug)
              setSelection({
                provider: slug,
                model:
                  next?.models?.includes(selection.model) && next?.slug === selection.provider
                    ? selection.model
                    : ''
              })
            },
            children: providers.map(p =>
              jsx(
                'option',
                { key: p.slug, value: p.slug, style: THEMED_OPTION_STYLE, children: p.label },
                p.slug
              )
            )
          }),
          jsx('select', {
            'data-testid': 'model-select',
            className:
              'flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-xs text-(--ui-text-primary)',
            value: selection.model,
            onChange: event =>
              setSelection(current => ({ ...current, model: event.target.value })),
            children: providerModels.map(m =>
              jsx('option', { key: m, value: m, style: THEMED_OPTION_STYLE, children: m }, m)
            )
          })
        ]
      }),
      jsx('textarea', {
        className:
          'min-h-24 rounded border border-(--ui-stroke-secondary) bg-transparent px-3 py-2 text-xs text-(--ui-text-primary)',
        placeholder: 'Prompt',
        value: prompt,
        onChange: event => setPrompt(event.target.value)
      }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          jsx('button', {
            type: 'button',
            className:
              'rounded border border-(--ui-stroke-secondary) px-3 py-1 text-xs text-(--ui-text-primary) disabled:opacity-50',
            disabled: !canRun,
            onClick: onRun,
            children: 'Run'
          }),
          running
            ? jsx('button', {
                type: 'button',
                className:
                  'rounded border border-(--ui-stroke-secondary) px-3 py-1 text-xs text-(--ui-text-primary)',
                onClick: onCancel,
                children: 'Cancel'
              })
            : null,
          jsx('button', {
            type: 'button',
            className:
              'rounded border border-(--ui-stroke-secondary) px-3 py-1 text-xs text-(--ui-text-secondary)',
            onClick: onClear,
            children: 'Clear'
          })
        ]
      }),
      running
        ? jsx('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: 'Running'
          })
        : null,
      run.state === 'error'
        ? jsx('div', {
            className:
              'rounded border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: SAFE_ERROR_MESSAGE
          })
        : null,
      run.state === 'rate_limited'
        ? jsx('div', {
            className:
              'rounded border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-secondary)',
            children: RATE_LIMIT_MESSAGE
          })
        : null,
      result
        ? (() => {
            const usage = result.usage || null
            const elapsed = formatElapsed(result.elapsed_ms)
            const cost = usage ? formatCost(usage.cost_usd) : null
            return jsxs('div', {
              className:
                'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) px-3 py-2 text-xs text-(--ui-text-primary)',
              children: [
                jsx('div', {
                  'data-testid': 'result-state',
                  className: 'text-(--ui-text-tertiary)',
                  children: `Complete`
                }),
                elapsed
                  ? jsx('div', {
                      'data-testid': 'result-elapsed',
                      className: 'text-(--ui-text-tertiary)',
                      children: `Time ${elapsed}`
                    })
                  : null,
                servedIdentity
                  ? jsx('div', {
                      'data-testid': 'result-served-identity',
                      className: 'text-(--ui-text-tertiary)',
                      children: `Served ${servedIdentity}`
                    })
                  : null,
                requestedIdentity
                  ? jsx('div', {
                      'data-testid': 'result-requested-identity',
                      className: 'text-(--ui-text-tertiary)',
                      children: `Requested ${requestedIdentity}`
                    })
                  : null,
                jsx('div', {
                  'data-testid': 'result-usage',
                  className: 'text-(--ui-text-tertiary)',
                  children: usage
                    ? `Input tokens ${formatTokens(usage.input_tokens)}, Output tokens ${formatTokens(usage.output_tokens)}, Total tokens ${formatTokens(usage.total_tokens)}`
                    : 'Token usage Unavailable'
                }),
                usage &&
                ((typeof usage.cache_read_tokens === 'number' && usage.cache_read_tokens !== null) ||
                  (typeof usage.cache_write_tokens === 'number' && usage.cache_write_tokens !== null))
                  ? jsx('div', {
                      className: 'text-(--ui-text-tertiary)',
                      children: `Cache read ${formatTokens(usage.cache_read_tokens)}, Cache write ${formatTokens(usage.cache_write_tokens)}`
                    })
                  : null,
                cost
                  ? jsx('div', {
                      className: 'text-(--ui-text-tertiary)',
                      children: `Cost ${cost}`
                    })
                  : null,
                jsx('div', {
                  className: 'whitespace-pre-wrap',
                  children: result.text
                })
              ]
            })
          })()
        : null
    ]
  })
}

export default {
  id: 'hermes-model-lab',
  name: 'Model Lab',
  defaultEnabled: false,
  register(ctx) {
    const callbacks = {
      loadHealth: () => ctx.rest('/health'),
      loadModels: () => ctx.rest('/models'),
      completePrompt: body =>
        ctx.rest('/complete', {
          method: 'POST',
          body,
          timeoutMs: REQUEST_TIMEOUT_MS
        })
    }
    ctx.register({
      id: 'model-lab-pane',
      area: 'panes',
      title: 'Model Lab',
      data: { placement: 'right', width: '340px' },
      render: () => jsx(ModelLabPane, { callbacks })
    })
  }
}
