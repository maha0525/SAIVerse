// AddonInstallProgressDialog の「確認の要求は 1 回だけ」「SSE が切れたら状態の問い合わせで
// 終わりを待つ」を、ブラウザなしで確かめる。React の hooks を最小限に模した器で
// 本物のコンポーネントを回し、fetch は偽物を渡す (API・LLM には一切つながない)。
// docs/issues/addon_install_progress_dialog_stuck_on_long_installs.md
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const messages = require('../src/i18n/messages.json');

const file = path.resolve(__dirname, '../src/components/AddonInstallProgressDialog.tsx');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = (key) => messages[key].ja;

/** hooks を模した器。strict: true で React の開発モード (StrictMode) の二度回しを再現する。 */
function mount(props, apiFetch, { strict = false } = {}) {
    const states = [];
    const refs = [];
    const effects = [];  // { deps, cleanup }
    let pending = [];
    let dirty = false;
    let html = '';
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
        useEffect(fn, deps) { pending.push({ i: shim._e++, fn, deps }); },
    };
    let source = fs.readFileSync(file, 'utf8');
    // 待ち時間だけ縮める (中身の判定はそのまま)
    source = source
        .replace(/const STALL_TIMEOUT_MS = [\d_]+;/, 'const STALL_TIMEOUT_MS = 300;')
        .replace(/const STALL_CHECK_INTERVAL_MS = [\d_]+;/, 'const STALL_CHECK_INTERVAL_MS = 30;')
        .replace(/const STATUS_POLL_INTERVAL_MS = [\d_]+;/, 'const STATUS_POLL_INTERVAL_MS = 30;');
    const output = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;
    const mod = { exports: {} };
    const localRequire = (name) => {
        if (name === 'react') return shim;
        if (name === '@/i18n/core') return { t: (key, vars) => messages[key].ja.replace(/\{(\w+)\}/g, (_, k) => vars?.[k] ?? '') };
        if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
        if (name === '@/i18n/api') return { apiFetch, parseUIEvent: (t) => JSON.parse(t) };
        if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === './common/ModalOverlay') return { __esModule: true, default: ({ children }) => children };
        return require(name);
    };
    new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
    const Component = mod.exports.default;

    function runEffects(list) {
        for (const { i, fn, deps } of list) {
            const prev = effects[i];
            const changed = !prev || !deps || deps.length !== prev.deps.length || deps.some((d, k) => !Object.is(d, prev.deps[k]));
            if (!changed) continue;
            if (prev?.cleanup) prev.cleanup();
            effects[i] = { deps: deps || [], cleanup: fn() || null };
        }
    }
    function render() {
        shim._s = 0; shim._r = 0; shim._e = 0;
        pending = [];
        dirty = false;
        html = renderToStaticMarkup(Component(props));
        return pending;
    }
    const first = render();
    runEffects(first);
    if (strict) {
        // StrictMode: 片付けを全部回してから、同じ effect をもう一度回す
        for (const e of effects) e?.cleanup?.();
        effects.length = 0;
        runEffects(first);
    }
    return {
        get html() { return html; },
        async settle(ms = 20) {
            await sleep(ms);
            for (let n = 0; dirty && n < 20; n++) runEffects(render());
        },
        unmount() { for (const e of effects) e?.cleanup?.(); },
    };
}

/** SSE の偽物。send で行を流し、end で正常終了、signal の中断で読み取りを失敗させる。 */
function fakeStream(signal) {
    let controller;
    const stream = new ReadableStream({ start(c) { controller = c; } });
    const enc = new TextEncoder();
    signal?.addEventListener('abort', () => { try { controller.error(new DOMException('aborted', 'AbortError')); } catch { /* closed */ } });
    return {
        stream,
        send: (obj) => controller.enqueue(enc.encode(`data: ${JSON.stringify(obj)}\n\n`)),
        end: () => controller.close(),
    };
}

const props = { addonId: 'saiverse-voice-tts', displayName: 'Voice TTS', operation: 'update', answers: {}, onClose: () => {} };
const OP = 'op-1';

async function strictModeSendsOnce() {
    const posts = [];
    const view = mount(props, async (url, init) => {
        posts.push({ url, init });
        const s = fakeStream(init.signal);
        s.send({ phase: 'started', operation_id: OP });
        return { ok: true, status: 200, body: s.stream };
    }, { strict: true });
    await view.settle();
    assert.equal(posts.length, 1, 'StrictMode の二度回しで確認の要求が二回送られた');
    assert.equal(posts[0].url, '/api/addon-catalog/update/confirm');
    assert.equal(posts[0].init.signal.aborted, false, '二度回しの片付けで、唯一の要求が中断された');
    // 実行中も閉じる手段がある (× と、フッターの「閉じる (処理は続きます)」)
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text019')));
    assert.ok(view.html.includes(`aria-label="${text('components.AddonInstallProgressDialog.text013')}"`));
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text012')));
    view.unmount();
    await sleep(10);
    assert.equal(posts[0].init.signal.aborted, true, '閉じたあとも読み続けている');
    console.log('Progress dialog sends the confirm request once under StrictMode and can be closed while running.');
}

async function stallThenPollToResult() {
    let statusCalls = 0;
    let signal;
    const view = mount(props, async (url, init) => {
        if (url.startsWith('/api/addon-catalog/operations/')) {
            statusCalls++;
            const body = statusCalls < 3
                ? { running: true, operation_id: OP, last_result: null }
                : { running: false, operation_id: null, last_result: { operation_id: OP, ok: true, restart_required: true, manifest: { name: 'x', version: '0.6.0', setup_version: 3 } } };
            return { ok: true, status: 200, json: async () => body };
        }
        signal = init.signal;
        const s = fakeStream(init.signal);
        s.send({ phase: 'started', operation_id: OP });
        return { ok: true, status: 200, body: s.stream };  // その後は何も届かない (中継が切れたまま)
    });
    await view.settle(50);
    assert.ok(!view.html.includes(text('components.AddonInstallProgressDialog.text016')));
    let sawLost = false;
    for (let n = 0; n < 60 && !view.html.includes('v0.6.0'); n++) {
        await view.settle(20);
        if (view.html.includes(text('components.AddonInstallProgressDialog.text016'))) sawLost = true;
    }
    assert.ok(signal.aborted, '無通信なのに読み取りを打ち切っていない');
    assert.ok(sawLost, '「接続が切れましたが、処理は続いています」が出なかった');
    assert.ok(statusCalls >= 3);
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text008')), view.html);
    assert.ok(view.html.includes('v0.6.0'));
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text010')));
    view.unmount();
    console.log('Progress dialog detects a silent connection, polls the operation status, and shows the recorded result.');
}

async function cleanEndWithoutRecordIsUnknown() {
    const view = mount(props, async (url, init) => {
        if (url.startsWith('/api/addon-catalog/operations/')) {
            // バックエンドの再起動などで、この操作の結果の記録が無い
            return { ok: true, status: 200, json: async () => ({ running: false, operation_id: null, last_result: null }) };
        }
        const s = fakeStream(init.signal);
        s.send({ phase: 'started', operation_id: OP });
        setTimeout(() => s.end(), 10);  // finished を送らずに終わる
        return { ok: true, status: 200, body: s.stream };
    });
    for (let n = 0; n < 30 && !view.html.includes(text('components.AddonInstallProgressDialog.text017')); n++) await view.settle(20);
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text017')), view.html);
    assert.ok(view.html.includes(text('components.AddonInstallProgressDialog.text018')));
    assert.ok(!view.html.includes(text('components.AddonInstallProgressDialog.text008')), '結果の記録が無いのに「完了」と言った');
    view.unmount();
    console.log('Progress dialog reports an unknown outcome (not success) when the stream ends without a result record.');
}

(async () => {
    await strictModeSendsOnce();
    await stallThenPollToResult();
    await cleanEndWithoutRecordIsUnknown();
})().catch((error) => { console.error(error); process.exitCode = 1; });
