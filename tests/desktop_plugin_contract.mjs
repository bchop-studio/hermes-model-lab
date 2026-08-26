import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import vm from 'node:vm'

const root = new URL('../', import.meta.url)
const source = await fs.readFile(new URL('desktop/plugin.js', root), 'utf8')
const context = vm.createContext({ console, Promise, setTimeout, clearTimeout })

function synthetic(exports) {
  const names = Object.keys(exports)
  return new vm.SyntheticModule(
    names,
    function initialize() {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value)
    },
    { context }
  )
}

// Minimal reactive React stand-in: state updates are applied synchronously
// and effects are queued, then flushed explicitly like a React commit phase.
function createReactHarness() {
  let states = []
  let index = 0
  let effectQueue = []
  const react = {
    useEffect(effect) {
      effectQueue.push(effect)
    },
    useState(initial) {
      const slot = index++
      if (!(slot in states)) states[slot] = initial
      return [
        states[slot],
        value => {
          states[slot] = typeof value === 'function' ? value(states[slot]) : value
        }
      ]
    }
  }
  return {
    module: react,
    begin() {
      index = 0
      effectQueue = []
    },
    flushEffects() {
      for (const effect of effectQueue) effect()
    },
    reset() {
      states = []
      index = 0
      effectQueue = []
    }
  }
}

const reactHarness = createReactHarness()
const react = synthetic(reactHarness.module)
const jsxRuntime = synthetic({
  jsx(type, props) {
    return { type, props }
  },
  jsxs(type, props) {
    return { type, props }
  }
})

const module = new vm.SourceTextModule(source, {
  context,
  identifier: 'desktop/plugin.js'
})
await module.link(async specifier => {
  if (specifier === 'react') return react
  if (specifier === 'react/jsx-runtime') return jsxRuntime
  throw new Error(`Unexpected import: ${specifier}`)
})
await module.evaluate()

const plugin = module.namespace.default
const contributions = []
const restCalls = []
let pendingRest = null
plugin.register({
  register(contribution) {
    contributions.push(contribution)
    return () => {}
  },
  rest(path, options) {
    restCalls.push({ path, options })
    if (path === '/health') {
      return Promise.resolve({ ok: true, plugin: 'hermes-model-lab', version: '0.1.0' })
    }
    return new Promise((resolve, reject) => {
      pendingRest = { resolve, reject }
    })
  }
})

assert.equal(plugin.id, 'hermes-model-lab')
assert.equal(plugin.name, 'Model Lab')
assert.equal(plugin.defaultEnabled, false)
assert.equal(contributions.length, 1)

const pane = contributions[0]
assert.equal(pane.id, 'model-lab-pane')
assert.equal(pane.area, 'panes')
assert.equal(pane.data.placement, 'right')
assert.equal(pane.data.width, '340px')

// Walk the rendered element tree.
function* walk(node) {
  if (!node || typeof node !== 'object') return
  yield node
  const children = node.props?.children
  for (const child of Array.isArray(children) ? children : [children]) {
    yield* walk(child)
  }
}

function renderPane() {
  reactHarness.begin()
  const rootElement = pane.render()
  return rootElement.type(rootElement.props)
}

function findByType(tree, type) {
  return [...walk(tree)].filter(node => node.type === type)
}

function buttonByText(tree, text) {
  return findByType(tree, 'button').find(node => {
    const children = node.props.children
    const flat = Array.isArray(children) ? children : [children]
    return flat.includes(text)
  })
}

function flush(rounds = 12) {
  // One zero-delay timer turn plus microtask rounds, so cross-context
  // promise reactions queued by the vm harness are fully drained.
  let promise = new Promise(resolve => setTimeout(resolve, 0))
  for (let i = 0; i < rounds; i += 1) promise = promise.then(() => {})
  return promise
}

function textOf(tree) {
  let out = ''
  for (const node of walk(tree)) {
    const children = node.props?.children
    for (const child of Array.isArray(children) ? children : [children]) {
      if (typeof child === 'string') out += `${child}\n`
    }
  }
  return out
}

// Mount: health and model-options fire once each, prompt form exists,
// run starts blocked.
reactHarness.reset()
let tree = renderPane()
reactHarness.flushEffects()
await flush()
assert.equal(restCalls.length, 2)
assert.deepEqual(
  restCalls.map(call => call.path).sort(),
  ['/health', '/models'],
  'mount must fetch exactly /health and /models'
)

const textareas = findByType(tree, 'textarea')
assert.equal(textareas.length, 1, 'expected one prompt textarea')
const runButton = buttonByText(tree, 'Run')
assert.ok(runButton, 'expected a Run button')
assert.equal(runButton.props.disabled, true, 'Run starts disabled with an empty prompt')

// Empty prompts are rejected in the UI: no completion request is made.
runButton.props.onClick?.()
await flush()
assert.equal(
  restCalls.filter(call => call.path === '/complete').length,
  0,
  'empty prompt must not call /complete'
)

// Type a prompt: Run becomes enabled.
textareas[0].props.onChange({ target: { value: 'Reply exactly MODEL_LAB_T004_OK' } })
tree = renderPane()
assert.equal(buttonByText(tree, 'Run').props.disabled, false)

// Run: one POST through ctx.rest with a bounded timeout, loading state shows,
// and Cancel is available while the request is in flight.
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
const completeCalls = restCalls.filter(call => call.path === '/complete')
assert.equal(completeCalls.length, 1, 'expected exactly one /complete call')
const request = completeCalls[0]
assert.equal(request.options.method, 'POST')
assert.deepEqual(Object.keys(request.options.body), ['prompt'])
assert.equal(request.options.body.prompt, 'Reply exactly MODEL_LAB_T004_OK')
assert.equal(request.options.timeoutMs, 70000)
assert.equal(textOf(tree).includes('Running'), true, 'loading state must be visible')
const cancelButton = buttonByText(tree, 'Cancel')
assert.ok(cancelButton, 'Cancel must be available during a run')
assert.equal(
  buttonByText(tree, 'Run').props.disabled,
  true,
  'only one active request: Run stays disabled while running'
)

// Cancel: waiting stops, the late response is discarded, no repaint with results.
cancelButton.props.onClick()
tree = renderPane()
assert.equal(textOf(tree).includes('Running'), false, 'cancel must stop the loading state')
pendingRest.resolve({
  text: 'LATE_RESPONSE',
  provider: 'test-provider',
  model: 'test-model'
})
await flush()
tree = renderPane()
assert.equal(textOf(tree).includes('LATE_RESPONSE'), false, 'late response after cancel must be discarded')
assert.equal(textOf(tree).includes('Something went wrong'), false, 'cancel must not surface an error')

// Second run succeeds: response text plus provider/model render.
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
assert.equal(restCalls.filter(call => call.path === '/complete').length, 2)
pendingRest.resolve({
  text: 'MODEL_LAB_T004_OK',
  provider: 'test-provider',
  model: 'test-model'
})
await flush()
tree = renderPane()
const successText = textOf(tree)
assert.equal(successText.includes('MODEL_LAB_T004_OK'), true, 'response text must render')
assert.equal(successText.includes('test-provider'), true, 'provider must render when returned')
assert.equal(successText.includes('test-model'), true, 'model must render when returned')
assert.equal(successText.includes('Running'), false, 'loading state must clear on success')

// Clear: prompt, result, and error all reset.
buttonByText(tree, 'Clear').props.onClick()
tree = renderPane()
assert.equal(findByType(tree, 'textarea')[0].props.value, '', 'Clear must empty the prompt')
assert.equal(textOf(tree).includes('MODEL_LAB_T004_OK'), false, 'Clear must remove the result')
assert.equal(buttonByText(tree, 'Run').props.disabled, true)

// Failure: the pane shows one fixed safe message, never the backend detail.
findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'boom' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
pendingRest.reject(new Error('502 Model request failed. SECRET_PROVIDER_DETAIL'))
await flush()
tree = renderPane()
const errorText = textOf(tree)
assert.equal(errorText.includes('Something went wrong'), true, 'a fixed safe error state must show')
assert.equal(errorText.includes('SECRET_PROVIDER_DETAIL'), false, 'backend detail must never render')
assert.equal(errorText.includes('Running'), false, 'loading state must clear on failure')

// Clear also resets the error state.
buttonByText(tree, 'Clear').props.onClick()
tree = renderPane()
assert.equal(textOf(tree).includes('Something went wrong'), false, 'Clear must remove the error state')

// Clear during a live run is also a cancellation boundary: any late result is inert.
findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'clear while running' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
assert.equal(textOf(tree).includes('Running'), true)
buttonByText(tree, 'Clear').props.onClick()
tree = renderPane()
assert.equal(findByType(tree, 'textarea')[0].props.value, '')
assert.equal(textOf(tree).includes('Running'), false)
pendingRest.resolve({
  text: 'LATE_AFTER_CLEAR',
  provider: 'test-provider',
  model: 'test-model'
})
await flush()
tree = renderPane()
assert.equal(
  textOf(tree).includes('LATE_AFTER_CLEAR'),
  false,
  'late response after Clear must be discarded'
)

// Safety bans: no host bridge, no credentials, no raw HTML injection.
assert.equal(source.includes('host.request'), false)
assert.equal(source.includes('API_KEY'), false)
assert.equal(source.includes('dangerouslySetInnerHTML'), false)

console.log(
  'DESKTOP_PLUGIN_CONTRACT_PASS pane=right defaultEnabled=false health=/health ' +
    'runner=run-cancel-clear timeoutMs=70000 errors=fixed-safe'
)

// ─── T005: allowed provider and model selection ──────────────────────────
//
// The pane loads /models once at mount, renders provider/model selects from
// the sanitized projection only, sends selection fields with /complete, and
// resets selection on Clear.

function renderFresh() {
  reactHarness.reset()
  const t = renderPane()
  reactHarness.flushEffects()
  return t
}

function selectByPlaceholder(tree, placeholder) {
  return findByType(tree, 'select').find(
    node => node.props['data-testid'] === placeholder
  )
}

reactHarness.reset()
tree = renderFresh()
await flush()

// /models is fetched at mount alongside health.
assert.equal(restCalls.some(call => call.path === '/models'), true,
  'pane must fetch /models at mount')

const modelsPayload = {
  active: { provider: 'nous', model: 'stealth/ox-alpha' },
  providers: [
    { slug: 'nous', label: 'Nous Lab', models: ['Hermes-4-405B', 'stealth/ox-alpha'] },
    { slug: 'openrouter', label: 'OpenRouter', models: ['openai/gpt-4o-mini'] }
  ]
}

// Simulate the resolved /models response by re-rendering with a stub.
// Find how the pane consumes it: it must call ctx.rest('/models').
const modelsCall = restCalls.find(call => call.path === '/models')
assert.ok(modelsCall, '/models must be requested through ctx.rest')

// Drive the pane's promise for /models through the same harness used for
// completions: resolve it via pendingRest if the pane queued one, else the
// pane may have consumed it synchronously. Resolve whichever is pending.
if (pendingRest) {
  pendingRest.resolve(modelsPayload)
  await flush()
}
tree = renderPane()

// Provider select exists and lists configured providers only.
const providerSelect = selectByPlaceholder(tree, 'provider-select')
assert.ok(providerSelect, 'provider select must exist')
const providerOptions = (providerSelect.props.children || []).filter(Boolean)
const optionTexts = providerOptions.map(node => node.props.value)
assert.deepEqual(optionTexts.sort(), ['nous', 'openrouter'])
for (const option of providerOptions) {
  assert.equal(
    option.props.style?.backgroundColor,
    'var(--ui-bg-elevated)',
    'provider options must inherit the active Hermes popup surface'
  )
  assert.equal(
    option.props.style?.color,
    'var(--ui-text-primary)',
    'provider options must use readable active-theme text'
  )
}

// Model select defaults to the active model of the default provider and
// never shows host-only fields anywhere in the tree.
const treeText = JSON.stringify(tree)
assert.equal(treeText.includes('key_env'), false)
assert.equal(treeText.includes('NOUS_API_KEY'), false)
assert.equal(treeText.includes('base_url'), false)

// Choosing a provider narrows its models; choosing a model then running
// sends both fields with /complete.
providerSelect.props.onChange({ target: { value: 'openrouter' } })
tree = renderPane()
const modelSelectAfter = selectByPlaceholder(tree, 'model-select')
assert.ok(modelSelectAfter, 'model select must exist')
const modelOptions = (modelSelectAfter.props.children || [])
  .filter(Boolean)
assert.deepEqual(modelOptions.map(node => node.props.value), ['openai/gpt-4o-mini'])
for (const option of modelOptions) {
  assert.equal(
    option.props.style?.backgroundColor,
    'var(--ui-bg-elevated)',
    'model options must inherit the active Hermes popup surface'
  )
  assert.equal(
    option.props.style?.color,
    'var(--ui-text-primary)',
    'model options must use readable active-theme text'
  )
}

modelSelectAfter.props.onChange({ target: { value: 'openai/gpt-4o-mini' } })
findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'T005_SELECT' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
const selCall = restCalls.filter(call => call.path === '/complete').pop()
assert.deepEqual(
  Object.keys(selCall.options.body).sort(),
  ['model', 'prompt', 'provider'],
  'selection rides along with the completion request'
)
assert.equal(selCall.options.body.provider, 'openrouter')
assert.equal(selCall.options.body.model, 'openai/gpt-4o-mini')
pendingRest.resolve({ text: 'SEL_OK', provider: 'openrouter', model: 'openai/gpt-4o-mini' })
await flush()
tree = renderPane()
assert.equal(textOf(tree).includes('SEL_OK'), true)

// Clear also resets the selection back to the active default.
buttonByText(tree, 'Clear').props.onClick()
tree = renderPane()
const resetProvider = selectByPlaceholder(tree, 'provider-select')
assert.equal(resetProvider.props.value, 'nous', 'Clear must reset provider to active')
const resetModel = selectByPlaceholder(tree, 'model-select')
assert.equal(resetModel.props.value, 'stealth/ox-alpha', 'Clear must reset model to active')

console.log('DESKTOP_PLUGIN_CONTRACT_T005_PASS selection=provider-model sanitized-only')

// ─── T006: response facts without guessing ───────────────────────────────
//
// The success card shows completion state, provider, model, elapsed time,
// and token facts when present, with clear Unavailable labels when absent.
// Failure paths keep fixed safe messages; rate limits show their own state.

reactHarness.reset()
tree = renderFresh()
await flush()

findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'T006_FACTS' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
pendingRest.resolve({
  state: 'complete',
  text: 'T006_OK',
  provider: 'nous',
  model: 'stealth/ox-alpha',
  elapsed_ms: 1234,
  usage: {
    input_tokens: 11,
    output_tokens: 7,
    total_tokens: 18,
    cache_read_tokens: 4,
    cache_write_tokens: 2,
    cost_usd: null
  }
})
await flush()
tree = renderPane()
const factsText = textOf(tree)
assert.equal(factsText.includes('T006_OK'), true, 'response text must render')
assert.equal(factsText.includes('Complete'), true, 'completion state must be labeled')
assert.equal(factsText.includes('nous'), true)
assert.equal(factsText.includes('stealth/ox-alpha'), true)
assert.equal(factsText.includes('1.2s'), true, 'elapsed time must render in seconds')
assert.equal(factsText.includes('11'), true, 'input tokens must render')
assert.equal(factsText.includes('7'), true, 'output tokens must render')
assert.equal(factsText.includes('18'), true, 'total tokens must render')
assert.equal(factsText.includes('4'), true, 'cache read tokens must render')
assert.equal(factsText.toLowerCase().includes('unavailable'), false,
  'present facts must not be marked unavailable')

// Missing usage: token rows show Unavailable instead of fake zeros.
buttonByText(tree, 'Run') && findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'T006_NOUSAGE' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
pendingRest.resolve({
  state: 'complete',
  text: 'T006_NOUSAGE_OK',
  provider: 'nous',
  model: 'stealth/ox-alpha',
  elapsed_ms: 900,
  usage: null
})
await flush()
tree = renderPane()
const missingUsageText = textOf(tree)
assert.equal(missingUsageText.includes('T006_NOUSAGE_OK'), true)
assert.notEqual(
  /tokens?\s*:?\s*0(?!\d)/i.test(missingUsageText) && missingUsageText.toLowerCase().includes('input tokens'),
  true,
  'missing usage must never render as zero tokens'
)
assert.equal(missingUsageText.toLowerCase().includes('unavailable'), true,
  'missing usage must be labeled Unavailable')

// Rate limit failure: a distinct fixed safe message, never raw detail.
findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'T006_RL' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
const rlErr = new Error('429 rate limited SECRET_QUOTA_DETAIL')
rlErr.statusCode = 429
pendingRest.reject(rlErr)
await flush()
tree = renderPane()
const rlText = textOf(tree)
assert.equal(rlText.includes('Rate limited'), true, 'rate limit must show its own fixed state')
assert.equal(rlText.includes('SECRET_QUOTA_DETAIL'), false, 'raw detail must never render')

// Generic failure keeps the original fixed safe message.
findByType(tree, 'textarea')[0].props.onChange({ target: { value: 'T006_FAIL' } })
tree = renderPane()
buttonByText(tree, 'Run').props.onClick()
tree = renderPane()
pendingRest.reject(new Error('502 Model request failed. SECRET_PROVIDER_DETAIL'))
await flush()
tree = renderPane()
const failText = textOf(tree)
assert.equal(failText.includes('Something went wrong'), true)
assert.equal(failText.includes('SECRET_PROVIDER_DETAIL'), false)

// Clear removes the facts card too.
buttonByText(tree, 'Clear').props.onClick()
tree = renderPane()
assert.equal(textOf(tree).includes('Unavailable'), false, 'Clear must remove the facts card')

console.log('DESKTOP_PLUGIN_CONTRACT_T006_PASS facts=state-provider-model-elapsed-usage unavailable-labeled errors=fixed-safe')

// ─── Alias attribution: Requested vs Served identity labels ──────────────
//
// The result card must label requested identity and served identity
// explicitly. A host alias may serve a different model than selected, so
// served identity is never called "selected".

function runAliasCase({ requested, resolved }) {
  reactHarness.reset()
  const tree0 = renderFresh()
  // Seed the catalog + default selection like the mount /models response.
  findByType(tree0, 'textarea')[0].props.onChange({ target: { value: 'ALIAS_CASE' } })
  let t = renderPane()
  buttonByText(t, 'Run').props.onClick()
  pendingRest.resolve(resolved)
  return flush().then(() => renderPane())
}

// Alias case: selection was nous/stealth/ox-alpha but the host served a
// different model through the alias.
{
  const aliasTree = await runAliasCase({
    resolved: {
      state: 'complete',
      text: 'T_ALIAS_OK',
      requested_provider: 'nous',
      requested_model: 'stealth/ox-alpha',
      provider: 'nous',
      model: 'tencent/hy3:free',
      elapsed_ms: 100,
      usage: null
    }
  })
  const aliasText = textOf(aliasTree)
  assert.equal(aliasText.includes('Requested nous / stealth/ox-alpha'), true,
    'requested identity must be labeled explicitly')
  assert.equal(aliasText.includes('Served nous / tencent/hy3:free'), true,
    'served identity must be labeled explicitly')
  assert.equal(aliasText.includes('tencent/hy3:free'), true)
}

// Identical case: requested equals served. Both identities still render,
// each with its own label.
{
  const sameTree = await runAliasCase({
    resolved: {
      state: 'complete',
      text: 'T_SAME_OK',
      requested_provider: 'nous',
      requested_model: 'stealth/ox-alpha',
      provider: 'nous',
      model: 'stealth/ox-alpha',
      elapsed_ms: 100,
      usage: null
    }
  })
  const sameText = textOf(sameTree)
  assert.equal(sameText.includes('Requested nous / stealth/ox-alpha'), true,
    'identical case still labels requested identity')
  assert.equal(sameText.includes('Served nous / stealth/ox-alpha'), true,
    'identical case still labels served identity')
}

console.log('DESKTOP_PLUGIN_CONTRACT_ALIAS_PASS requested-and-served=labeled')
