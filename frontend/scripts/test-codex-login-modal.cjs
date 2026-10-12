// CodexLoginModal の状態応答の順序規則を、ブラウザなしで確かめる。
// - 同じ試行の結果 (success / error) は確定で、後から届いた途中経過や success 後の error で覆らない
// - 待っている試行が別の試行に入れ替わったら、別の試行の中身を取り込まずにやり直しを案内する
// React の hooks を最小限に模した器で本物のコンポーネントを回し、apiFetch は偽物を渡す
// (バックエンド・OpenAI には一切つながない)。docs/intent/codex_subscription_auth.md §8
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const messages = require('../src/i18n/messages.json');

const file = path.resolve(__dirname, '../src/components/settings/CodexLoginModal.tsx');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (key) => messages[`components.settings.CodexLoginModal.${key}`].ja;
const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#x27;');

function mount(props, apiFetch) {
    const states = [];
    const refs = [];
    const callbacks = [];
    const effects = [];
    let pending = [];
    let dirty = false;
    let html = '';
    const sameDeps = (a, b) => a && b && a.length === b.length && a.every((d, k) => Object.is(d, b[k]));
    const shim = {
        ...React,
        useState(initial) {
            const i = shim._s++;
            if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial;
            return [states[i], (v) => {
                const next = typeof v === 'function' ? v(states[i]) : v;
                if (next !== states[i]) { states[i] = next; dirty = true; }
            }];
        },
        useRef(initial) {
            const i = shim._r++;
            if (!(i in refs)) refs[i] = { current: initial };
            return refs[i];
        },
        useCallback(fn, deps) {
            const i = shim._c++;
            const prev = callbacks[i];
            if (prev && sameDeps(prev.deps, deps)) return prev.fn;
            callbacks[i] = { fn, deps };
            return fn;
        },
        useEffect(fn, deps) { pending.push({ i: shim._e++, fn, deps }); },
    };
    const source = fs.readFileSync(file, 'utf8')
        .replace(/const POLL_INTERVAL_MS = [\d_]+;/, 'const POLL_INTERVAL_MS = 15;');
    const output = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;
    const mod = { exports: {} };
    const localRequire = (name) => {
        if (name === 'react') return shim;
        if (name === '@/i18n/core') return { t: (key, vars) => messages[key].ja.replace(/\{(\w+)\}/g, (_, k) => vars?.[k] ?? '') };
        if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
        if (name === '@/i18n/api') return { apiFetch };
        if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === '../common/ModalOverlay') return { __esModule: true, default: ({ children }) => children };
        return require(name);
    };
    new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
    const Component = mod.exports.default;

    function runEffects(list) {
        for (const { i, fn, deps } of list) {
            const prev = effects[i];
            const changed = !prev || !deps || !sameDeps(prev.deps, deps);
            if (!changed) continue;
            if (prev?.cleanup) prev.cleanup();
            effects[i] = { deps: deps || [], cleanup: fn() || null };
        }
    }
    function render() {
        shim._s = 0; shim._r = 0; shim._c = 0; shim._e = 0;
        pending = [];
        dirty = false;
        html = renderToStaticMarkup(Component(props));
        return pending;
    }
    runEffects(render());
    return {
        get html() { return html; },
        async settle(ms = 20) {
            await sleep(ms);
            for (let n = 0; dirty && n < 20; n++) runEffects(render());
        },
        unmount() { for (const e of effects) e?.cleanup?.(); },
    };
}

/** start は attempt 1 で waiting を返し、status は手で解決するまで保留する偽の API。 */
function fakeApi() {
    const held = [];  // 保留中の status 応答 (解決関数)
    const apiFetch = async (url) => {
        if (url.endsWith('/siwc/start')) {
            return {
                ok: true, status: 200,
                json: async () => ({
                    state: 'waiting', attempt_id: 1, lease_id: 'L1',
                    auth_url: 'https://auth.example/one', redirect_uri: 'http://127.0.0.1:1455/auth/callback',
                }),
            };
        }
        if (url.endsWith('/siwc/status')) {
            return new Promise((resolve) => held.push((body) => resolve({ ok: true, status: 200, json: async () => body })));
        }
        if (url.endsWith('/cancel')) return { ok: true, status: 200, json: async () => ({ ok: true }) };
        throw new Error(`unexpected request ${url}`);
    };
    return { apiFetch, held };
}

async function waitForPolls(view, held, count) {
    for (let n = 0; n < 50 && held.length < count; n++) await view.settle(10);
    assert.ok(held.length >= count, `status polls did not overlap (${held.length})`);
}

async function successIsFinal() {
    let successCalls = 0;
    const { apiFetch, held } = fakeApi();
    const view = mount({ isOpen: true, onClose: () => {}, onSuccess: () => { successCalls++; } }, apiFetch);
    await view.settle();
    assert.ok(view.html.includes('https://auth.example/one'), view.html);
    await waitForPolls(view, held, 3);
    held[1]({ state: 'success', attempt_id: 1, account_id: 'acct' });
    await view.settle();
    assert.ok(view.html.includes(text('text006')), view.html);
    // 先に出ていた poll が遅れて waiting を返しても、success は覆らない
    held[0]({ state: 'waiting', attempt_id: 1, auth_url: 'https://auth.example/one' });
    await view.settle();
    assert.ok(view.html.includes(text('text006')), 'a late waiting overwrote success');
    // success の後の error も無視する
    held[2]({ state: 'error', attempt_id: 1, error: 'late failure' });
    await view.settle();
    assert.ok(view.html.includes(text('text006')), 'a late error overwrote success');
    assert.ok(!view.html.includes('late failure'));
    assert.equal(successCalls, 1);
    view.unmount();
    console.log('Codex login modal: success stays final against late waiting / error responses and notifies once.');
}

async function errorIsFinal() {
    const { apiFetch, held } = fakeApi();
    const view = mount({ isOpen: true, onClose: () => {}, onSuccess: () => {} }, apiFetch);
    await view.settle();
    await waitForPolls(view, held, 3);
    held[1]({ state: 'error', attempt_id: 1, error: 'exchange failed' });
    await view.settle();
    assert.ok(view.html.includes('exchange failed'), view.html);
    held[0]({ state: 'waiting', attempt_id: 1, auth_url: 'https://auth.example/one' });
    await view.settle();
    assert.ok(view.html.includes('exchange failed'), 'a late waiting overwrote the error');
    // idle → error の読み替えも、確定した error を上書きしない
    held[2]({ state: 'idle' });
    await view.settle();
    assert.ok(view.html.includes('exchange failed'), 'a late idle overwrote the error');
    assert.ok(!view.html.includes(escapeHtml(text('text029'))));
    assert.ok(view.html.includes(text('text009')), 'no retry button');
    view.unmount();
    console.log('Codex login modal: error stays final against late waiting / idle responses.');
}

async function replacedAttemptIsReportedNotAdopted() {
    const { apiFetch, held } = fakeApi();
    const view = mount({ isOpen: true, onClose: () => {}, onSuccess: () => {} }, apiFetch);
    await view.settle();
    await waitForPolls(view, held, 1);
    held[0]({ state: 'waiting', attempt_id: 2, auth_url: 'https://auth.example/two' });
    await view.settle();
    assert.ok(view.html.includes(escapeHtml(text('text030'))), view.html);
    assert.ok(view.html.includes(text('text009')), 'no retry button');
    assert.ok(!view.html.includes('https://auth.example/two'), "adopted the other attempt's sign-in link");
    view.unmount();
    console.log('Codex login modal: a replaced attempt shows the retry screen and never adopts the other attempt.');
}

(async () => {
    await successIsFinal();
    await errorIsFinal();
    await replacedAttemptIsReportedNotAdopted();
})().catch((error) => {
    console.error(error);
    // A failed assertion skips unmount, so the modal's poll interval would keep
    // the process alive forever; exit explicitly.
    process.exit(1);
});
