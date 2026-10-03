// Exercise the actual component's state and handlers without a browser or backend.
// Match the TypeScript loader used by test-i18n.cjs; no extra test dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.resolve(__dirname, '../src/components/SystemAlertBanner.tsx');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
    },
}).outputText;

function harness(initialAlerts) {
    const states = [], effects = [], requests = [], notices = [];
    let cursor = 0, mounted = false, confirmed = false;
    let serverAlerts = initialAlerts, postResponse = { ok: true }, postGate;
    const hooks = {
        useState(initial) {
            const index = cursor++;
            if (!(index in states)) states[index] = initial;
            return [states[index], value => {
                states[index] = typeof value === 'function' ? value(states[index]) : value;
            }];
        },
        useEffect(effect) { if (!mounted) effects.push(effect); },
    };
    const jsx = (type, props) => ({ type, props });
    const localRequire = name => {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'lucide-react') return {};
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === '@/i18n/core') return { t: key => key };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            requests.push({ url, method: options?.method || 'GET' });
            if (options?.method === 'POST') {
                if (postGate) await postGate;
                if (postResponse instanceof Error) throw postResponse;
                if (postResponse.ok) serverAlerts = serverAlerts.filter(a => a.details?.kind !== 'unreadable');
                return postResponse;
            }
            assert.equal(url, '/api/system/alerts');
            return { ok: true, json: async () => ({ alerts: serverAlerts }) };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    global.window = { confirm: () => confirmed, alert: message => notices.push(message) };
    function render() {
        cursor = 0;
        const tree = module.exports.default();
        mounted = true;
        return tree;
    }
    return {
        render, requests, notices,
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
        confirm(value) { confirmed = value; },
        post(response, gate) { postResponse = response; postGate = gate; },
    };
}

function all(tree, predicate) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(node => all(node, predicate));
    return [...(predicate(tree) ? [tree] : []), ...all(tree.props?.children, predicate)];
}
const headers = tree => all(tree, node => node.type === 'button' && 'aria-expanded' in node.props);
const archiveButton = tree => all(tree, node => node.type === 'button' && node.props.className === 'secondaryButton')[0];
const exampleAlerts = () => [
    { id: 'critical', level: 'critical', title: 'Critical', message: 'Keep visible', details: { kind: 'check_failed' } },
    { id: 'legacy', level: 'warning', title: 'Old log', message: 'Unreadable', details: { building_id: 'room / 1', kind: 'unreadable' } },
    { id: 'information', level: 'info', title: 'Information', message: 'Keep too' },
];

(async () => {
    let h = harness([]);
    assert.equal(await h.mount(), null);

    h = harness(exampleAlerts());
    let tree = await h.mount();
    assert.deepEqual(headers(tree).map(node => node.props['aria-expanded']), [true, false, false]);
    assert.equal(all(tree, node => node.type === 'button').length, 4);
    headers(tree)[1].props.onClick();
    assert.deepEqual(headers(h.render()).map(node => node.props['aria-expanded']), [true, true, false]);
    headers(h.render())[1].props.onClick();
    assert.equal(headers(h.render())[1].props['aria-expanded'], false);

    // Cancel does not submit, hide alerts, or leave the button disabled.
    await archiveButton(h.render()).props.onClick();
    assert.equal(h.requests.length, 1);
    assert.equal(archiveButton(h.render()).props.disabled, false);
    assert.equal(headers(h.render()).length, 3);

    // A failed submission can be retried, including after a network error.
    h.confirm(true);
    h.post({ ok: false, status: 500 });
    await archiveButton(h.render()).props.onClick();
    assert.equal(h.notices.length, 1);
    assert.equal(archiveButton(h.render()).props.disabled, false);
    assert.equal(headers(h.render()).length, 3);
    h.post(new Error('offline'));
    await archiveButton(h.render()).props.onClick();
    assert.equal(h.notices.length, 2);
    assert.equal(archiveButton(h.render()).props.disabled, false);

    // While awaiting the server the existing button is disabled. Success fetches
    // alerts again and removes only the archived log's action, preserving others.
    let release;
    h.post({ ok: true }, new Promise(resolve => { release = resolve; }));
    const pending = archiveButton(h.render()).props.onClick();
    assert.equal(archiveButton(h.render()).props.disabled, true);
    release();
    await pending;
    tree = h.render();
    assert.equal(headers(tree).length, 2);
    assert.equal(archiveButton(tree), undefined);
    assert.deepEqual(headers(tree).map(node => node.props['aria-expanded']), [true, false]);
    assert.ok(h.requests.some(r => r.url === '/api/system/legacy-log/room%20%2F%201/archive' && r.method === 'POST'));
    assert.ok(h.requests.every(r => !r.url.includes('/quarantine')));
    console.log('System alerts: expansion, cancel, failure, retry, busy state, and unrelated alerts passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
