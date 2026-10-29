// The top banner and the settings entry each keep their own copy of the server
// alerts. Settling the last pending row from one of them must make the other
// fetch the alerts again, so the banner does not keep a stale pending-review
// alert after the review is finished from the settings entry (and vice versa).
// Both real components are mounted against one fake server and one window
// event bus; the review modal itself is a stand-in whose onChanged is called
// the way the real modal calls it after an apply.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function compile(name) {
    const file = path.resolve(__dirname, `../src/components/${name}.tsx`);
    return ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        compilerOptions: {
            module: ts.ModuleKind.CommonJS,
            target: ts.ScriptTarget.ES2022,
            jsx: ts.JsxEmit.ReactJSX,
            esModuleInterop: true,
        },
    }).outputText;
}

const REVIEW_ID = 'system_notice_review_pending';
const REVIEW_CHANGED_EVENT = 'saiverse:system-notice-review-changed';
function ReviewModal() { return null; }

const server = { alerts: [], requests: [] };
const listeners = new Map();
global.window = {
    confirm: () => true,
    alert: () => {},
    addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) { listeners.get(type)?.delete(fn); },
    dispatchEvent(event) {
        for (const fn of [...(listeners.get(event.type) || [])]) fn(event);
        return true;
    },
};

function mountable(name, props) {
    const compiled = compile(name);
    const states = [], effects = [];
    let cursor = 0, mounted = false;
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
    const jsx = (type, p) => ({ type, props: p });
    const localRequire = dep => {
        if (dep === 'react') return hooks;
        if (dep === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (dep === 'lucide-react') return {};
        if (dep.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (dep === './SystemNoticeReviewModal') return {
            __esModule: true, default: ReviewModal,
            NOTICE_REVIEW_ALERT_ID: REVIEW_ID, NOTICE_REVIEW_CHANGED_EVENT: REVIEW_CHANGED_EVENT,
        };
        if (dep === '@/i18n/core') return { t: key => key };
        if (dep === '@/i18n/useLocale') return { useLocale() {} };
        if (dep === '@/i18n/api') return { apiFetch: async url => {
            server.requests.push(url);
            assert.equal(url, '/api/system/alerts');
            return { ok: true, json: async () => ({ alerts: server.alerts }) };
        } };
        throw new Error(`Unexpected dependency ${dep}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    function render() {
        cursor = 0;
        const tree = module.exports.default(props);
        mounted = true;
        return tree;
    }
    return {
        render,
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
    };
}

function all(tree, predicate) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(node => all(node, predicate));
    return [...(predicate(tree) ? [tree] : []), ...all(tree.props?.children, predicate)];
}
const flush = () => new Promise(setImmediate);
const reviewAlert = { id: REVIEW_ID, level: 'warning', title: '1 message needs your review', message: 'm', details: {} };
const bannerShowsReview = tree => all(tree, node => node.type === 'button'
    && node.props['data-i18n'] === 'components.SystemAlertBanner.openNoticeReview').length > 0;

(async () => {
    // Settle the last row from the settings entry: the banner follows.
    server.alerts = [reviewAlert];
    let banner = mountable('SystemAlertBanner');
    let entry = mountable('SystemNoticeReviewEntry', { classNames: { card: 'card' } });
    assert.ok(bannerShowsReview(await banner.mount()));
    let tree = await entry.mount();
    all(tree, node => node.type === 'button')[0].props.onClick();
    let modal = all(entry.render(), node => node.type === ReviewModal)[0];
    assert.ok(modal);
    server.alerts = [];
    await modal.props.onChanged();
    await flush();
    assert.equal(banner.render(), null, 'banner keeps the pending-review alert after settling from settings');
    modal.props.onClose();
    assert.equal(entry.render(), null);

    // And the other way: settle from the banner, the settings entry follows.
    listeners.clear();
    server.alerts = [reviewAlert];
    banner = mountable('SystemAlertBanner');
    entry = mountable('SystemNoticeReviewEntry', { classNames: { card: 'card' } });
    tree = await banner.mount();
    assert.ok(all(await entry.mount(), node => node.props?.className === 'card').length === 1);
    all(tree, node => node.type === 'button' && node.props['data-i18n'] === 'components.SystemAlertBanner.openNoticeReview')[0].props.onClick();
    modal = all(banner.render(), node => node.type === ReviewModal)[0];
    assert.ok(modal);
    server.alerts = [];
    await modal.props.onChanged();
    await flush();
    assert.equal(entry.render(), null, 'settings entry keeps the pending-review alert after settling from the banner');

    console.log('Notice review alert sync: settling from either entry refreshes both the banner and the settings entry.');
})().catch(error => { console.error(error); process.exitCode = 1; });
