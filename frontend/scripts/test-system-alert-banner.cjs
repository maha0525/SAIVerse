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

// Stand-in for the review modal: the banner only decides when to mount it.
function ReviewModal() { return null; }

function harness(initialAlerts) {
    const states = [], effects = [], requests = [], notices = [];
    const effectDeps = [], pendingEffects = [];
    let cursor = 0, effectCursor = 0, mounted = false, confirmed = false, backendDown = false;
    let serverAlerts = initialAlerts, postResponse = { ok: true }, postGate, dismissFails = false;
    const hooks = {
        useState(initial) {
            const index = cursor++;
            if (!(index in states)) states[index] = initial;
            return [states[index], value => {
                states[index] = typeof value === 'function' ? value(states[index]) : value;
            }];
        },
        // Runs on mount, and again on a later render only when a dependency changed
        // (React's rule for an effect with a dependency array).
        useEffect(effect, deps) {
            const index = effectCursor++;
            const prev = effectDeps[index];
            effectDeps[index] = deps;
            if (!mounted) { effects.push(effect); return; }
            if (!deps || !prev || deps.some((d, k) => d !== prev[k])) pendingEffects.push(effect);
        },
    };
    const jsx = (type, props) => ({ type, props });
    const localRequire = name => {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'lucide-react') return {};
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === './SystemNoticeReviewModal') return {
            __esModule: true, default: ReviewModal, NOTICE_REVIEW_ALERT_ID: 'system_notice_review_pending',
            NOTICE_REVIEW_CHANGED_EVENT: 'saiverse:system-notice-review-changed',
        };
        if (name === '@/i18n/core') return { t: key => key };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            requests.push({ url, method: options?.method || 'GET' });
            const dismissed = /^\/api\/system\/alerts\/([^/]+)\/dismiss$/.exec(url);
            if (options?.method === 'POST' && dismissed) {
                if (dismissFails) throw new Error('backend not reachable');
                const id = decodeURIComponent(dismissed[1]);
                serverAlerts = serverAlerts.map(a => (a.id === id ? { ...a, dismissed: true } : a));
                return { ok: true, json: async () => ({ dismissed: true }) };
            }
            if (options?.method === 'POST') {
                if (postGate) await postGate;
                if (postResponse instanceof Error) throw postResponse;
                if (postResponse.ok) serverAlerts = serverAlerts.filter(a => a.details?.kind !== 'unreadable');
                return postResponse;
            }
            assert.equal(url, '/api/system/alerts');
            if (backendDown) throw new Error('backend not reachable');
            return { ok: true, json: async () => ({ alerts: serverAlerts }) };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    const listeners = new Map();
    global.window = {
        confirm: () => confirmed,
        alert: message => notices.push(message),
        addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter(f => f !== fn)); },
        dispatchEvent(event) { (listeners.get(event.type) || []).forEach(fn => fn(event)); return true; },
    };
    let props = {};
    function render(nextProps) {
        if (nextProps) props = nextProps;
        cursor = 0;
        effectCursor = 0;
        const tree = module.exports.default(props);
        mounted = true;
        return tree;
    }
    return {
        render, requests, notices,
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
        async rerender(nextProps) {
            render(nextProps);
            pendingEffects.splice(0).forEach(effect => effect());
            await new Promise(setImmediate);
            return render();
        },
        down(value) { backendDown = value; },
        confirm(value) { confirmed = value; },
        post(response, gate) { postResponse = response; postGate = gate; },
        failDismiss(value) { dismissFails = value; },
        server() { return serverAlerts.filter(a => !a.dismissed).map(a => a.id); },
        serverAlerts() { return serverAlerts; },
    };
}

function all(tree, predicate) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(node => all(node, predicate));
    return [...(predicate(tree) ? [tree] : []), ...all(tree.props?.children, predicate)];
}
const headers = tree => all(tree, node => node.type === 'button' && 'aria-expanded' in node.props);
const archiveButton = tree => all(tree, node => node.type === 'button' && node.props.className === 'secondaryButton')[0];
const closeButtons = tree => all(tree, node => node.type === 'button'
    && String(node.props['data-i18n'] || '').includes('dismissLabel'));
const exampleAlerts = () => [
    { id: 'critical', level: 'critical', title: 'Critical', message: 'Keep visible', details: { kind: 'check_failed' } },
    { id: 'legacy', level: 'warning', title: 'Old log', message: 'Unreadable', details: { building_id: 'room / 1', kind: 'unreadable' } },
    { id: 'information', level: 'info', title: 'Information', message: 'Keep too' },
];

(async () => {
    let h = harness([]);
    assert.equal(await h.mount(), null);

    // The page opens before the backend finishes starting (or the backend restarts
    // while the page stays open): the first fetch fails, and the banner must fetch
    // again once the page is connected to the backend again — not on a timer.
    h = harness(exampleAlerts());
    h.down(true);
    assert.equal(await h.mount(), null);
    h.down(false);
    assert.equal(await h.rerender({ backendConnected: false }), null);
    assert.equal(h.requests.filter(r => r.url === '/api/system/alerts').length, 1);
    let reconnected = await h.rerender({ backendConnected: true });
    assert.equal(headers(reconnected).length, 3);
    assert.equal(h.requests.filter(r => r.url === '/api/system/alerts').length, 2);
    // Staying connected does not fetch again
    await h.rerender({ backendConnected: true });
    assert.equal(h.requests.filter(r => r.url === '/api/system/alerts').length, 2);

    // Closing an alert tells the server, so reopening the page does not show it
    // again during this startup (2026-10-30: the migration-done alert came back on
    // every page reload while it was only hidden inside the page).
    h = harness(exampleAlerts());
    let closing = await h.mount();
    closeButtons(closing)[2].props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(h.requests.filter(r => r.method === 'POST').map(r => r.url),
        ['/api/system/alerts/information/dismiss']);
    assert.equal(headers(h.render()).length, 2);
    assert.deepEqual(h.server(), ['critical', 'legacy']);
    // The server keeps the alert (other screens follow it) but marks it closed; a
    // page opened afterwards does not show it
    assert.equal(h.serverAlerts().length, 3);
    const reopened = harness(h.serverAlerts());
    assert.equal(headers(await reopened.mount()).length, 2);
    // If the server cannot be told, the alert still closes on this page
    h = harness(exampleAlerts());
    h.failDismiss(true);
    closing = await h.mount();
    closeButtons(closing)[0].props.onClick();
    await new Promise(setImmediate);
    assert.equal(headers(h.render()).length, 2);
    assert.deepEqual(h.server(), ['critical', 'legacy', 'information']);

    h = harness(exampleAlerts());
    let tree = await h.mount();
    assert.deepEqual(headers(tree).map(node => node.props['aria-expanded']), [true, false, false]);
    // 3 headers + 3 close buttons + 1 archive button
    assert.equal(all(tree, node => node.type === 'button').length, 7);
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

    // The pending-review alert carries a button that mounts the review modal;
    // other alerts do not. Closing the modal unmounts it, and the modal's change
    // callback fetches the alerts again.
    const reviewAlert = { id: 'system_notice_review_pending', level: 'warning', title: 'Review', message: 'm', details: {} };
    h = harness([reviewAlert, { id: 'information', level: 'info', title: 'Information', message: 'Keep too' }]);
    tree = await h.mount();
    const reviewButtons = all(tree, node => node.type === 'button' && node.props['data-i18n'] === 'components.SystemAlertBanner.openNoticeReview');
    assert.equal(reviewButtons.length, 1);
    assert.equal(all(tree, node => node.type === ReviewModal).length, 0);
    reviewButtons[0].props.onClick();
    tree = h.render();
    const modal = all(tree, node => node.type === ReviewModal)[0];
    assert.ok(modal);
    const before = h.requests.length;
    await modal.props.onChanged();
    assert.equal(h.requests.length, before + 1);
    assert.equal(h.requests[before].url, '/api/system/alerts');
    modal.props.onClose();
    assert.equal(all(h.render(), node => node.type === ReviewModal).length, 0);

    // An expanded alert whose details are empty ({}) or missing draws no details
    // box; an alert with details draws one row per key.
    h = harness([
        { id: 'empty', level: 'warning', title: 'Empty', message: 'm', details: {} },
        { id: 'none', level: 'info', title: 'None', message: 'm' },
        { id: 'full', level: 'critical', title: 'Full', message: 'm', details: { kind: 'check_failed', path: 'p' } },
    ]);
    tree = await h.mount();
    headers(tree)[0].props.onClick();
    headers(h.render())[1].props.onClick();
    tree = h.render();
    assert.deepEqual(headers(tree).map(node => node.props['aria-expanded']), [true, true, true]);
    const detailBoxes = all(tree, node => node.type === 'dl');
    assert.equal(detailBoxes.length, 1);
    assert.equal(all(detailBoxes[0], node => node.type === 'dt').length, 2);

    // Light is the default look; dark mode overrides it under [data-theme="dark"]
    // (app/layout.tsx sets data-theme on <html>). Each level keeps its own
    // left-edge colour in both themes, distinct from the other levels.
    const css = fs.readFileSync(path.resolve(__dirname, '../src/components/SystemAlertBanner.module.css'), 'utf8');
    const leftEdge = selector => {
        const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const block = css.match(new RegExp(`(?:^|\\n)${escaped} \\{([^}]*)\\}`));
        assert.ok(block, `missing rule ${selector}`);
        return block[1].match(/border-left-color:\s*([^;]+);/)[1].trim();
    };
    for (const prefix of ['', ':global([data-theme="dark"]) ']) {
        const edges = ['critical', 'warning', 'info'].map(level => leftEdge(`${prefix}.${level}`));
        assert.equal(new Set(edges).size, 3, `${prefix || 'light'}: ${edges}`);
    }
    const lightAlert = css.match(/(?:^|\n)\.alert \{([^}]*)\}/)[1];
    assert.match(lightAlert, /background:\s*#ffffff;/);
    assert.ok(/:global\(\[data-theme="dark"\]\) \.alert \{[^}]*background:\s*#2a2a2a;/.test(css));
    console.log('System alerts: refetch on reconnect, close remembered by the server, expansion, cancel, failure, retry, busy state, unrelated alerts, review entry, empty details, and light/dark level colours passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
