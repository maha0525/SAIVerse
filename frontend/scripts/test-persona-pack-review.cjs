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

function harness(allItems) {
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
            const items = allItems.slice(offset, offset + limit).map(item => (query.get('full') === 'true'
                ? { ...item, lite: { ...item.lite, content: item.fullText ?? item.lite.content, truncated: false } }
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
    return { render, requests, applied };
}

const byI18n = (nodes, key) => nodes.find(n => n.props?.['data-i18n'] === `${P}${key}`);
const radios = nodes => nodes.filter(n => n.type === 'input' && n.props.type === 'radio');
const text = nodes => JSON.stringify(nodes.map(n => (typeof n.props?.children === 'string' ? n.props.children : '')));

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

    console.log('Pack comparison list: nothing chosen by default, apply waits for every item, bulk then one change, delete note once, paged list, and long text on demand passed.');
})().catch(error => {
    console.error(error);
    process.exit(1);
});

function flat2(node, out = []) {
    if (Array.isArray(node)) node.forEach(child => flat2(child, out));
    else if (node && typeof node === 'object') {
        out.push(node);
        flat2(node.props?.children, out);
    }
    return out;
}
