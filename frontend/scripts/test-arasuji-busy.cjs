// Real ArasujiViewer JSX/handlers with synthetic hooks and API responses only.
// No network, live personas, memory writes, timers, or LLM calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const ts = require('typescript');
const compiled = ts.transpileModule(fs.readFileSync(path.resolve(__dirname, '../src/components/memory/ArasujiViewer.tsx'), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText;
const busyStates = ['running', 'started', 'pending', 'cancelling'];
const job = status => status ? { job_id: 'synthetic-job', status, progress: 0, total: 1, message: 'Synthetic job', entries_created: 0, error: null } : null;
function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
function all(node, predicate) {
    if (Array.isArray(node)) return node.flatMap(child => all(child, predicate));
    if (!node || typeof node !== 'object') return [];
    return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
function button(tree, className) {
    const found = all(tree, node => node.type === 'button' && node.props.className === className);
    assert.equal(found.length, 1, `expected one ${className} button`);
    return found[0];
}
function harness({ latest = null, foldReady = true, failPost = false, repair = true } = {}) {
    const slots = [], effects = [], requests = [], notices = [], intervals = new Map();
    let cursor = 0, dirty = true, tree, nextTimer = 1, pollingJob = null;
    const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
    const hooks = {
        ...React,
        useState(initial) {
            const index = cursor++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => {
                const next = typeof value === 'function' ? value(slots[index]) : value;
                if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
            }];
        },
        useRef(initial) { return slots[cursor++] ??= { current: initial }; },
        useCallback(fn, deps) {
            const index = cursor++;
            if (!same(slots[index]?.deps, deps)) slots[index] = { deps, fn };
            return slots[index].fn;
        },
        useEffect(effect, deps) {
            const index = cursor++;
            if (!same(slots[index]?.deps, deps)) {
                const previous = slots[index]; slots[index] = { deps };
                effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = effect(); });
            }
        },
    };
    const localRequire = name => {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return require(name);
        if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name.endsWith('/ModalOverlay')) return { __esModule: true, default: () => null };
        if (name.endsWith('/ContextVolumeBar')) return { __esModule: true, default: () => null, canDrawContextVolumeBar: () => false };
        if (name === '@/i18n/core') return { t: key => key, getFormatLocale: () => 'en-US' };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            if (options?.method) {
                assert.equal(options.method, 'POST');
                assert.equal(url, '/api/people/synthetic/arasuji/generate');
                requests.push(JSON.parse(options.body));
                return { ok: !failPost, json: async () => failPost ? { detail: 'Synthetic failure' } : { job_id: 'new-job' } };
            }
            let data;
            if (url.endsWith('/generate/latest')) data = await latest;
            else if (url.endsWith('/generate/synthetic-job') || url.endsWith('/generate/new-job')) data = pollingJob;
            else if (url.endsWith('/stats')) data = { max_level: 0, total_count: 0, counts_by_level: {} };
            else if (url.endsWith('/cost-estimate')) data = { unprocessed_messages: repair ? 7 : 0, consolidation_calls: 0, repair_incomplete: repair, estimated_llm_calls: 1, estimated_cost_usd: 0, model_name: 'fake', is_free_tier: true };
            else if (url.endsWith('/context-status')) data = { metabolism: true, presented_chars: 100, target_chars: 50, fold_ready: foldReady, fold_unit_chars: 10 };
            else if (url === '/api/config/developer-mode') data = { enabled: false };
            else if (url === '/api/people/synthetic/arasuji') data = { entries: [] };
            else throw new Error(`Unexpected API ${url}`);
            return { ok: true, json: async () => data };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', 'setInterval', 'clearInterval', 'alert', 'console', compiled)(
        localRequire, module, module.exports,
        fn => { const id = nextTimer++; intervals.set(id, fn); return id; }, id => intervals.delete(id),
        message => notices.push(message), { error: (...args) => notices.push(args) },
    );
    function render() { cursor = 0; dirty = false; tree = module.exports.default({ personaId: 'synthetic' }); effects.splice(0).forEach(fn => fn()); return tree; }
    async function settle() {
        for (let i = 0; i < 25; i++) { if (dirty) render(); await new Promise(setImmediate); if (!dirty) return tree; }
        throw new Error('Viewer did not settle');
    }
    return { settle, requests, notices,
        async poll(status) { pollingJob = job(status); for (const callback of [...intervals.values()]) await callback(); return settle(); },
        failPost(value) { failPost = value; },
    };
}
(async () => {
    let cases = 0;
    for (const status of busyStates) {
        const h = harness({ latest: job(status) });
        const tree = await h.settle();
        assert.equal(button(tree, 'generateBtn').props.disabled, true, `header must block ${status}`);
        assert.equal(button(tree, 'repairBannerBtn').props.disabled, true, `repair banner must block ${status}`);
        assert.equal(h.requests.length, 0); cases++;

        for (const mode of ['generate', 'repair']) {
            // The modal is already open when a delayed latest-job response arrives.
            const lookup = deferred(), h = harness({ latest: lookup.promise });
            let tree = await h.settle();
            button(tree, mode === 'generate' ? 'generateBtn' : 'repairBannerBtn').props.onClick();
            tree = await h.settle();
            assert.equal(button(tree, 'startBtn').props.disabled, false);
            lookup.resolve(job(status)); tree = await h.settle();
            assert.equal(button(tree, 'startBtn').props.disabled, true, `${mode} modal must block ${status}`);
            await button(tree, 'startBtn').props.onClick(); // Defensive handler guard, even without DOM disabled behavior.
            assert.equal(h.requests.length, 0, `${mode} handler must not POST during ${status}`);
            cases++;
        }
    }
    for (const status of [null, 'completed', 'failed', 'cancelled']) {
        for (const mode of ['generate', 'repair']) {
            const h = harness({ latest: job(status) }); let tree = await h.settle();
            const opener = button(tree, mode === 'generate' ? 'generateBtn' : 'repairBannerBtn');
            assert.equal(!!opener.props.disabled, false, `${mode} opens after ${status}`); opener.props.onClick();
            tree = await h.settle(); assert.equal(button(tree, 'startBtn').props.disabled, false);
            await button(tree, 'startBtn').props.onClick(); tree = await h.settle();
            assert.deepEqual(h.requests, [mode === 'generate' ? {} : { mode: 'repair', confirmed_unprocessed_messages: 7 }]);
            assert.equal(button(tree, 'generateBtn').props.disabled, true, 'POST success enters started');
            assert.equal(button(tree, 'repairBannerBtn').props.disabled, true);
            cases++;
        }
    }
    // All in-progress poll states keep both starts blocked, terminal result releases them.
    const polling = harness({ latest: job('running') }); await polling.settle();
    for (const status of ['pending', 'cancelling', 'running', 'completed']) {
        const tree = await polling.poll(status);
        assert.equal(button(tree, 'generateBtn').props.disabled, status !== 'completed');
        assert.equal(button(tree, 'repairBannerBtn').props.disabled, status !== 'completed');
    }
    cases++;
    // Existing fold/readiness rules still prevent empty work.
    const unready = harness({ foldReady: false }); let tree = await unready.settle();
    button(tree, 'generateBtn').props.onClick(); tree = await unready.settle();
    assert.equal(button(tree, 'startBtn').props.disabled, true); cases++;
    // Dismiss and reopen remain usable; failed POST does not leave a busy latch.
    const retry = harness({ failPost: true }); tree = await retry.settle();
    button(tree, 'generateBtn').props.onClick(); tree = await retry.settle();
    button(tree, 'cancelBtn').props.onClick(); tree = await retry.settle();
    assert.equal(all(tree, node => node.props?.className === 'startBtn').length, 0);
    button(tree, 'generateBtn').props.onClick(); tree = await retry.settle();
    await button(tree, 'startBtn').props.onClick(); tree = await retry.settle();
    assert.equal(button(tree, 'generateBtn').props.disabled, false); retry.failPost(false);
    button(tree, 'generateBtn').props.onClick(); tree = await retry.settle();
    await button(tree, 'startBtn').props.onClick(); tree = await retry.settle();
    assert.equal(retry.requests.length, 2); assert.equal(button(tree, 'generateBtn').props.disabled, true); cases++;
    console.log(`${cases} Arasuji busy-state cases passed: four active states, both open modals/handlers, terminal restart, polling, fold readiness, close/reopen, failed POST/retry.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
