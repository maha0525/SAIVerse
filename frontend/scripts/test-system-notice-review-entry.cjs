// The settings entry to the review modal: it stays reachable after the banner is
// closed, and appears only while the server keeps the pending-review alert.
// Same TypeScript loader and hook stand-ins as test-system-alert-banner.cjs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.resolve(__dirname, '../src/components/SystemNoticeReviewEntry.tsx');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
    },
}).outputText;

function ReviewModal() { return null; }
const REVIEW_ID = 'system_notice_review_pending';

function harness(initialAlerts) {
    const states = [], effects = [], requests = [];
    let cursor = 0, mounted = false, serverAlerts = initialAlerts;
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
        if (name === './SystemNoticeReviewModal') return {
            __esModule: true, default: ReviewModal, NOTICE_REVIEW_ALERT_ID: REVIEW_ID,
            NOTICE_REVIEW_CHANGED_EVENT: 'saiverse:system-notice-review-changed',
        };
        if (name === '@/i18n/core') return { t: key => key };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async url => {
            requests.push(url);
            assert.equal(url, '/api/system/alerts');
            return { ok: true, json: async () => ({ alerts: serverAlerts }) };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    const listeners = new Map();
    global.window = {
        addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter(f => f !== fn)); },
        dispatchEvent(event) { (listeners.get(event.type) || []).forEach(fn => fn(event)); return true; },
    };
    const props = { classNames: { card: 'card', button: 'button' } };
    function render() {
        cursor = 0;
        const tree = module.exports.default(props);
        mounted = true;
        return tree;
    }
    return {
        render, requests,
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
        setAlerts(value) { serverAlerts = value; },
    };
}

function all(tree, predicate) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(node => all(node, predicate));
    return [...(predicate(tree) ? [tree] : []), ...all(tree.props?.children, predicate)];
}

(async () => {
    // Nothing pending: no entry.
    let h = harness([{ id: 'other', level: 'info', title: 't', message: 'm' }]);
    assert.equal(await h.mount(), null);

    // Pending: the entry shows the server's title and opens the review modal.
    const review = { id: REVIEW_ID, level: 'warning', title: '3 messages need your review', message: 'm' };
    h = harness([review]);
    let tree = await h.mount();
    assert.equal(all(tree, node => node.type === 'div' && node.props.className === 'card').length, 1);
    assert.ok(all(tree, node => node.type === 'p').some(node => node.props.children === review.title));
    assert.equal(all(tree, node => node.type === ReviewModal).length, 0);
    all(tree, node => node.type === 'button')[0].props.onClick();
    tree = h.render();
    const modal = all(tree, node => node.type === ReviewModal)[0];
    assert.ok(modal);

    // Settling everything removes the server alert; the modal's change callback
    // announces the change, the entry fetches again and disappears once the modal
    // is closed (the banner follows the same announcement —
    // test-system-notice-review-alert-sync.cjs).
    h.setAlerts([]);
    const before = h.requests.length;
    await modal.props.onChanged();
    await new Promise(setImmediate);
    assert.equal(h.requests.length, before + 1);
    modal.props.onClose();
    assert.equal(h.render(), null);
    console.log('Notice review entry: hidden without pending rows, shown with them, opens the modal, and follows the alert.');
})().catch(error => { console.error(error); process.exitCode = 1; });
