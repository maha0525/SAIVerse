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

const LONG = 'x'.repeat(5000);
const ALL = ['keep', 'system', 'user', 'delete'];
const P = 'components.SystemNoticeReviewModal.';

const TIME_OPTIONS = {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
};
const LOCALE = 'ja-JP';
const when = seconds => new Date(seconds * 1000).toLocaleString(LOCALE, TIME_OPTIONS);

// The server still sends each row's tags, line_role and reason; the screen must not show them.
function row(pid, mid, actions, role = 'user', extra = {}) {
    return {
        persona_id: pid, persona_name: pid.toUpperCase(), message_id: mid, role, tags: ['tool-tag'],
        line_role: 'some_line_role', content: `body of ${mid}`, reason: 'some reason text',
        created_at: 1700000000, actions,
        version: `v-${mid}`, attachments: [], attachment_counts: {}, ...extra,
    };
}

// A group of one role as the server returns it: its actions are what every row allows.
function kind(id, rows, extra = {}) {
    const common = ALL.filter(a => rows.every(r => r.actions.includes(a)));
    return {
        kind_id: id, role: rows[0].role, count: rows.length, actions: common, system_split: false,
        rows, ...extra,
    };
}

function persona(pid, kinds) {
    return {
        persona_id: pid, persona_name: pid.toUpperCase(),
        count: kinds.reduce((n, k) => n + k.rows.length, 0), kinds,
    };
}

function listing(personas, errors = []) {
    return { total: personas.reduce((n, p) => n + p.count, 0), personas, errors };
}

function harness(initialListing) {
    const states = [], effects = [], requests = [], notices = [], questions = [];
    let cursor = 0, mounted = false, confirmed = false, closed = 0, changed = 0;
    let serverListing = initialListing;
    let applyResponse = () => ({ applied: [], rejected: [], remaining: 0 });
    let contextResponse = () => ({ ok: false, status: 500 });
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
        if (name === '@/i18n/core') return {
            t: (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key),
            getFormatLocale: () => LOCALE,
        };
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
            if (url.startsWith('/api/system/notice-review/context?')) {
                const query = Object.fromEntries(new URLSearchParams(url.slice(url.indexOf('?') + 1)));
                const result = contextResponse(query);
                if (result instanceof Error) throw result;
                return { ok: result.ok, status: result.status, json: async () => result.body };
            }
            assert.equal(url, '/api/system/notice-review');
            return { ok: true, json: async () => serverListing };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', compiled)(localRequire, module, module.exports);
    global.window = {
        confirm: question => { questions.push(question); return confirmed; },
        alert: message => notices.push(message),
    };
    const props = { onClose: () => { closed++; }, onChanged: async () => { changed++; } };
    function render() {
        cursor = 0;
        const tree = module.exports.default(props);
        mounted = true;
        return tree;
    }
    return {
        module, render, requests, notices, questions,
        get closed() { return closed; },
        get changed() { return changed; },
        async mount() { render(); effects.forEach(effect => effect()); await new Promise(setImmediate); return render(); },
        confirm(value) { confirmed = value; },
        onApply(fn) { applyResponse = fn; },
        onContext(fn) { contextResponse = fn; },
        setServer(value) { serverListing = value; },
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
const strings = tree => all(tree, node => ['span', 'p', 'li', 'h3', 'div', 'button'].includes(node.type))
    .map(node => node.props.children).flat().filter(x => typeof x === 'string');
const actionButtons = tree => all(tree, node => node.type === 'button' && /^components\.SystemNoticeReviewModal\.action/.test(text(node)));
const sections = tree => all(tree, node => node.type === 'section');
const kinds = tree => all(tree, node => node.type === 'div' && node.props.className === 'kind');
const bulkRow = tree => all(tree, node => node.type === 'div' && node.props.className === 'actions bulk')[0];
const rowItems = tree => all(tree, node => node.type === 'li' && node.props.className === 'row');
const ownButtons = item => all(item.props.children.filter(c => c && c.props?.className === 'actions'), () => true)
    .filter(node => node.type === 'button');
const pres = tree => all(tree, node => node.type === 'pre');
const expandButton = tree => all(tree, node => node.type === 'button' && node.props.className === 'expandButton')[0];
const contextButton = item => all(item, node => node.type === 'button' && node.props.className === 'contextButton')[0];
const contextItems = item => all(item, node => node.type === 'li' && /^contextItem/.test(node.props.className));
const rowTime = item => all(item, node => node.type === 'span' && node.props.className === 'rowTime')[0];
const contextGets = h => h.requests.filter(r => r.method === 'GET' && r.url.startsWith('/api/system/notice-review/context?'));
const posts = h => h.requests.filter(r => r.method === 'POST');
const params = s => JSON.parse(s.slice(s.indexOf(':') + 1));
const count = (list, value) => list.filter(s => s === value).length;

(async () => {
    // ---- Two levels: personas, then one group per role within each persona ---
    const a1 = row('a', 'm1', ALL, 'user', { content: LONG });
    const a2 = row('a', 'm2', ['keep', 'user', 'delete'], 'user', {
        attachments: [{ kind: 'audio', ref: 'saiverse://audio/a.ogg' }], attachment_counts: { audio: 1 },
        reason: 'another reason', tags: ['other-tag'], created_at: null,
    });
    const userKind = kind('k1', [a1, a2]);
    const assistantKind = kind('k2', [row('a', 'm3', ['keep', 'system', 'delete'], 'assistant')]);
    const bobKind = kind('k3', [row('b', 'm4', ALL)]);

    let h = harness(listing([persona('a', [userKind, assistantKind]), persona('b', [bobKind])]));
    let tree = await h.mount();
    // One section per persona, headed with its name and its own count.
    assert.equal(sections(tree).length, 2);
    const headers = all(tree, node => node.type === 'h3').map(text);
    assert.deepEqual(headers.map(params), [{ name: 'A', count: 3 }, { name: 'B', count: 1 }]);
    assert.ok(headers.every(s => s.startsWith(`${P}personaHeader:`)));
    const [aSection, bSection] = sections(tree);
    assert.equal(kinds(aSection).length, 2);
    assert.equal(kinds(bSection).length, 1);

    // The group heading says only whose name the rows are in and how many.
    const [first, second] = kinds(aSection);
    const firstHeader = all(first, node => node.type === 'div' && node.props.className === 'kindHeader')[0];
    assert.deepEqual(strings(firstHeader), [
        `${P}kindTitle:${JSON.stringify({ role: `${P}roleUser` })}`,
        `${P}groupCount:${JSON.stringify({ count: 2 })}`,
    ]);
    assert.ok(strings(second).includes(`${P}kindTitle:${JSON.stringify({ role: `${P}rolePersona` })}`));
    // No reason, tag or line_role is shown anywhere, on the group or on a row.
    const everything = strings(tree);
    for (const hidden of ['some reason text', 'another reason', 'tool-tag', 'other-tag', 'some_line_role']) {
        assert.equal(everything.some(s => s.includes(hidden)), false, hidden);
    }
    assert.equal(everything.some(s => /SystemNoticeReviewModal\.(reason|kindTags|kindLineRole)/.test(s)), false);
    assert.equal(all(tree, node => node.props?.className === 'kindMarks' || node.props?.className === 'reason').length, 0);

    // Every row is listed on its own, with its date and time, its full text and its own actions.
    const firstRows = rowItems(first);
    assert.equal(firstRows.length, 2);
    assert.equal(text(rowTime(firstRows[0])), when(1700000000));
    assert.ok(/2023/.test(text(rowTime(firstRows[0]))));
    assert.equal(text(rowTime(firstRows[1])), `${P}unknownTime`);
    assert.equal(pres(firstRows[0])[0].props.children, LONG);
    assert.equal(pres(firstRows[1])[0].props.children, 'body of m2');
    assert.deepEqual(ownButtons(firstRows[0]).map(text), ALL.map(a => `${P}action${a[0].toUpperCase()}${a.slice(1)}`));
    assert.deepEqual(ownButtons(firstRows[1]).map(text), [`${P}actionKeep`, `${P}actionUser`, `${P}actionDelete`]);
    // Only the row with attachments explains why "convert to official" is missing.
    assert.equal(count(strings(firstRows[0]), `${P}attachmentsNoSystem`), 0);
    assert.equal(count(strings(firstRows[1]), `${P}attachmentsNoSystem`), 1);
    assert.ok(strings(firstRows[1]).some(s => s.startsWith(`${P}attachments:`) && s.includes(`${P}kindAudio`)));
    assert.ok(strings(firstRows[1]).includes('saiverse://audio/a.ogg'));

    // The bulk row names the kind's count and offers only the actions every row allows,
    // and says that the rest are on each row's own buttons.
    const bulk = bulkRow(first);
    assert.ok(strings(bulk).includes(`${P}applyAll:${JSON.stringify({ count: 2 })}`));
    assert.deepEqual(actionButtons(bulk).map(text), [`${P}actionKeep`, `${P}actionUser`, `${P}actionDelete`]);
    assert.ok(strings(first).includes(`${P}bulkSubsetNote`));
    // A kind with a single row has no bulk row.
    assert.equal(bulkRow(second), undefined);
    assert.equal(bulkRow(bSection), undefined);

    // A bulk action asks first and names how many rows it touches; cancelling sends nothing.
    await actionButtons(bulk)[1].props.onClick();
    assert.equal(posts(h).length, 0);
    assert.match(h.questions.at(-1), /^components\.SystemNoticeReviewModal\.confirmApplyMany:/);
    assert.equal(params(h.questions.at(-1)).count, 2);

    // Confirmed, it sends every row of that kind with the version the screen saw,
    // then refreshes alerts and the list.
    h.confirm(true);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 2,
        next: listing([persona('a', [assistantKind]), persona('b', [bobKind])]) }));
    await actionButtons(bulk)[1].props.onClick();
    assert.deepEqual(posts(h).at(-1).body, { action: 'user', items: [
        { persona_id: 'a', message_id: 'm1', version: 'v-m1' },
        { persona_id: 'a', message_id: 'm2', version: 'v-m2' },
    ] });
    assert.equal(h.changed, 1);
    tree = h.render();
    assert.equal(kinds(tree).length, 2);
    assert.equal(h.closed, 0);

    // A single-row action other than delete does not ask.
    h.confirm(false);
    const asked = h.questions.length;
    h.onApply(body => ({ applied: [], rejected: body.items.map(i => ({ ...i, reason: 'changed' })), remaining: 2 }));
    await ownButtons(rowItems(tree)[0])[0].props.onClick();
    assert.equal(h.questions.length, asked);
    assert.deepEqual(posts(h).at(-1).body, { action: 'keep', items: [{ persona_id: 'a', message_id: 'm3', version: 'v-m3' }] });
    // Rows whose content changed since the list was loaded are reported as such.
    assert.match(h.notices.at(-1), /^components\.SystemNoticeReviewModal\.rejectedChanged:/);

    // Delete asks first even for one row; cancelling sends nothing.
    const before = posts(h).length;
    tree = h.render();
    await ownButtons(rowItems(tree)[0]).at(-1).props.onClick();
    assert.equal(posts(h).length, before);

    // Confirmed delete of the last rows closes the modal once nothing is left,
    // and rejected rows are reported.
    h.confirm(true);
    h.onApply(body => ({ applied: [], rejected: body.items.map(i => ({ ...i, reason: 'not_allowed' })), remaining: 0, next: listing([]) }));
    await ownButtons(rowItems(tree)[0]).at(-1).props.onClick();
    assert.deepEqual(posts(h).at(-1).body, { action: 'delete', items: [{ persona_id: 'a', message_id: 'm3', version: 'v-m3' }] });
    assert.match(h.notices.at(-1), /^components\.SystemNoticeReviewModal\.rejected:/);
    assert.equal(h.closed, 1);

    // ---- A long kind shows its first rows and opens the rest on request ------
    const many = Array.from({ length: 7 }, (_, i) => row('a', `n${i}`, ALL));
    h = harness(listing([persona('a', [kind('big', many)])]));
    tree = await h.mount();
    assert.equal(rowItems(tree).length, 5);
    let more = expandButton(tree);
    assert.equal(more.props['aria-expanded'], false);
    assert.ok(text(more).startsWith(`${P}showMore:`));
    assert.equal(params(text(more)).count, 2);
    more.props.onClick();
    tree = h.render();
    assert.deepEqual(rowItems(tree).map(li => pres(li)[0].props.children), many.map(r => r.content));
    // Rows past the first five also show their date and time and their own context button.
    assert.ok(rowItems(tree).every(li => text(rowTime(li)) === when(1700000000) && contextButton(li)));
    more = expandButton(tree);
    assert.equal(more.props['aria-expanded'], true);
    assert.ok(text(more).startsWith(`${P}showLess:`));
    // The bulk action still covers every row of the kind, shown or not.
    h.confirm(true);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 0, next: listing([]) }));
    more.props.onClick();
    tree = h.render();
    assert.equal(rowItems(tree).length, 5);
    await actionButtons(bulkRow(tree))[0].props.onClick();
    assert.equal(posts(h).at(-1).body.items.length, 7);
    assert.equal(params(h.questions.at(-1)).count, 7);

    // ---- Notes that hold for every row are said once, on the kind ------------
    const audio = extra => ({
        attachments: [{ kind: 'audio', ref: 'saiverse://audio/v.ogg' }], attachment_counts: { audio: 1 }, ...extra,
    });
    const blocked = kind('blocked', [
        row('a', 'b1', ['keep', 'user', 'delete'], 'user', audio()),
        row('a', 'b2', ['keep', 'user', 'delete'], 'user', audio()),
    ]);
    const split = kind('split', [
        row('a', 's1', ALL, 'user', audio({ system_split: true })),
        row('a', 's2', ALL, 'user', audio({ system_split: true })),
    ], { system_split: true });
    h = harness(listing([persona('a', [blocked, split])]));
    tree = await h.mount();
    const [blockedKind, splitKind] = kinds(tree);
    assert.equal(count(strings(blockedKind), `${P}attachmentsNoSystem`), 1);
    assert.equal(count(strings(blockedKind), `${P}systemSplitNote`), 0);
    assert.equal(count(strings(splitKind), `${P}systemSplitNote`), 1);
    assert.equal(count(strings(splitKind), `${P}attachmentsNoSystem`), 0);
    assert.deepEqual(actionButtons(bulkRow(splitKind)).map(text), ALL.map(a => `${P}action${a[0].toUpperCase()}${a.slice(1)}`));
    assert.equal(strings(splitKind).includes(`${P}bulkSubsetNote`), false);

    // When split and whole-row rows are mixed, the server withholds the bulk
    // "convert to official"; the split row explains itself on its own.
    const mixed = kind('mixed', [
        row('a', 'x1', ALL, 'user', audio({ system_split: true })),
        row('a', 'x2', ALL),
    ], { actions: ['keep', 'user', 'delete'] });
    h = harness(listing([persona('a', [mixed])]));
    tree = await h.mount();
    const mixedRows = rowItems(tree);
    assert.equal(count(strings(mixedRows[0]), `${P}systemSplitNote`), 1);
    assert.equal(count(strings(mixedRows[1]), `${P}systemSplitNote`), 0);
    assert.equal(actionButtons(bulkRow(tree)).map(text).includes(`${P}actionSystem`), false);
    assert.ok(ownButtons(mixedRows[0]).map(text).includes(`${P}actionSystem`));
    assert.ok(strings(tree).includes(`${P}bulkSubsetNote`));

    // ---- Surrounding messages, fetched only when asked ----------------------
    h = harness(listing([persona('a', [kind('ctx', [row('a', 'c4', ALL), row('a', 'c9', ALL)])])]));
    tree = await h.mount();
    assert.equal(contextGets(h).length, 0);
    assert.equal(contextItems(tree).length, 0);
    let ctxButton = contextButton(rowItems(tree)[0]);
    assert.equal(text(ctxButton), `${P}contextShow`);
    assert.equal(ctxButton.props['aria-expanded'], false);
    const around = [
        { message_id: 'c3', role: 'assistant', content: 'before', created_at: 1700000000 - 60, target: false },
        { message_id: 'c4', role: 'user', content: 'body of c4', created_at: 1700000000, target: true },
        { message_id: 'c5', role: 'system', content: LONG, created_at: null, target: false },
    ];
    h.onContext(query => ({
        ok: true, status: 200,
        body: { persona_id: query.persona_id, message_id: query.message_id, messages: around },
    }));
    await ctxButton.props.onClick();
    assert.equal(contextGets(h).length, 1);
    assert.deepEqual(
        Object.fromEntries(new URLSearchParams(contextGets(h)[0].url.split('?')[1])),
        { persona_id: 'a', message_id: 'c4' },
    );
    tree = h.render();
    const [openRow, otherRow] = rowItems(tree);
    ctxButton = contextButton(openRow);
    assert.equal(text(ctxButton), `${P}contextHide`);
    assert.equal(ctxButton.props['aria-expanded'], true);
    // Only the row that was asked shows its surroundings: who, when and what, in time order.
    assert.equal(contextItems(otherRow).length, 0);
    const items = contextItems(openRow);
    assert.deepEqual(items.map(li => pres(li)[0].props.children), ['before', 'body of c4', LONG]);
    assert.deepEqual(items.map(li => strings(li)), [
        [`${P}rolePersona`, when(1700000000 - 60)],
        [`${P}roleUser`, when(1700000000)],
        [`${P}roleSystem`, `${P}unknownTime`],
    ]);
    assert.deepEqual(items.map(li => li.props.className.includes('contextTarget')), [false, true, false]);
    assert.ok(items.every(li => pres(li)[0].props.className === 'contextContent'));
    // Pressing again closes it without asking the server.
    await ctxButton.props.onClick();
    tree = h.render();
    assert.equal(contextItems(tree).length, 0);
    assert.equal(contextGets(h).length, 1);
    assert.equal(text(contextButton(rowItems(tree)[0])), `${P}contextShow`);
    // A failed fetch says so under that row only.
    h.onContext(() => ({ ok: false, status: 500 }));
    await contextButton(rowItems(tree)[1]).props.onClick();
    tree = h.render();
    assert.equal(contextGets(h).length, 2);
    assert.ok(strings(rowItems(tree)[1]).includes(`${P}contextFailed`));
    assert.equal(strings(rowItems(tree)[0]).includes(`${P}contextFailed`), false);
    // So does a network error; reopening fetches again.
    await contextButton(rowItems(tree)[1]).props.onClick();
    h.onContext(() => new Error('offline'));
    tree = h.render();
    await contextButton(rowItems(tree)[1]).props.onClick();
    tree = h.render();
    assert.equal(contextGets(h).length, 3);
    assert.ok(strings(rowItems(tree)[1]).includes(`${P}contextFailed`));
    // Settling a row closes the surroundings fetched before it.
    h.onContext(query => ({ ok: true, status: 200, body: { messages: around } }));
    await contextButton(rowItems(tree)[0]).props.onClick();
    tree = h.render();
    assert.ok(contextItems(tree).length > 0);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 1 }));
    await ownButtons(rowItems(tree)[1])[0].props.onClick();
    tree = h.render();
    assert.equal(contextItems(tree).length, 0);

    // ---- Unreadable memories ------------------------------------------------
    // While some personas' memories could not be read, settling the last visible
    // row does not close the modal; the unreadable personas are named and a retry
    // reloads the list.
    const unreadable = [{ persona_id: 'c', persona_name: 'Carol', error: 'locked' }];
    h = harness(listing([persona('a', [blocked])], unreadable));
    tree = await h.mount();
    assert.ok(all(tree, node => node.type === 'p').some(node => String(node.props.children).includes('Carol')));
    h.confirm(true);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 0, next: listing([], unreadable) }));
    await actionButtons(bulkRow(tree)).at(-1).props.onClick();
    assert.equal(h.closed, 0);
    tree = h.render();
    const retry = all(tree, node => node.type === 'button' && text(node) === `${P}retry`)[0];
    assert.ok(retry);
    const gets = () => h.requests.filter(r => r.method === 'GET').length;
    const loads = gets();
    h.setServer(listing([]));
    await retry.props.onClick();
    assert.equal(gets(), loads + 1);
    assert.equal(all(h.render(), node => node.type === 'p').some(node => String(node.props.children).includes('Carol')), false);

    console.log('Notice review modal: personas then role groups, no reasons/tags/line_role, every row with date and full text, surrounding messages on demand, bulk limited to common actions, show more, notes once per group, counted confirmation, versions, changed rows, unreadable memories, delete confirmation, and close-when-empty passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
