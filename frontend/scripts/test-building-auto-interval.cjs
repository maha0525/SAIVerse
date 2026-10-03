// Exercise the real components through rendered controls and mocked API boundaries.
// No browser, backend, production data, or LLM is used. No extra test dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const MODAL = 'BuildingSettingsModal';
const WORLD = 'settings/WorldEditor';
const key = (component, suffix) => `components.${component.replaceAll('/', '.')}.${suffix}`;
const fixture = (id, interval) => ({
    BUILDINGID: id, BUILDINGNAME: `Room ${id}`, DESCRIPTION: '', CAPACITY: 8,
    CITYID: 1, AUTO_INTERVAL_SEC: interval, SYSTEM_INSTRUCTION: '', ITEM_DISPLAY_LIMIT: 0,
});
const buildings = [fixture('a', 37), fixture('b', 0)];
const compiled = new Map([MODAL, WORLD].map(name => [name, ts.transpileModule(
    fs.readFileSync(path.resolve(__dirname, `../src/components/${name}.tsx`), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } },
).outputText]));

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}
function expand(node) {
    if (Array.isArray(node)) return node.map(expand);
    if (!node || typeof node !== 'object') return node;
    if (typeof node.type === 'function') return expand(node.type(node.props));
    return { ...node, props: { ...node.props, children: expand(node.props.children) } };
}
function all(node, predicate) {
    if (Array.isArray(node)) return node.flatMap(child => all(child, predicate));
    if (!node || typeof node !== 'object') return [];
    return [...(predicate(node) ? [node] : []), ...all(node.props.children, predicate)];
}
function text(node) {
    if (Array.isArray(node)) return node.map(text).join('');
    return node && typeof node === 'object' ? text(node.props.children) : String(node ?? '');
}
function button(tree, label) {
    const found = all(tree, node => node.type === 'button' && text(node).includes(label));
    assert.equal(found.length, 1, `expected one button: ${label}`);
    return found[0];
}
function input(tree, label) {
    const field = all(tree, node => node.type === 'div' && node.props.className === 'field'
        && all(node, child => child.type === 'label' && text(child) === label).length === 1);
    assert.equal(field.length, 1, `expected one field: ${label}`);
    const controls = all(field[0], node => ['input', 'textarea', 'select'].includes(node.type));
    assert.equal(controls.length, 1, `expected one control: ${label}`);
    return controls[0];
}

function harness(component, overrides = {}) {
    const slots = [], effects = [], requests = [], notices = [], readQueues = new Map();
    let cursor = 0, dirty = true, tree, closes = 0, saved = 0, failWrite = false, writeGate;
    let props = { isOpen: true, buildingId: 'a', onClose: () => { closes++; }, onSaved: () => { saved++; }, ...overrides };
    const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
    const hooks = {
        useState(initial) {
            const index = cursor++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => {
                const next = typeof value === 'function' ? value(slots[index]) : value;
                if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
            }];
        },
        useRef(initial) {
            const index = cursor++;
            return slots[index] ??= { current: initial };
        },
        useEffect(effect, deps) {
            const index = cursor++;
            if (!same(slots[index], deps)) { slots[index] = deps; effects.push(effect); }
        },
        useCallback(callback, deps) {
            const index = cursor++;
            if (!same(slots[index]?.deps, deps)) slots[index] = { callback, deps };
            return slots[index].callback;
        },
    };
    const rows = table => ({ building: buildings, city: [{ CITYID: 1, CITY_SLUG: 'test', CITYNAME: 'Test' }] }[table] || []);
    const read = table => readQueues.get(table)?.shift() ?? Promise.resolve(rows(table));
    const jsx = (type, props) => ({ type, props });
    const localRequire = name => {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'lucide-react') return new Proxy({}, { get: (_, icon) => `icon-${icon}` });
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, field) => field }) };
        if (name.endsWith('/ImageUpload') || name.endsWith('/FileUpload')) return { __esModule: true, default: () => null };
        if (name === '@/i18n/core') return { t: id => id };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name.endsWith('/lib/dbTable')) return {
            DB_TABLE_PAGE_SIZE: 100, fetchAllTableRows: read,
            fetchTablePage: async table => ({ rows: rows(table), total: rows(table).length }),
        };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            if (options?.method) {
                assert.match(url, /^\/api\/world\/buildings(?:\/[ab])?$/);
                requests.push({ url, method: options.method, body: JSON.parse(options.body) });
                if (writeGate) await writeGate;
                return { ok: !failWrite, json: async () => ({ detail: 'synthetic failure' }), text: async () => '{}' };
            }
            assert.ok(['/api/world/prompts/available', '/api/people/realtime-spell-catalog'].includes(url)
                || /^\/api\/world\/buildings\/[ab]\/realtime-spell$/.test(url), `unexpected GET ${url}`);
            return { ok: true, json: async () => [] };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', 'alert', 'console', compiled.get(component))(
        localRequire, module, module.exports, message => notices.push(message),
        { error: (...args) => notices.push(args), warn: (...args) => notices.push(args) },
    );
    function render() {
        cursor = 0; dirty = false;
        tree = expand(module.exports.default(props));
        effects.splice(0).forEach(effect => effect());
        return tree;
    }
    return {
        render, requests, notices,
        async settle() {
            for (let i = 0; i < 10; i++) {
                if (dirty) render();
                await new Promise(setImmediate);
                if (!dirty) return tree;
            }
            throw new Error('Component did not settle');
        },
        props(next) { props = { ...props, ...next }; dirty = true; return render(); },
        queueRead(table, promise) { readQueues.set(table, [...(readQueues.get(table) || []), promise]); },
        writeFailure(value) { failWrite = value; },
        pauseWrite(promise) { writeGate = promise; },
        closed: () => closes, saved: () => saved,
    };
}
function noInterval(tree, component, numericCount) {
    assert.ok(!text(tree).includes(key(component, component === MODAL ? 'text014' : 'text054')));
    assert.equal(all(tree, node => node.type === 'input' && node.props.type === 'number').length, numericCount);
}
async function openWorld() {
    const h = harness(WORLD);
    const tree = await h.settle();
    button(tree, key(WORLD, 'label002')).props.onClick();
    await h.settle();
    return h;
}
function selectWorld(h, id) {
    const items = all(h.render(), node => node.type === 'div' && node.props.className?.split(' ').includes('item')
        && text(node) === `Room ${id}`);
    assert.equal(items.length, 1);
    items[0].props.onClick();
}

(async () => {
    let cases = 0;
    for (const building of buildings) {
        const h = harness(MODAL, { buildingId: building.BUILDINGID });
        let tree = await h.settle();
        noInterval(tree, MODAL, 2); // Capacity and the independent item display limit remain.
        input(tree, key(MODAL, 'text011')).props.onChange({ target: { value: 'Renamed' } });
        tree = await h.settle();
        h.writeFailure(true);
        await button(tree, key(MODAL, 'text034')).props.onClick();
        tree = await h.settle();
        assert.equal(h.closed(), 0);
        h.writeFailure(false);
        const gate = deferred();
        h.pauseWrite(gate.promise);
        const pending = button(tree, key(MODAL, 'text034')).props.onClick();
        assert.equal(button(h.render(), key(MODAL, 'text033')).props.disabled, true);
        gate.resolve(); await pending;
        assert.equal(h.closed(), 1); assert.equal(h.saved(), 1);
        assert.equal(h.requests.length, 2);
        for (const request of h.requests) {
            assert.equal(request.url, `/api/world/buildings/${building.BUILDINGID}`);
            assert.equal(request.body.auto_interval, building.AUTO_INTERVAL_SEC);
            assert.equal(request.body.name, 'Renamed');
            assert.equal(request.body.item_display_limit, 0);
        }
        cases++;
    }

    // Dismiss without saving, reopen, and load a different building while a read is pending.
    const modal = harness(MODAL);
    let tree = await modal.settle();
    input(tree, key(MODAL, 'text011')).props.onChange({ target: { value: 'Discard me' } });
    modal.render().props.onClick(); // Overlay close.
    assert.equal(modal.requests.length, 0);
    assert.equal(modal.props({ isOpen: false }), null);
    const staleRead = deferred();
    modal.queueRead('building', staleRead.promise);
    modal.props({ isOpen: true });
    modal.props({ buildingId: 'b' });
    tree = await modal.settle();
    staleRead.resolve(buildings);
    tree = await modal.settle();
    assert.equal(input(tree, key(MODAL, 'text011')).props.value, 'Room b');
    await button(tree, key(MODAL, 'text034')).props.onClick();
    assert.equal(modal.requests[0].url, '/api/world/buildings/b');
    assert.equal(modal.requests[0].body.auto_interval, 0);
    cases++;

    // The retained value must not bypass the modal's existing loaded-ID save guard.
    const guarded = harness(MODAL);
    await guarded.settle();
    const nextRead = deferred();
    guarded.queueRead('building', nextRead.promise);
    tree = guarded.props({ buildingId: 'b' });
    const oldSave = button(tree, key(MODAL, 'text034'));
    assert.equal(oldSave.props.disabled, true);
    await oldSave.props.onClick();
    assert.equal(guarded.requests.length, 0);
    nextRead.resolve(buildings);
    await button(await guarded.settle(), key(MODAL, 'text034')).props.onClick();
    assert.equal(guarded.requests[0].body.auto_interval, 0);
    assert.equal(guarded.requests[0].url, '/api/world/buildings/b');
    cases++;

    for (const building of buildings) {
        const h = await openWorld();
        noInterval(h.render(), WORLD, 1); // New-building form exposes capacity only.
        selectWorld(h, building.BUILDINGID);
        tree = await h.settle();
        noInterval(tree, WORLD, 2);
        input(tree, key(WORLD, 'text048')).props.onChange({ target: { value: 'Renamed' } });
        tree = await h.settle();
        h.writeFailure(true);
        await button(tree, key(WORLD, 'text024')).props.onClick();
        h.writeFailure(false);
        await button(await h.settle(), key(WORLD, 'text024')).props.onClick();
        assert.equal(h.requests.length, 2);
        for (const request of h.requests) {
            assert.equal(request.url, `/api/world/buildings/${building.BUILDINGID}`);
            assert.equal(request.body.auto_interval, building.AUTO_INTERVAL_SEC);
            assert.equal(request.body.name, 'Renamed');
            assert.equal(request.body.item_display_limit, 0);
        }
        cases++;
    }

    // A late response for A must not replace B's retained interval.
    const world = await openWorld();
    const links = deferred();
    world.queueRead('building_tool_link', links.promise);
    selectWorld(world, 'a');
    selectWorld(world, 'b');
    await world.settle(); links.resolve([]);
    tree = await world.settle();
    assert.equal(input(tree, key(WORLD, 'text048')).props.value, 'Room b');
    await button(tree, key(WORLD, 'text024')).props.onClick();
    assert.equal(world.requests[0].url, '/api/world/buildings/b');
    assert.equal(world.requests[0].body.auto_interval, 0);
    cases++;

    // New-building reset must discard the old record, retaining the create API contract.
    button(await world.settle(), key(WORLD, 'text045')).props.onClick();
    tree = await world.settle();
    noInterval(tree, WORLD, 1);
    assert.equal(input(tree, key(WORLD, 'text048')).props.value, '');
    input(tree, key(WORLD, 'text048')).props.onChange({ target: { value: 'New room' } });
    tree = await world.settle();
    input(tree, key(WORLD, 'text051')).props.onChange({ target: { value: '1' } });
    await button(await world.settle(), key(WORLD, 'text026')).props.onClick();
    assert.equal(world.requests[1].method, 'POST');
    assert.equal(world.requests[1].body.name, 'New room');
    assert.equal(Object.hasOwn(world.requests[1].body, 'auto_interval'), false);
    cases++;

    // Discard an in-flight selection before its tool links arrive.
    const abandoned = deferred();
    world.queueRead('building_tool_link', abandoned.promise);
    selectWorld(world, 'a');
    button(world.render(), key(WORLD, 'text045')).props.onClick();
    abandoned.resolve([]);
    tree = await world.settle();
    assert.equal(input(tree, key(WORLD, 'text048')).props.value, '');
    assert.equal(all(tree, node => node.type === 'button' && text(node).includes(key(WORLD, 'text024'))).length, 0);
    noInterval(tree, WORLD, 1);
    cases++;
    console.log(`${cases} building interval cases passed: UI removal, exact-value saves, failure/retry, dismissal, identity guards, stale selection, and create reset.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
