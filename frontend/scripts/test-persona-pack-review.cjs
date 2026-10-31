// Exercise the comparison list of a pack that came back from Lite without a browser or backend
// (same TypeScript loader and hook stand-ins as test-system-alert-banner.cjs).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const file = path.resolve(__dirname, '../src/components/memory/PersonaPackReview.tsx');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
    },
}).outputText;

const P = 'components.memory.PersonaPack.';

function side(content, extra = {}) {
    return { exists: true, deleted: false, title: null, summary: null, content, updated_at: 1700000000, truncated: false, ...extra };
}

function message(id, extra = {}) {
    return { key: `message:${id}`, kind: 'message', change: 'edited', body: side(`body ${id}`), lite: side(`lite ${id}`), ...extra };
}

function harness(allItems, { failPages = () => false, fullKey = null } = {}) {
    const states = [], effectDeps = [], requests = [];
    let cursor = 0, effectCursor = 0, pending = [];
    const hooks = {
        useState(initial) {
            const index = cursor++;
            if (!(index in states)) states[index] = initial;
            return [states[index], value => {
                states[index] = typeof value === 'function' ? value(states[index]) : value;
            }];
        },
        useEffect(effect, deps) {
            const index = effectCursor++;
            const prev = effectDeps[index];
            effectDeps[index] = deps;
            if (!prev || deps.some((d, k) => d !== prev[k])) pending.push(effect);
        },
    };
    const jsx = (type, props) => ({ type, props });
    const localRequire = name => {
        if (name === 'react') return { __esModule: true, default: {}, ...hooks };
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === '@/i18n/core') return { t: (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key), getFormatLocale: () => 'ja-JP' };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async url => {
            requests.push(url);
            const query = new URL(url, 'http://local').searchParams;
            const offset = Number(query.get('offset'));
            const limit = Number(query.get('limit'));
            const full = query.get('full') === 'true';
            if (!full && failPages(offset)) {
                return { ok: false, status: 500, statusText: 'Server Error', json: async () => ({ detail: 'broken' }) };
            }
            const items = allItems.slice(offset, offset + limit).map(item => (full
                ? { ...item, key: fullKey ?? item.key, lite: { ...item.lite, content: item.fullText ?? item.lite.content, truncated: false } }
                : item));
            return { ok: true, json: async () => ({ items }) };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    const applied = [];
    const props = {
        personaId: 'p1',
        compare: {
            token: 'tok', has_changes: true, total: allItems.length, counts: {},
            message_deletions: allItems.filter(i => i.kind === 'message' && i.change === 'deleted').length,
            warnings: [],
        },
        applying: false,
        onApply: (bulk, choices) => applied.push({ bulk, choices }),
        onCancel() {},
    };
    function flat(node, out = []) {
        if (Array.isArray(node)) node.forEach(child => flat(child, out));
        else if (node && typeof node === 'object') {
            if (typeof node.type === 'function') flat(node.type(node.props), out);
            else {
                out.push(node);
                flat(node.props?.children, out);
            }
        }
        return out;
    }
    async function render() {
        cursor = 0;
        effectCursor = 0;
        let tree = module.exports.default(props);
        while (pending.length) {
            pending.splice(0).forEach(effect => effect());
            await new Promise(setImmediate);
            cursor = 0;
            effectCursor = 0;
            tree = module.exports.default(props);
        }
        return flat(tree);
    }
    /** The effect runs only up to its first await: what the screen shows while a page is loading. */
    async function renderOnce() {
        cursor = 0;
        effectCursor = 0;
        module.exports.default(props);
        pending.splice(0).forEach(effect => effect());
        cursor = 0;
        effectCursor = 0;
        return flat(module.exports.default(props));
    }
    return { render, renderOnce, requests, applied };
}

const byI18n = (nodes, key) => nodes.find(n => n.props?.['data-i18n'] === `${P}${key}`);
const radios = nodes => nodes.filter(n => n.type === 'input' && n.props.type === 'radio');
const text = nodes => JSON.stringify(nodes.map(n => (typeof n.props?.children === 'string' ? n.props.children : '')));
const pagerButton = (nodes, key) => nodes.find(n => n.type === 'button' && n.props.className === 'secondaryButton'
    && flat2(n).some(c => c.props?.['data-i18n'] === `${P}${key}`));

(async () => {
    // Nothing is chosen by default, and the apply button waits for every item.
    {
        const h = harness([message('a'), message('b', { change: 'deleted', lite: side(null, { exists: false }) })]);
        let nodes = await h.render();
        assert.equal(radios(nodes).length, 4);
        assert.ok(radios(nodes).every(r => r.props.checked === false));
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, true);
        assert.ok(byI18n(nodes, 'reviewUndecided'));
        assert.ok(byI18n(nodes, 'reviewDeleteNote'), 'the note about the Chronicle appears for a message deletion');
        assert.equal(nodes.filter(n => n.props?.['data-i18n'] === `${P}reviewDeleteNote`).length, 1);
        assert.ok(byI18n(nodes, 'reviewDeletedInLite'));

        radios(nodes)[1].props.onChange();
        nodes = await h.render();
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, true);
        assert.ok(byI18n(nodes, 'reviewUndecided').props.children.includes('"p1":1'));

        radios(nodes)[2].props.onChange();
        nodes = await h.render();
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, false);
        byI18n(nodes, 'reviewApply').props.onClick();
        assert.deepEqual(h.applied, [{ bulk: null, choices: { 'message:a': 'lite', 'message:b': 'body' } }]);
    }

    // A bulk choice decides everything, one item can still differ, and a new bulk clears that item.
    {
        const h = harness([message('a'), message('b')]);
        let nodes = await h.render();
        byI18n(nodes, 'reviewAllLite').props.onClick();
        nodes = await h.render();
        assert.ok(byI18n(nodes, 'reviewAllDecided'));
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, false);
        assert.deepEqual(radios(nodes).map(r => r.props.checked), [false, true, false, true]);

        radios(nodes)[2].props.onChange();
        nodes = await h.render();
        assert.deepEqual(radios(nodes).map(r => r.props.checked), [false, true, true, false]);
        byI18n(nodes, 'reviewApply').props.onClick();
        assert.deepEqual(h.applied[0], { bulk: 'lite', choices: { 'message:b': 'body' } });

        byI18n(nodes, 'reviewAllBody').props.onClick();
        nodes = await h.render();
        assert.deepEqual(radios(nodes).map(r => r.props.checked), [true, false, true, false]);
    }

    // The list loads one page at a time; a choice on another page still counts.
    {
        const items = Array.from({ length: 35 }, (_, i) => message(`m${i}`));
        const h = harness(items);
        let nodes = await h.render();
        assert.equal(radios(nodes).length, 60);
        assert.equal(h.requests.length, 1);
        radios(nodes)[0].props.onChange();
        nodes = await h.render();
        assert.ok(byI18n(nodes, 'reviewUndecided').props.children.includes('"p1":34'));
        const next = nodes.find(n => n.type === 'button' && n.props.className === 'secondaryButton'
            && flat2(n).some(c => c.props?.['data-i18n'] === `${P}reviewNext`));
        next.props.onClick();
        nodes = await h.render();
        assert.equal(radios(nodes).length, 10);
        assert.ok(h.requests[1].includes('offset=30'));
        assert.ok(byI18n(nodes, 'reviewUndecided').props.children.includes('"p1":34'));
    }

    // A long text is cut in the list, and opening it reads the whole text once.
    {
        const h = harness([message('a', {
            lite: side('short version', { truncated: true }), fullText: 'the whole long version '.repeat(30),
        })]);
        let nodes = await h.render();
        assert.ok(text(nodes).includes('short version'));
        const open = nodes.find(n => n.type === 'button' && n.props.className === 'reviewLink');
        open.props.onClick();
        await new Promise(setImmediate);
        nodes = await h.render();
        assert.ok(text(nodes).includes('the whole long version'));
        assert.ok(h.requests[1].includes('full=true'));
        assert.ok(byI18n(nodes, 'reviewCollapse'));
    }

    // Moving to a page that cannot be read shows nothing of the old page, and choosing stops until a page loads.
    {
        const items = Array.from({ length: 35 }, (_, i) => message(`m${i}`));
        const h = harness(items, { failPages: offset => offset === 30 });
        let nodes = await h.render();
        byI18n(nodes, 'reviewAllLite').props.onClick();
        nodes = await h.render();
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, false);
        pagerButton(nodes, 'reviewNext').props.onClick();
        nodes = await h.render();
        assert.equal(radios(nodes).length, 0, 'the items of the page left behind are gone');
        assert.ok(text(nodes).includes('reviewLoadFailed'));
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, true);
        assert.equal(byI18n(nodes, 'reviewAllLite').props.disabled, true);
        assert.equal(byI18n(nodes, 'reviewAllBody').props.disabled, true);

        pagerButton(nodes, 'reviewPrev').props.onClick();
        nodes = await h.render();
        assert.equal(radios(nodes).length, 60);
        assert.ok(!text(nodes).includes('reviewLoadFailed'));
        assert.equal(byI18n(nodes, 'reviewApply').props.disabled, false);
    }

    // The old items are gone from the moment the next page starts loading.
    {
        const items = Array.from({ length: 35 }, (_, i) => message(`m${i}`));
        const h = harness(items);
        let nodes = await h.render();
        pagerButton(nodes, 'reviewNext').props.onClick();
        nodes = await h.renderOnce();
        assert.equal(radios(nodes).length, 0);
        assert.ok(byI18n(nodes, 'reviewLoading'));
    }

    // A whole text that comes back for another item is not shown, and choosing goes on.
    {
        const h = harness([message('a', {
            lite: side('short version', { truncated: true }), fullText: 'text of another item',
        })], { fullKey: 'message:other' });
        let nodes = await h.render();
        nodes.find(n => n.type === 'button' && n.props.className === 'reviewLink').props.onClick();
        await new Promise(setImmediate);
        nodes = await h.render();
        assert.ok(!text(nodes).includes('text of another item'));
        assert.ok(text(nodes).includes('reviewLoadFailed'));
        assert.ok(byI18n(nodes, 'reviewShowAll'), 'the item stays folded');
        assert.equal(radios(nodes).length, 2);
        assert.equal(byI18n(nodes, 'reviewAllLite').props.disabled, false);
    }

    console.log('Pack comparison list: nothing chosen by default, apply waits for every item, bulk then one change, delete note once, paged list, long text on demand, a failed page stops choosing, and a whole text of another item is refused passed.');
    await panelTests();
})().catch(error => {
    console.error(error);
    process.exit(1);
});

/** PersonaPackPanel with the review stood in: what the panel asks of the server. */
function panelHarness(respond) {
    const panelFile = path.resolve(__dirname, '../src/components/memory/PersonaPackPanel.tsx');
    const panelCode = ts.transpileModule(fs.readFileSync(panelFile, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    }).outputText;
    const states = [], requests = [];
    let cursor = 0;
    const Review = () => null;
    const jsx = (type, props) => ({ type, props });
    const localRequire = name => {
        if (name === 'react') {
            return {
                __esModule: true, default: {},
                useRef: () => ({ current: null }),
                useState(initial) {
                    const index = cursor++;
                    if (!(index in states)) states[index] = initial;
                    return [states[index], value => { states[index] = value; }];
                },
            };
        }
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
        if (name === '@/i18n/core') return { t: key => key };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/i18n/api') return { apiFetch: async (url, init) => {
            requests.push({ url, method: init?.method ?? 'GET' });
            return respond(url, init);
        } };
        if (name === './PersonaPackResult') return { __esModule: true, default: () => null };
        if (name === './PersonaPackReview') return { __esModule: true, default: Review };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', panelCode)(localRequire, module, module.exports);
    const render = () => {
        cursor = 0;
        return flat2(module.exports.default({ personaId: 'p1' }));
    };
    return { render, requests, review: nodes => nodes.find(n => n.type === Review) };
}

async function openReview(h) {
    let nodes = h.render();
    nodes.find(n => n.type === 'input' && n.props.type === 'file').props.onChange({ target: { files: [{ name: 'pack.zip' }], value: '' } });
    nodes = h.render();
    await byI18n(nodes, 'mergeAction').props.onClick();
    await new Promise(setImmediate);
    return h.render();
}

async function panelTests() {
    const compared = { ok: true, json: async () => ({ token: 'tok', has_changes: true, total: 2, counts: {}, message_deletions: 0, warnings: [], resume: false }) };

    // Cancelling the list deletes the comparison on the server, and a refused delete still closes the list.
    for (const answer of [{ ok: true }, { ok: false, status: 500, statusText: 'Server Error', json: async () => ({ detail: 'in use' }) }]) {
        const warnings = [];
        const warn = console.warn;
        console.warn = (...args) => warnings.push(args);
        const h = panelHarness((url, init) => (init?.method === 'DELETE' ? answer : compared));
        const nodes = await openReview(h);
        h.review(nodes).props.onCancel();
        await new Promise(setImmediate);
        console.warn = warn;
        assert.deepEqual(h.requests.at(-1), { url: '/api/people/p1/persona-pack/compare/tok', method: 'DELETE' });
        assert.equal(h.review(h.render()), undefined);
        assert.equal(warnings.length, answer.ok ? 0 : 1, 'a failed delete leaves a line in the console');
    }

    // A failure after the memories were saved closes the list and asks for a new comparison.
    {
        const h = panelHarness((url, init) => (url.endsWith('/apply')
            ? { ok: false, status: 500, statusText: 'Server Error', json: async () => ({ detail: { code: 'compare_partial', message: 'x' } }) }
            : compared));
        const nodes = await openReview(h);
        h.review(nodes).props.onApply('lite', {});
        await new Promise(setImmediate);
        await new Promise(setImmediate);
        const after = h.render();
        assert.equal(h.review(after), undefined);
        assert.ok(after.some(n => n.props?.children === `${P}comparePartial`));
    }

    console.log('Pack panel: cancel deletes the comparison, and a failure after the memories were saved asks for a new comparison passed.');
}

function flat2(node, out = []) {
    if (Array.isArray(node)) node.forEach(child => flat2(child, out));
    else if (node && typeof node === 'object') {
        out.push(node);
        flat2(node.props?.children, out);
    }
    return out;
}
