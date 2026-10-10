const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/lib/modelPricing.ts'), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const mod = { exports: {} };
new Function('exports', compiled)(mod.exports);
const rate = mod.exports.outputRateForInput;
for (const [model, threshold, base, long] of [
    ['claude-haiku-5.5', 100000, 0.5, 2.5], ['grok-4.7', 200000, 6, 12],
]) {
    const pricing = JSON.parse(fs.readFileSync(path.join(__dirname, '../../builtin_data/models', `${model}.json`))).pricing;
    assert.equal(rate(pricing, threshold), base);
    assert.equal(rate(pricing, threshold + 1), long);
}
assert.equal(rate({ output_per_1m_tokens: 5, long_context_threshold_tokens: 10 }, 11), 5);
assert.equal(rate({ output_per_1m_tokens: 5 }, 999999), 5);
assert.equal(rate(undefined, 1), undefined);
assert.equal(rate({ long_context_threshold_tokens: true, long_context_output_per_1m_tokens: 10, output_per_1m_tokens: 5 }, 11), 5);
const modal = fs.readFileSync(path.join(__dirname, '../src/components/ContextPreviewModal.tsx'), 'utf8');
assert.match(modal, /outputRateForInput\(persona\.pricing, persona\.total_input_tokens\)/);
console.log('model pricing boundary tests passed');

// Render the actual price consumers using synthetic API snapshots. Effects are
// disabled, so there is no browser, backend, production persona, or network.
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const messages = require('../src/i18n/messages.json');
const sourceRoot = path.resolve(__dirname, '../src');
function loadPricingComponent(file, react, apiFetch = () => { throw new Error('Network forbidden'); }, env = {}) {
    const cache = new Map();
    function load(filename) {
        if (cache.has(filename)) return cache.get(filename);
        const module = { exports: {} };
        const output = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
        }).outputText;
        const localRequire = name => {
            if (name === 'react') return react;
            if (name === '@/i18n/core') return {
                t: (key, params = {}) => messages[key].ja.replace(/\{(\w+)\}/g, (match, key) => params[key] ?? match),
                getFormatLocale: () => 'ja-JP',
            };
            if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
            if (name === '@/i18n/api') return { apiFetch };
            if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
            if (name.includes('ModelEditorModal') || name.includes('ContextVolumeBar')) return { __esModule: true, default: () => null };
            if (name.endsWith('ModalOverlay')) return { __esModule: true, default: ({ children }) => children };
            if (name.startsWith('@/lib/')) return load(path.join(sourceRoot, `${name.slice(2)}.ts`));
            return require(name);
        };
        new Function('require', 'module', 'exports', 'window', 'document', 'setTimeout', 'clearTimeout', output)(
            localRequire, module, module.exports, env.window, env.document, env.setTimeout ?? setTimeout, env.clearTimeout ?? clearTimeout,
        );
        cache.set(filename, module.exports);
        return module.exports;
    }
    return load(path.join(sourceRoot, 'components', file)).default;
}
function renderPricing(file, props, models = [], selected = '') {
    let stateIndex = 0;
    const react = {
        ...React,
        useState: initial => {
            const index = stateIndex++;
            const value = file === 'ChatOptions.tsx' && index < 2
                ? (index === 0 ? models : selected)
                : typeof initial === 'function' ? initial() : initial;
            return [value, () => {}];
        },
        useEffect: () => {}, useMemo: fn => fn(), useRef: value => ({ current: value }),
    };
    const Component = loadPricingComponent(file, react);
    return renderToStaticMarkup(React.createElement(Component, props));
}
const note = '2026-10-10 confirmed. Discount ends 2026-12-31 10:00 UTC. <App estimate>, provider timezone unspecified.';
const chatProps = { isOpen: true, onClose: () => {}, currentModel: 'dated', onModelChange: () => {} };
for (const [input, output] of [[0.75, 4.5], [1.5, 9]]) {
    const dated = { id: 'dated', name: 'Dated model', input_price: input, output_price: output, currency: 'USD', pricing_note: note };
    const plain = { id: 'plain', name: 'Plain model', input_price: 0, output_price: 0, currency: 'USD' };
    const chat = renderPricing('ChatOptions.tsx', chatProps, [dated, plain], 'dated');
    assert.ok(chat.includes(`$${input.toFixed(3)}`));
    assert.ok(chat.includes(`$${output.toFixed(3)}`));
    assert.ok(chat.includes('&lt;App estimate&gt;'));
    assert.ok(chat.includes('2026-12-31 10:00 UTC'));
    const switched = renderPricing('ChatOptions.tsx', chatProps, [dated, plain], 'plain');
    assert.ok(switched.includes('$0'));
    assert.ok(!switched.includes('App estimate'), 'the previous model note must disappear after selection changes');
    assert.equal(renderPricing('ChatOptions.tsx', { ...chatProps, isOpen: false }, [dated], 'dated'), '');

    const persona = {
        persona_id: 'synthetic', persona_name: 'Synthetic', model: 'dated',
        model_display_name: 'Dated model', provider: 'synthetic', context_length: 100000,
        sections: [], total_input_tokens: 1000, estimated_cost_best_usd: 0.01,
        estimated_cost_worst_usd: 0.02, cache_enabled: false, cache_ttl: null,
        cache_type: null, pricing: { currency: 'USD', output_per_1m_tokens: output, pricing_note: note }, messages: [],
    };
    const previewProps = { isOpen: true, isLoading: false, onClose: () => {}, data: { personas: [persona] } };
    const preview = renderPricing('ContextPreviewModal.tsx', previewProps);
    assert.ok(preview.includes(`$${output.toFixed(3)}`));
    assert.ok(preview.includes('&lt;App estimate&gt;'));
    assert.ok(preview.includes('2026-12-31 10:00 UTC'));
    const withoutNote = structuredClone(persona);
    delete withoutNote.pricing.pricing_note;
    assert.ok(!renderPricing('ContextPreviewModal.tsx', { ...previewProps, data: { personas: [withoutNote] } }).includes('App estimate'));
    assert.equal(renderPricing('ContextPreviewModal.tsx', { ...previewProps, isOpen: false }), '');
}
console.log('Effective pricing and optional notes render in chat options and context previews; model changes and dismissal clear them.');

const response = (data, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => data });
const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
};
const snapshot = input => [{ id: 'dated', name: 'Dated model', input_price: input, output_price: input * 6, currency: 'USD', pricing_note: note }];
function mountPriceRefresh(initialModelResponse) {
    const states = [], refs = [], effects = [], requests = [], queued = [];
    const timers = new Map();
    let nextTimer = 0, pending = [], dirty = false, html = '', models = snapshot(0.75), changedModels = 0;
    if (initialModelResponse) queued.push(initialModelResponse);
    const changed = (a, b) => !a || b.some((value, index) => !Object.is(value, a[index]));
    const react = {
        ...React,
        useState(initial) {
            const index = react.s++;
            if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
            return [states[index], value => {
                const next = typeof value === 'function' ? value(states[index]) : value;
                if (!Object.is(next, states[index])) { states[index] = next; dirty = true; }
            }];
        },
        useRef(initial) { return refs[react.r++] ??= { current: initial }; },
        useMemo: fn => fn(),
        useEffect(fn, deps) { pending.push({ index: react.e++, fn, deps }); },
    };
    const document = new EventTarget();
    document.visibilityState = 'visible';
    const env = {
        document,
        setTimeout: (fn, ms) => { timers.set(++nextTimer, { fn, ms, kind: 'timeout' }); return nextTimer; },
        clearTimeout: id => timers.delete(id),
    };
    env.window = {
        ...env,
        setInterval: (fn, ms) => { timers.set(++nextTimer, { fn, ms, kind: 'interval' }); return nextTimer; },
        clearInterval: id => timers.delete(id),
    };
    const apiFetch = async (url, options) => {
        requests.push({ url, signal: options?.signal });
        if (url === '/api/config/models') return queued.length ? queued.shift() : response(models);
        if (url === '/api/config/config') return response({ current_model: 'dated', parameters: {}, current_values: { temperature: 0.8 } });
        if (url === '/api/config/cache') return response({ supported: false });
        if (url === '/api/config/favorite-models') return response([]);
        throw new Error(`Unexpected request: ${url}`);
    };
    const Component = loadPricingComponent('ChatOptions.tsx', react, apiFetch, env);
    let props = { ...chatProps, onModelChange: () => { changedModels++; } };
    const render = () => {
        react.s = react.r = react.e = 0;
        dirty = false;
        pending = [];
        html = renderToStaticMarkup(Component(props));
        for (const { index, fn, deps } of pending) {
            if (!effects[index] || changed(effects[index].deps, deps)) {
                effects[index]?.cleanup?.();
                effects[index] = { deps, cleanup: fn() };
            }
        }
    };
    render();
    return {
        requests, queued, timers,
        get html() { return html; },
        get changedModels() { return changedModels; },
        setSnapshot: input => { models = snapshot(input); },
        update: next => { props = { ...props, ...next }; render(); },
        poll: () => { for (const timer of [...timers.values()]) if (timer.kind === 'interval' && timer.ms === 60000) timer.fn(); },
        visibility: value => { document.visibilityState = value; document.dispatchEvent(new Event('visibilitychange')); },
        async settle() { for (let i = 0; i < 8; i++) { await new Promise(done => setImmediate(done)); if (dirty) render(); } },
        unmount: () => { for (const effect of effects) effect?.cleanup?.(); },
    };
}
(async () => {
    const view = mountPriceRefresh();
    await view.settle();
    assert.ok(view.html.includes('$0.750'));
    assert.equal(view.changedModels, 1);
    view.setSnapshot(1.5); // Server time has crossed the exclusive promotion end.
    view.poll();
    await view.settle();
    assert.ok(view.html.includes('$1.500'));
    assert.equal(view.changedModels, 1, 'pricing refresh must not change the selected model');
    assert.ok(view.requests.slice(4).every(request => request.url === '/api/config/models'));

    view.visibility('hidden');
    const hiddenCount = view.requests.length;
    view.poll();
    await view.settle();
    assert.equal(view.requests.length, hiddenCount, 'hidden tabs must not poll');
    view.setSnapshot(2);
    view.visibility('visible');
    await view.settle();
    assert.ok(view.html.includes('$2.000'), 'tab resume must immediately refresh prices');

    view.queued.push(response(null, false));
    view.poll();
    await view.settle();
    assert.ok(view.html.includes('$2.000'), 'failed requests keep the latest known snapshot');
    view.setSnapshot(3);
    view.poll();
    await view.settle();
    assert.ok(view.html.includes('$3.000'), 'the next tick retries after failure');

    const slow = deferred();
    view.queued.push(slow.promise);
    view.poll();
    const slowRequest = view.requests.at(-1);
    view.setSnapshot(4);
    view.visibility('visible');
    await view.settle();
    slow.resolve(response(snapshot(0.75))); // Deliberately ignore abort in the fake transport.
    await view.settle();
    assert.equal(slowRequest.signal.aborted, true);
    assert.ok(view.html.includes('$4.000'), 'late responses must not restore an expired price');

    const closing = deferred();
    view.queued.push(closing.promise);
    view.poll();
    const closingRequest = view.requests.at(-1);
    view.update({ isOpen: false });
    assert.equal(view.html, '');
    assert.equal(closingRequest.signal.aborted, true);
    const closedCount = view.requests.length;
    view.poll();
    view.visibility('visible');
    assert.equal(view.requests.length, closedCount, 'closing removes polling and visibility listeners');
    view.setSnapshot(5);
    view.update({ isOpen: true });
    await view.settle();
    closing.resolve(response(snapshot(0.75)));
    await view.settle();
    assert.ok(view.html.includes('$5.000'), 'a response from a previous opening must not overwrite current prices');
    view.unmount();
    assert.equal(view.timers.size, 0);

    // The original multi-request fetch can also finish after a newer price read.
    const initial = deferred();
    const opening = mountPriceRefresh(initial.promise);
    opening.setSnapshot(1.5);
    opening.visibility('visible');
    await opening.settle();
    initial.resolve(response(snapshot(0.75)));
    await opening.settle();
    assert.ok(opening.html.includes('$1.500'), 'the original opening fetch shares the stale-response guard');
    opening.unmount();
    assert.equal(opening.timers.size, 0);
    console.log('Open chat pricing refresh: boundary tick, tab resume, retry, stale reads, close/reopen, and cleanup passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
