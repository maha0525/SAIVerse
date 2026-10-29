// Exercise the review modal's state and handlers without a browser or backend
// (same TypeScript loader and hook stand-ins as test-system-alert-banner.cjs).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.resolve(__dirname, '../src/components/SystemNoticeReviewModal.tsx');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
    },
}).outputText;

function Overlay() { return null; }

function row(pid, mid, actions, role = 'user') {
    return {
        persona_id: pid, persona_name: pid.toUpperCase(), message_id: mid, role, tags: ['tool'],
        content: 'Email sent.', content_truncated: false, reason: 'r', created_at: 1000, actions,
    };
}

function listing(groups) {
    return { total: groups.reduce((n, g) => n + g.rows.length, 0), groups, errors: [] };
}

function harness(initialListing) {
    const states = [], effects = [], requests = [], notices = [];
    let cursor = 0, mounted = false, confirmed = false, closed = 0, changed = 0;
    let serverListing = initialListing;
    let applyResponse = () => ({ applied: [], rejected: [], remaining: 0 });
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
        if (name === './common/ModalOverlay') return { __esModule: true, default: Overlay };
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === '@/i18n/core') return { t: (key, params) => (params && 'count' in params ? `${key}:${params.count}` : key) };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            const method = options?.method || 'GET';
            const body = options?.body ? JSON.parse(options.body) : undefined;
            requests.push({ url, method, body });
            if (method === 'POST') {
                assert.equal(url, '/api/system/notice-review/apply');
                const result = applyResponse(body);
                serverListing = result.next ?? serverListing;
                return { ok: true, json: async () => result };
            }
            assert.equal(url, '/api/system/notice-review');
            return { ok: true, json: async () => serverListing };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    global.window = { confirm: () => confirmed, alert: message => notices.push(message) };
    const props = { onClose: () => { closed++; }, onChanged: async () => { changed++; } };
    function render() {
        cursor = 0;
        const tree = module.exports.default(props);
        mounted = true;
        return tree;
    }
    return {
        render, requests, notices,
        get closed() { return closed; },
        get changed() { return changed; },
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
        confirm(value) { confirmed = value; },
        onApply(fn) { applyResponse = fn; },
    };
}

function all(tree, predicate) {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(node => all(node, predicate));
    return [...(predicate(tree) ? [tree] : []), ...all(tree.props?.children, predicate)];
}
const text = node => {
    const c = node.props.children;
    return Array.isArray(c) ? c.filter(x => typeof x === 'string').join('') : String(c);
};
const actionButtons = tree => all(tree, node => node.type === 'button' && /^components\.SystemNoticeReviewModal\.action/.test(text(node)));
const sections = tree => all(tree, node => node.type === 'section');

(async () => {
    const userable = { group_id: 'g1', role: 'user', head: 'Email sent.', tags: ['tool'], reason: 'r',
        actions: ['keep', 'system', 'user', 'delete'], count: 2,
        personas: [{ persona_id: 'a', persona_name: 'A', count: 1 }, { persona_id: 'b', persona_name: 'B', count: 1 }],
        rows: [row('a', 'm1', ['keep', 'system', 'user', 'delete']), row('b', 'm2', ['keep', 'system', 'user', 'delete'])] };
    const persona = { group_id: 'g2', role: 'assistant', head: 'memo', tags: [], reason: 'r',
        actions: ['keep', 'system', 'delete'], count: 1,
        personas: [{ persona_id: 'a', persona_name: 'A', count: 1 }],
        rows: [row('a', 'm3', ['keep', 'system', 'delete'], 'assistant')] };

    let h = harness(listing([userable, persona]));
    let tree = await h.mount();
    assert.equal(sections(tree).length, 2);
    // Each group offers exactly the actions the server returned for it.
    const [first, second] = sections(tree);
    assert.deepEqual(actionButtons(first).map(text), [
        'components.SystemNoticeReviewModal.actionKeep', 'components.SystemNoticeReviewModal.actionSystem',
        'components.SystemNoticeReviewModal.actionUser', 'components.SystemNoticeReviewModal.actionDelete',
    ]);
    assert.ok(!actionButtons(second).map(text).includes('components.SystemNoticeReviewModal.actionUser'));

    // Applying a group sends every row of that group, then refreshes alerts and the list.
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 1, next: listing([persona]) }));
    await actionButtons(first)[1].props.onClick();
    const post = h.requests.find(r => r.method === 'POST');
    assert.deepEqual(post.body, { action: 'system', items: [
        { persona_id: 'a', message_id: 'm1' }, { persona_id: 'b', message_id: 'm2' },
    ] });
    assert.equal(h.changed, 1);
    tree = h.render();
    assert.equal(sections(tree).length, 1);
    assert.equal(h.closed, 0);

    // Delete asks first; cancelling sends nothing.
    const posts = () => h.requests.filter(r => r.method === 'POST').length;
    const before = posts();
    await actionButtons(sections(tree)[0]).at(-1).props.onClick();
    assert.equal(posts(), before);

    // Rows can be chosen one by one after expanding the group.
    const expand = all(sections(tree)[0], node => node.type === 'button' && 'aria-expanded' in node.props)[0];
    expand.props.onClick();
    tree = h.render();
    const rowItems = all(tree, node => node.type === 'li');
    assert.equal(rowItems.length, 1);

    // Confirmed delete of the last row closes the modal once nothing is left,
    // and rejected rows are reported.
    h.confirm(true);
    h.onApply(body => ({ applied: [], rejected: body.items.map(i => ({ ...i, reason: 'not_allowed' })), remaining: 0, next: listing([]) }));
    await actionButtons(rowItems[0]).at(-1).props.onClick();
    const last = h.requests.filter(r => r.method === 'POST').at(-1);
    assert.deepEqual(last.body, { action: 'delete', items: [{ persona_id: 'a', message_id: 'm3' }] });
    assert.equal(h.notices.length, 1);
    assert.equal(h.closed, 1);
    assert.equal(h.changed, 2);
    console.log('Notice review modal: per-group actions, group apply, delete confirmation, row choice, reload, and close-when-empty passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
