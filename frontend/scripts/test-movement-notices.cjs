// Real TS/TSX with isolated React hooks and fake HTTP. No backend, persona or LLM calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const messages = require('../src/i18n/messages.json');
const src = path.resolve(__dirname, '../src');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (data, ok = true) => ({ ok, status: ok ? 200 : 500, json: async () => data, headers: new Headers() });
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return { promise, resolve, reject };
};
function environment() {
    const window = new EventTarget();
    const document = new EventTarget();
    document.visibilityState = 'visible';
    const timers = new Map();
    let next = 0;
    window.setInterval = fn => { timers.set(++next, fn); return next; };
    window.clearInterval = id => timers.delete(id);
    return {
        window, document, modules: new Map(),
        poll: () => { for (const fn of [...timers.values()]) fn(); },
        focus: () => window.dispatchEvent(new Event('focus')),
        timerCount: () => timers.size,
    };
}
function mount(relative, initialProps, apiFetch, env = environment(), locale = 'ja') {
    let props = initialProps, tree, html, dirty = false, pending = [];
    const states = [], refs = [], effects = [], memos = [];
    const changed = (a, b) => !a || !b || a.length !== b.length || b.some((v, i) => !Object.is(v, a[i]));
    const shim = {
        ...React,
        useState(initial) {
            const i = shim.s++;
            if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial;
            return [states[i], value => {
                const v = typeof value === 'function' ? value(states[i]) : value;
                if (!Object.is(v, states[i])) { states[i] = v; dirty = true; }
            }];
        },
        useRef(initial) { const i = shim.r++; return refs[i] ||= { current: initial }; },
        useEffect(fn, deps) { pending.push({ i: shim.e++, fn, deps }); },
        useCallback(fn, deps) {
            const i = shim.m++;
            if (!memos[i] || changed(memos[i].deps, deps)) memos[i] = { fn, deps };
            return memos[i].fn;
        },
    };
    const cache = new Map();
    function load(file) {
        const shared = /\/lib\/(movementNotices|buildingSettingsSave)\.ts$/.test(file);
        if (shared && env.modules.has(file)) return env.modules.get(file);
        if (cache.has(file)) return cache.get(file);
        const module = { exports: {} };
        const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
        }).outputText;
        const localRequire = name => {
            if (name === 'react') return shim;
            if (name === '@/i18n/core') return { t: (key, vars) => messages[key][locale].replace(/\{(\w+)\}/g, (_, k) => vars?.[k] ?? '') };
            if (name === '@/i18n/useLocale') return { useLocale: () => locale };
            if (name === '@/i18n/api') return { apiFetch };
            if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
            if (name.endsWith('/ImageUpload')) return { __esModule: true, default: () => null };
            if (name.startsWith('@/') || name.startsWith('.')) {
                const base = name.startsWith('@/') ? path.join(src, name.slice(2)) : path.resolve(path.dirname(file), name);
                return load(['.ts', '.tsx'].map(ext => base + ext).find(candidate => fs.existsSync(candidate)));
            }
            return require(name);
        };
        new Function('require', 'module', 'exports', 'window', 'document', 'CustomEvent', 'alert', output)(
            localRequire, module, module.exports, env.window, env.document, CustomEvent, () => { throw Error('Unexpected alert'); },
        );
        cache.set(file, module.exports);
        if (shared) env.modules.set(file, module.exports);
        return module.exports;
    }
    const exports = load(path.join(src, relative));
    const Component = exports.default || (props => {
        if (exports.useMovementNoticeSettings) return React.createElement('pre', {}, JSON.stringify(exports.useMovementNoticeSettings().settings));
        exports.useMovementNoticeAutoScroll(props); return null;
    });
    function render() {
        shim.s = shim.r = shim.e = shim.m = 0;
        pending = [];
        dirty = false;
        tree = Component(props);
        html = renderToStaticMarkup(tree);
        for (const { i, fn, deps } of pending) {
            if (!effects[i] || changed(effects[i].deps, deps)) {
                effects[i]?.cleanup?.();
                effects[i] = { deps, cleanup: fn() };
            }
        }
    }
    render();
    return {
        env,
        get tree() { return tree; },
        get html() { return html; },
        load: name => load(path.join(src, name)),
        update(nextProps) { props = { ...props, ...nextProps }; render(); },
        async settle() {
            for (let i = 0; i < 8; i++) { await tick(); if (dirty) render(); }
        },
        unmount() { for (const effect of effects) effect?.cleanup?.(); },
    };
}
function find(node, predicate) {
    if (!React.isValidElement(node)) return null;
    if (predicate(node)) return node;
    return React.Children.toArray(node.props.children).map(child => find(child, predicate)).find(Boolean);
}
const select = view => find(view.tree, e => e.type === 'select' && /movement-notices$/.test(e.props.id));
const button = (view, key) => find(view.tree, e => e.type === 'button' && e.props['data-i18n']?.split(' ').includes(key));
const noNetwork = () => { throw new Error('Unexpected HTTP request'); };

async function testFilteringAndPagination() {
    let rows = Object.freeze(Array.from({ length: 20 }, (_, i) => Object.freeze({ id: `m${20 + i}`, role: 'host', is_movement_notice: true, building_id: 'a' })));
    const env = environment();
    const requests = [];
    const view = mount('components/HistoryContinuation.tsx', {
        oldestId: rows[0].id, hasMore: true, ready: true, loading: false,
        onLoad: async before => { requests.push(before); },
    }, noNetwork, env);
    const { shouldShowMovementMessage: visible, applyMovementNoticeChange: apply } = view.load('lib/movementNotices.ts');
    const host = { role: 'host', is_movement_notice: true };
    for (const global of [false, true]) {
        for (const override of [null, false, true]) {
            const settings = { show_movement_notices: global, building_overrides: override === null ? {} : { a: override } };
            assert.equal(visible(host, settings, 'a'), override ?? global);
        }
    }
    const hidden = { show_movement_notices: false, building_overrides: { a: false, b: true } };
    assert.equal(visible({ ...host, building_id: 'b' }, hidden, 'a'), true, 'game rows use their source Building');
    assert.equal(visible({ ...host, building_id: 'a' }, hidden, 'b'), false);
    for (const message of [
        { role: 'user', content: 'entered the room', is_movement_notice: false },
        { role: 'assistant', content: 'entered the room', is_movement_notice: true },
        { role: 'host', content: 'left the room' }, // unclassified legacy text stays
        { role: 'system', isWarning: true },
        { role: 'system', isInfo: true, is_movement_notice: false },
        { role: 'host', content: 'picked up an item', is_movement_notice: false },
    ]) assert.equal(visible(message, hidden, 'a'), true);
    assert.equal(visible(host, null, 'a'), true, 'unloaded/failed settings preserve the default');
    assert.equal(visible(host, hidden, 'toString'), false, 'only explicit boolean overrides count');
    const cleared = apply(hidden, { buildingId: 'a', value: null });
    assert.equal(Object.hasOwn(cleared.building_overrides, 'a'), false);
    assert.equal(hidden.building_overrides.a, false, 'updates do not mutate prior settings');
    assert.equal(apply(hidden, { buildingId: 'a', value: true }).building_overrides.a, true);
    assert.equal(rows.filter(message => visible(message, hidden, 'a')).length, 0);
    assert.ok(view.html.includes(messages['movementNotices.loadOlder'].ja), 'hidden-only page keeps an accessible continuation');
    button(view, 'movementNotices.loadOlder').props.onClick();
    button(view, 'movementNotices.loadOlder').props.onClick();
    assert.deepEqual(requests, ['m20'], 'double clicks use one original cursor');
    await view.settle();
    const newer = rows;
    rows = Object.freeze([...Array.from({ length: 20 }, (_, i) => Object.freeze({ ...host, id: `m${i}`, building_id: 'a' })), ...rows]);
    view.update({ oldestId: rows[0].id });
    assert.equal(rows.filter(message => visible(message, hidden, 'a')).length, 0);
    button(view, 'movementNotices.loadOlder').props.onClick();
    await view.settle();
    assert.deepEqual(requests, ['m20', 'm0'], 'another hidden-only page advances using its unfiltered cursor');
    const polled = Object.freeze({ ...host, id: 'm40', building_id: 'a' });
    rows = Object.freeze([...rows, polled]);
    assert.equal(visible(polled, hidden, 'a'), false, 'polling and initial records share classification');
    assert.equal(rows.at(-1).id, 'm40', 'hidden newest record remains available to the next after poll');
    const shown = { show_movement_notices: true, building_overrides: {} };
    assert.deepEqual(rows.filter(message => visible(message, shown, 'a')), rows, 'show restores every loaded record without reload');
    assert.equal(rows[20], newer[0], 'message identity and order are unchanged');
    view.update({ hasMore: false });
    assert.equal(view.html, '', 'server has_more still controls termination');
    view.unmount();
    const page = fs.readFileSync(path.join(src, 'app/page.tsx'), 'utf8');
    assert.ok(page.includes('oldestId={messages[0]?.id}'));
    assert.match(page, /messages\.map\(\(msg, idx\) => \{\s*\/\/[^\n]+\n\s*if \(!shouldShowMovementMessage\(msg, movementNoticeSettings, currentBuildingId\)\) return null;/);
    assert.equal((page.match(/shouldShowMovementMessage\(/g) || []).length, 1, 'filter is only at the render boundary');
}

async function testAutoScroll() {
    const first = { id: 'hello', role: 'assistant' };
    const hidden = { show_movement_notices: false, building_overrides: {} };
    const scrolls = [];
    const endRef = { current: { scrollIntoView: options => scrolls.push(options) } };
    let rows = [first];
    const view = mount('hooks/useMovementNoticeAutoScroll.ts', { messages: rows, settings: hidden, currentBuildingId: 'a', ready: true, loadingOlder: false, endRef }, noNetwork);
    rows = [...rows, { id: 'entered', role: 'host', is_movement_notice: true }];
    view.update({ messages: rows });
    assert.equal(scrolls.length, 0, 'hidden-only after polling never scrolls');
    view.update({ settings: { show_movement_notices: true, building_overrides: {} } });
    assert.equal(scrolls.length, 0, 'showing loaded movement notices never scrolls');
    view.update({ settings: hidden });
    rows = [...rows, { id: 'reply', role: 'assistant' }];
    view.update({ messages: rows });
    assert.equal(scrolls.length, 1, 'visible new conversation still scrolls');
    rows = [{ id: 'old', role: 'assistant' }, ...rows];
    view.update({ messages: rows });
    assert.equal(scrolls.length, 1, 'older pages do not scroll to the bottom');
    rows = [...rows, { id: 'item', role: 'host', is_movement_notice: false }];
    view.update({ messages: rows });
    assert.equal(scrolls.length, 2, 'unrelated system notices retain their scroll behavior');
    view.unmount();
}

async function testSaveAcrossRemounts() {
    // Both server-commit delay and response-only delay remain serialized across modal instances.
    for (const commitEarly of [false, true]) {
        const env = environment();
        let saved = { show_movement_notices: true, building_overrides: {} };
        const writes = [], reads = [];
        const api = async (url, init) => {
            if (init?.method === 'PUT') {
                const value = JSON.parse(init.body).show_movement_notices;
                const pending = deferred();
                writes.push({ ...pending, value });
                if (commitEarly) saved = { ...saved, show_movement_notices: value };
                return pending.promise;
            }
            reads.push(url); return response(saved);
        };
        const old = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
        await old.settle();
        select(old).props.onChange({ target: { value: 'hide' } });
        old.unmount();
        const reopened = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
        await reopened.settle();
        const readCount = reads.length;
        assert.equal(select(reopened).props.disabled, true, 'pending save disables reopened control');
        select(reopened).props.onChange({ target: { value: 'show' } });
        env.focus(); env.poll();
        await reopened.settle();
        assert.equal(writes.length, 1, 'cannot send a newer write until the older response arrives');
        assert.equal(reads.length, readCount, 'focus/poll cannot read a pre-commit snapshot');
        saved = { ...saved, show_movement_notices: false };
        writes[0].resolve(response(saved));
        await reopened.settle();
        assert.equal(select(reopened).props.value, 'hide');
        assert.equal(select(reopened).props.disabled, false);
        select(reopened).props.onChange({ target: { value: 'show' } });
        assert.equal(writes.length, 2);
        saved = { ...saved, show_movement_notices: true };
        writes[1].resolve(response(saved));
        await reopened.settle();
        assert.equal(select(reopened).props.value, 'show');
        assert.equal(saved.show_movement_notices, true);
        reopened.unmount();
    }
}

async function testFailedGlobalSaveAfterRemount() {
    const env = environment();
    let write;
    const api = async (url, init) => {
        if (init?.method === 'PUT') { write = deferred(); return write.promise; }
        return response({ show_movement_notices: true, building_overrides: {} });
    };
    const old = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
    await old.settle();
    select(old).props.onChange({ target: { value: 'hide' } });
    old.unmount();
    const reopened = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
    write.reject(new Error('isolated connection failure'));
    await reopened.settle();
    assert.equal(select(reopened).props.value, 'show');
    assert.equal(select(reopened).props.disabled, false);
    assert.ok(reopened.html.includes(messages['movementNotices.saveError'].ja));
    reopened.unmount();
}

async function testGlobalBuildingOverlap() {
    const env = environment();
    let saved = { show_movement_notices: true, building_overrides: { a: false } };
    let write;
    let delayRead = false;
    const reads = [];
    const api = async (url, init) => {
        if (init?.method === 'PUT') { write = deferred(); return write.promise; }
        if (delayRead) { const read = deferred(); reads.push(read); return read.promise; }
        return response(saved);
    };
    const chat = mount('hooks/useMovementNoticeSettings.ts', {}, api, env);
    const control = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
    await chat.settle(); await control.settle();
    select(control).props.onChange({ target: { value: 'hide' } });
    control.unmount();
    const globalResponseSnapshot = { show_movement_notices: false, building_overrides: { a: false } };
    saved = { show_movement_notices: false, building_overrides: { a: true } };
    chat.load('lib/movementNotices.ts').announceMovementNoticeChange({ buildingId: 'a', value: true });
    await chat.settle();
    assert.equal(JSON.parse(chat.tree.props.children).building_overrides.a, true);
    delayRead = true;
    write.resolve(response(globalResponseSnapshot));
    await chat.settle();
    assert.equal(JSON.parse(chat.tree.props.children).show_movement_notices, false);
    assert.equal(JSON.parse(chat.tree.props.children).building_overrides.a, true, 'old global response never replaces a newer Building override');
    reads.at(-1).resolve(response(saved));
    await chat.settle();
    assert.deepEqual(JSON.parse(chat.tree.props.children), saved);
    chat.unmount();
}

async function testGlobalControl() {
    const env = environment();
    let saved = { show_movement_notices: true, building_overrides: { a: false } };
    let failRead = true;
    let write;
    const calls = [], changes = [];
    env.window.addEventListener('saiverse:movement-notices-changed', event => changes.push(event.detail));
    const api = async (url, init) => {
        calls.push({ url, init });
        assert.equal(url, '/api/config/movement-notices');
        if (init?.method === 'PUT') { write = deferred(); return write.promise; }
        return response(saved, !failRead);
    };
    const view = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env);
    await view.settle();
    assert.ok(view.html.includes('role="alert"'));
    assert.equal(select(view).props.disabled, true);
    failRead = false;
    button(view, 'movementNotices.retry').props.onClick();
    await view.settle();
    assert.equal(select(view).props.value, 'show');
    assert.equal(select(view).props.disabled, false);
    select(view).props.onChange({ target: { value: 'hide' } });
    select(view).props.onChange({ target: { value: 'hide' } });
    await view.settle();
    assert.equal(calls.filter(c => c.init?.method === 'PUT').length, 1);
    assert.deepEqual(JSON.parse(calls.at(-1).init.body), { show_movement_notices: false });
    assert.equal(select(view).props.disabled, true);
    saved = { ...saved, show_movement_notices: false };
    write.resolve(response(saved));
    await view.settle();
    assert.equal(select(view).props.value, 'hide');
    assert.equal(changes.length, 1);
    assert.deepEqual(changes[0], { globalValue: false });
    select(view).props.onChange({ target: { value: 'show' } });
    write.resolve(response({}, false));
    await view.settle();
    assert.equal(select(view).props.value, 'hide');
    assert.ok(view.html.includes(messages['movementNotices.saveError'].ja));
    assert.equal(changes.length, 1, 'failed saves never publish a new choice');
    select(view).props.onChange({ target: { value: 'show' } });
    saved = { ...saved, show_movement_notices: true };
    write.resolve(response(saved));
    await view.settle();
    assert.equal(select(view).props.value, 'show');
    assert.ok(!view.html.includes(messages['movementNotices.saveError'].ja));
    // Other clients are reflected by the read-only timer, focus and visibility refresh.
    saved = { ...saved, show_movement_notices: false };
    env.poll();
    await view.settle();
    assert.equal(select(view).props.value, 'hide');
    saved = { ...saved, show_movement_notices: true };
    env.focus();
    await view.settle();
    assert.equal(select(view).props.value, 'show');
    saved = { ...saved, show_movement_notices: false };
    env.document.dispatchEvent(new Event('visibilitychange'));
    await view.settle();
    assert.equal(select(view).props.value, 'hide');
    // Close during a save: publish the persisted setting, but don't act on a closed UI.
    select(view).props.onChange({ target: { value: 'show' } });
    view.unmount();
    assert.equal(env.timerCount(), 0);
    saved = { ...saved, show_movement_notices: true };
    write.resolve(response(saved));
    await tick(); await tick();
    assert.equal(changes.at(-1).globalValue, true);
    const reopened = mount('components/settings/MovementNoticeSetting.tsx', {}, api, env, 'en');
    await reopened.settle();
    assert.equal(select(reopened).props.value, 'show');
    assert.ok(reopened.html.includes('Entry and exit notices'));
    assert.ok(!reopened.html.includes('入退室'));
    reopened.unmount();
}

async function testReadRaces() {
    const env = environment();
    const reads = [];
    const view = mount('components/settings/MovementNoticeSetting.tsx', {}, (url, init) => {
        assert.notEqual(init?.method, 'PUT');
        const read = deferred(); reads.push({ ...read, signal: init.signal }); return read.promise;
    }, env);
    env.focus();
    assert.equal(reads[0].signal.aborted, true);
    reads[1].resolve(response({ show_movement_notices: false, building_overrides: {} }));
    await view.settle();
    reads[0].resolve(response({ show_movement_notices: true, building_overrides: {} }));
    await view.settle();
    assert.equal(select(view).props.value, 'hide', 'older fetch cannot override newer fetch');
    env.poll();
    view.load('lib/movementNotices.ts').announceMovementNoticeChange({ settings: { show_movement_notices: true, building_overrides: { b: true } } });
    reads[2].resolve(response({ show_movement_notices: false, building_overrides: {} }));
    await view.settle();
    assert.equal(select(view).props.value, 'show', 'pre-save read cannot undo a successful save');
    view.unmount();
    assert.equal(env.timerCount(), 0);
}

async function testBuildingControl() {
    const env = environment();
    const buildings = [
        { BUILDINGID: 'a', BUILDINGNAME: 'Room A', SHOW_MOVEMENT_NOTICES: null },
        { BUILDINGID: 'b', BUILDINGNAME: 'Room B', SHOW_MOVEMENT_NOTICES: false },
    ];
    const calls = [], changes = [];
    let write, closes = 0, savedCallbacks = 0;
    env.window.addEventListener('saiverse:movement-notices-changed', event => changes.push(event.detail));
    const api = async (url, init) => {
        calls.push({ url, init });
        if (init?.method === 'PUT') { write = deferred(); return write.promise; }
        if (url.startsWith('/api/db/tables/building?')) return response(buildings.map(b => ({ ...b })));
        return response([]);
    };
    const props = { isOpen: true, buildingId: 'a', onClose: () => closes++, onSaved: () => savedCallbacks++ };
    const view = mount('components/BuildingSettingsModal.tsx', props, api, env);
    await view.settle();
    assert.equal(select(view).props.value, 'inherit');
    const opts = React.Children.toArray(select(view).props.children).map(o => o.props.children);
    assert.deepEqual(opts, ['グローバル設定に合わせる', '表示', '非表示']);
    const saveKey = 'components.BuildingSettingsModal.text034';
    const choose = async value => { select(view).props.onChange({ target: { value } }); await view.settle(); };
    await choose('hide');
    button(view, saveKey).props.onClick();
    button(view, saveKey).props.onClick();
    assert.equal(calls.filter(c => c.init?.method === 'PUT').length, 1);
    assert.equal(calls.at(-1).url, '/api/world/buildings/a');
    assert.equal(JSON.parse(calls.at(-1).init.body).show_movement_notices, false);
    write.resolve(response({}, false));
    await view.settle();
    assert.equal(select(view).props.value, 'hide', 'failure retains the editable choice for retry');
    assert.ok(view.html.includes('role="alert"'));
    assert.equal(closes, 0); assert.equal(changes.length, 0);
    button(view, saveKey).props.onClick();
    buildings[0].SHOW_MOVEMENT_NOTICES = false;
    write.resolve(response({}));
    await view.settle();
    assert.equal(closes, 1); assert.equal(savedCallbacks, 1);
    assert.deepEqual(changes[0], { buildingId: 'a', value: false });
    view.update({ isOpen: false });
    view.update({ isOpen: true });
    await view.settle();
    assert.equal(select(view).props.value, 'hide', 'reopen reads the persisted value');
    await choose('show');
    button(view, saveKey).props.onClick();
    assert.equal(JSON.parse(calls.at(-1).init.body).show_movement_notices, true);
    // Navigating while saving must not close/overwrite the newly selected room.
    view.update({ buildingId: 'b' });
    await view.settle();
    assert.equal(select(view).props.value, 'hide');
    write.resolve(response({}));
    await view.settle();
    assert.equal(closes, 1); assert.equal(savedCallbacks, 1);
    assert.deepEqual(changes.at(-1), { buildingId: 'a', value: true });
    assert.equal(select(view).props.value, 'hide');
    await choose('inherit');
    button(view, saveKey).props.onClick();
    assert.equal(calls.at(-1).url, '/api/world/buildings/b');
    assert.equal(JSON.parse(calls.at(-1).init.body).show_movement_notices, null, 'inherit is explicitly sent as null');
    write.resolve(response({}));
    await view.settle();
    view.unmount();
}

async function testBuildingSaveAcrossReopen() {
    for (const [remount, failed] of [[false, false], [true, false], [true, true]]) {
        const env = environment();
        let saved = { BUILDINGID: 'a', BUILDINGNAME: 'A', SHOW_MOVEMENT_NOTICES: null };
        let write;
        let reads = 0, writes = 0, closes = 0;
        const api = async (url, init) => {
            if (init?.method === 'PUT') { writes++; write = deferred(); return write.promise; }
            if (url.startsWith('/api/db/tables/building?')) { reads++; return response([{ ...saved }]); }
            return response([]);
        };
        const props = { isOpen: true, buildingId: 'a', onClose: () => closes++ };
        const original = mount('components/BuildingSettingsModal.tsx', props, api, env);
        await original.settle();
        select(original).props.onChange({ target: { value: 'hide' } });
        await original.settle();
        button(original, 'components.BuildingSettingsModal.text034').props.onClick();
        let reopened;
        if (remount) {
            original.unmount();
            reopened = mount('components/BuildingSettingsModal.tsx', props, api, env);
        } else {
            original.update({ isOpen: false });
            original.update({ isOpen: true });
            reopened = original;
        }
        await reopened.settle();
        assert.equal(reads, 1, 'reopened Building form waits for pending write before reading');
        assert.ok(!select(reopened), 'cannot edit or save a stale form');
        if (!failed) saved = { ...saved, SHOW_MOVEMENT_NOTICES: false };
        write.resolve(response({}, !failed));
        await reopened.settle();
        assert.equal(select(reopened).props.value, failed ? 'inherit' : 'hide');
        assert.equal(select(reopened).props.disabled, false);
        if (failed) assert.ok(reopened.html.includes('role="alert"'), 'late save failure stays visible after remount');
        assert.equal(reads, 2);
        assert.equal(closes, 0, 'previous save cannot close the reopened form');
        select(reopened).props.onChange({ target: { value: 'show' } });
        await reopened.settle();
        button(reopened, 'components.BuildingSettingsModal.text034').props.onClick();
        assert.equal(writes, 2, 'new choice can save after the first completed');
        write.resolve(response({}));
        await reopened.settle();
        reopened.unmount();
    }
}

async function testCloseWhileWaitingForBuildingSave() {
    const env = environment();
    let saved = { BUILDINGID: 'a', BUILDINGNAME: 'A', SHOW_MOVEMENT_NOTICES: null };
    let write;
    const api = async (url, init) => {
        if (init?.method === 'PUT') { write = deferred(); return write.promise; }
        if (url.startsWith('/api/db/tables/building?')) return response([{ ...saved }]);
        return response([]);
    };
    const props = { isOpen: true, buildingId: 'a', onClose: () => {} };
    const original = mount('components/BuildingSettingsModal.tsx', props, api, env);
    await original.settle();
    select(original).props.onChange({ target: { value: 'hide' } });
    await original.settle();
    button(original, 'components.BuildingSettingsModal.text034').props.onClick();
    original.unmount();
    const waiting = mount('components/BuildingSettingsModal.tsx', props, api, env);
    await waiting.settle();
    assert.ok(!select(waiting));
    waiting.update({ isOpen: false });
    saved = { ...saved, SHOW_MOVEMENT_NOTICES: false };
    write.resolve(response({}));
    await waiting.settle();
    waiting.update({ isOpen: true });
    await waiting.settle();
    assert.equal(select(waiting).props.value, 'hide');
    assert.equal(select(waiting).props.disabled, false, 'reopening after a closed pending wait clears the saving state');
    assert.equal(button(waiting, 'components.BuildingSettingsModal.text034').props.disabled, false);
    waiting.unmount();
}

async function testBuildingReadRaces() {
    const reads = [], writes = [];
    const api = async (url, init) => {
        if (init?.method === 'PUT') { writes.push({ url, init }); return response({}); }
        if (url.startsWith('/api/db/tables/building?')) { const read = deferred(); reads.push(read); return read.promise; }
        return response([]);
    };
    const view = mount('components/BuildingSettingsModal.tsx', { isOpen: true, buildingId: 'a', onClose: () => {} }, api);
    view.update({ buildingId: 'b' });
    view.update({ buildingId: 'a' });
    reads[2].resolve(response([{ BUILDINGID: 'a', BUILDINGNAME: 'Current A', SHOW_MOVEMENT_NOTICES: false }]));
    await view.settle();
    reads[0].resolve(response([{ BUILDINGID: 'a', BUILDINGNAME: 'Stale A', SHOW_MOVEMENT_NOTICES: true }]));
    reads[1].resolve(response([{ BUILDINGID: 'b', BUILDINGNAME: 'Stale B', SHOW_MOVEMENT_NOTICES: true }]));
    await view.settle();
    assert.equal(select(view).props.value, 'hide', 'A → B → A ignores both stale responses');
    assert.ok(view.html.includes('Current A')); assert.ok(!view.html.includes('Stale'));
    view.update({ isOpen: false });
    view.update({ isOpen: true });
    await view.settle();
    assert.ok(!select(view), 'closed/reopened form waits for a new load');
    reads[3].reject(new Error('isolated network failure'));
    const original = console.error; console.error = () => {};
    try { await view.settle(); } finally { console.error = original; }
    assert.ok(view.html.includes('role="alert"'));
    assert.equal(button(view, 'components.BuildingSettingsModal.text034').props.disabled, true);
    assert.equal(writes.length, 0, 'loading errors cannot save stale settings');
    view.unmount();
}

(async () => {
    await testFilteringAndPagination();
    await testGlobalControl();
    await testSaveAcrossRemounts();
    await testGlobalBuildingOverlap();
    await testFailedGlobalSaveAfterRemount();
    await testAutoScroll();
    await testReadRaces();
    await testBuildingControl();
    await testBuildingReadRaces();
    await testBuildingSaveAcrossReopen();
    await testCloseWhileWaitingForBuildingSave();
    console.log('Movement notices: precedence, render-only history/cursors, hidden-page continuation, settings saves/retries/reopen, localization, polling/focus and stale response guards passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
