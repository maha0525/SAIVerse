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

function row(pid, mid, actions, role = 'user', extra = {}) {
    return {
        persona_id: pid, persona_name: pid.toUpperCase(), message_id: mid, role, tags: ['tool'],
        content: 'Email sent.', reason: 'r', created_at: 1000, actions,
        version: `v-${mid}`, attachments: [], attachment_counts: {}, ...extra,
    };
}

function listing(groups, errors = []) {
    return { total: groups.reduce((n, g) => n + g.rows.length, 0), groups, errors };
}

function harness(initialListing) {
    const states = [], effects = [], requests = [], notices = [], questions = [];
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
        if (name === '@/i18n/core') return {
            t: (key, params) => (params ? `${key}:${JSON.stringify(params)}` : key),
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
const actionButtons = tree => all(tree, node => node.type === 'button' && /^components\.SystemNoticeReviewModal\.action/.test(text(node)));
const sections = tree => all(tree, node => node.type === 'section');
const pres = tree => all(tree, node => node.type === 'pre');
const posts = h => h.requests.filter(r => r.method === 'POST');

(async () => {
    const userable = { group_id: 'g1', role: 'user', head: 'Email sent.', tags: ['tool'], reason: 'r',
        actions: ['keep', 'system', 'user', 'delete'], count: 2,
        personas: [{ persona_id: 'a', persona_name: 'A', count: 1 }, { persona_id: 'b', persona_name: 'B', count: 1 }],
        rows: [row('a', 'm1', ['keep', 'system', 'user', 'delete'], 'user', { content: LONG }),
            row('b', 'm2', ['keep', 'system', 'user', 'delete'])] };
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
    // The full text is shown, not a cut-off preview.
    assert.equal(pres(first)[0].props.children, LONG);

    // A group action asks first and names how many rows it touches; cancelling sends nothing.
    await actionButtons(first)[1].props.onClick();
    assert.equal(posts(h).length, 0);
    assert.match(h.questions.at(-1), /^components\.SystemNoticeReviewModal\.confirmApplyMany:/);
    assert.equal(JSON.parse(h.questions.at(-1).split(':').slice(1).join(':')).count, 2);

    // Confirmed, it sends every row of that group with the version the screen saw,
    // then refreshes alerts and the list.
    h.confirm(true);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 1, next: listing([persona]) }));
    await actionButtons(first)[1].props.onClick();
    assert.deepEqual(posts(h).at(-1).body, { action: 'system', items: [
        { persona_id: 'a', message_id: 'm1', version: 'v-m1' },
        { persona_id: 'b', message_id: 'm2', version: 'v-m2' },
    ] });
    assert.equal(h.changed, 1);
    tree = h.render();
    assert.equal(sections(tree).length, 1);
    assert.equal(h.closed, 0);

    // A single-row action other than delete does not ask.
    h.confirm(false);
    const expand = all(sections(tree)[0], node => node.type === 'button' && 'aria-expanded' in node.props)[0];
    expand.props.onClick();
    tree = h.render();
    let rowItems = all(tree, node => node.type === 'li');
    assert.equal(rowItems.length, 1);
    const asked = h.questions.length;
    h.onApply(body => ({ applied: [], rejected: body.items.map(i => ({ ...i, reason: 'changed' })), remaining: 1 }));
    await actionButtons(rowItems[0])[0].props.onClick();
    assert.equal(h.questions.length, asked);
    // Rows whose content changed since the list was loaded are reported as such.
    assert.match(h.notices.at(-1), /^components\.SystemNoticeReviewModal\.rejectedChanged:/);

    // Delete asks first even for one row; cancelling sends nothing.
    const before = posts(h).length;
    tree = h.render();
    rowItems = all(tree, node => node.type === 'li');
    await actionButtons(rowItems[0]).at(-1).props.onClick();
    assert.equal(posts(h).length, before);

    // Confirmed delete of the last row closes the modal once nothing is left,
    // and rejected rows are reported.
    h.confirm(true);
    h.onApply(body => ({ applied: [], rejected: body.items.map(i => ({ ...i, reason: 'not_allowed' })), remaining: 0, next: listing([]) }));
    await actionButtons(rowItems[0]).at(-1).props.onClick();
    assert.deepEqual(posts(h).at(-1).body, { action: 'delete', items: [{ persona_id: 'a', message_id: 'm3', version: 'v-m3' }] });
    assert.match(h.notices.at(-1), /^components\.SystemNoticeReviewModal\.rejected:/);
    assert.equal(h.closed, 1);

    // Attachments are listed with kinds and counts, and a group whose rows carry
    // attachments explains why "convert to official" is missing.
    const withAudio = { group_id: 'g3', role: 'user', head: 'voice', tags: [], reason: 'r',
        actions: ['keep', 'user', 'delete'], count: 1,
        personas: [{ persona_id: 'a', persona_name: 'A', count: 1 }],
        rows: [row('a', 'm4', ['keep', 'user', 'delete'], 'user', {
            attachments: [{ kind: 'audio', ref: 'saiverse://audio/voice.ogg' }],
            attachment_counts: { audio: 1 },
        })] };
    h = harness(listing([withAudio]));
    tree = await h.mount();
    const strings = all(tree, node => node.type === 'span' || node.type === 'p' || node.type === 'li')
        .map(node => node.props.children).flat().filter(x => typeof x === 'string');
    assert.ok(strings.some(s => s.startsWith('components.SystemNoticeReviewModal.attachments:')
        && s.includes('components.SystemNoticeReviewModal.kindAudio')));
    assert.ok(strings.includes('saiverse://audio/voice.ogg'));
    assert.ok(strings.includes('components.SystemNoticeReviewModal.attachmentsNoSystem'));
    assert.equal(strings.includes('components.SystemNoticeReviewModal.systemSplitNote'), false);

    // A voice-input group whose "convert to official" splits the row into a
    // notice and the user's audio says so, instead of the attachments note.
    const splitActions = ['keep', 'system', 'user', 'delete'];
    const voiceSplit = { ...withAudio, group_id: 'g4', actions: splitActions, system_split: true,
        rows: [row('a', 'm5', splitActions, 'user', {
            attachments: [{ kind: 'audio', ref: 'saiverse://audio/voice2.ogg' }],
            attachment_counts: { audio: 1 }, system_split: true,
        })] };
    h = harness(listing([voiceSplit]));
    tree = await h.mount();
    const splitStrings = all(tree, node => node.type === 'span' || node.type === 'p' || node.type === 'li')
        .map(node => node.props.children).flat().filter(x => typeof x === 'string');
    assert.ok(splitStrings.includes('components.SystemNoticeReviewModal.systemSplitNote'));
    assert.equal(splitStrings.includes('components.SystemNoticeReviewModal.attachmentsNoSystem'), false);

    // While some personas' memories could not be read, settling the last visible
    // row does not close the modal; the unreadable personas are named and a retry
    // reloads the list.
    const unreadable = [{ persona_id: 'c', persona_name: 'Carol', error: 'locked' }];
    h = harness(listing([withAudio], unreadable));
    tree = await h.mount();
    assert.ok(all(tree, node => node.type === 'p').some(node => String(node.props.children).includes('Carol')));
    h.confirm(true);
    h.onApply(body => ({ applied: body.items, rejected: [], remaining: 0, next: listing([], unreadable) }));
    await actionButtons(sections(tree)[0]).at(-1).props.onClick();
    assert.equal(h.closed, 0);
    tree = h.render();
    const retry = all(tree, node => node.type === 'button' && text(node) === 'components.SystemNoticeReviewModal.retry')[0];
    assert.ok(retry);
    const gets = () => h.requests.filter(r => r.method === 'GET').length;
    const loads = gets();
    h.setServer(listing([]));
    await retry.props.onClick();
    assert.equal(gets(), loads + 1);
    assert.equal(all(h.render(), node => node.type === 'p').some(node => String(node.props.children).includes('Carol')), false);

    console.log('Notice review modal: per-group actions, full text, counted confirmation, versions, changed rows, attachments, unreadable memories, delete confirmation, and close-when-empty passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
