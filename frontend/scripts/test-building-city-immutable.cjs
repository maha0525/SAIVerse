// Run with: node scripts/test-building-city-immutable.cjs (after npm ci).
// Exercise the real modal's JSX and handlers with synthetic hooks/API fixtures,
// and verify the select's HTML with ReactDOMServer. No browser, live data, or LLM.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');

const key = suffix => `components.BuildingSettingsModal.${suffix}`;
const cities = [
    { CITYID: 7, CITYNAME: '日本語の街', CITY_SLUG: 'city_a' },
    { CITYID: 42, CITYNAME: '', CITY_SLUG: 'city_b' },
];
const building = (id, cityId) => ({
    BUILDINGID: id, CITYID: cityId, BUILDINGNAME: `Room ${id}`, DESCRIPTION: 'Description',
    CAPACITY: 8, AUTO_INTERVAL_SEC: 37, ITEM_DISPLAY_LIMIT: 0,
    SYSTEM_INSTRUCTION: 'System instruction', IMAGE_PATH: '/synthetic/image.png',
    EXTRA_PROMPT_FILES: '["room.txt"]',
});
const buildings = [building('a', 7), building('b', 42)];
const compiled = ts.transpileModule(
    fs.readFileSync(path.resolve(__dirname, '../src/components/BuildingSettingsModal.tsx'), 'utf8'),
    { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } },
).outputText;

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}
function all(node, predicate) {
    if (Array.isArray(node)) return node.flatMap(child => all(child, predicate));
    if (!node || typeof node !== 'object') return [];
    return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
}
function text(node) {
    if (Array.isArray(node)) return node.map(text).join('');
    return node && typeof node === 'object' ? text(node.props?.children) : String(node ?? '');
}
function control(tree, suffix) {
    const fields = all(tree, node => node.type === 'div' && node.props.className === 'field'
        && all(node, child => child.type === 'label' && text(child) === key(suffix)).length === 1);
    assert.equal(fields.length, 1, `expected one field: ${suffix}`);
    const controls = all(fields[0], node => ['input', 'textarea', 'select'].includes(node.type));
    assert.equal(controls.length, 1);
    return controls[0];
}
function save(tree) {
    const buttons = all(tree, node => node.type === 'button' && node.props.className === 'saveBtn');
    assert.equal(buttons.length, 1);
    return buttons[0];
}
function expectCity(tree, fixture) {
    const select = control(tree, 'text012');
    assert.equal(select.type, 'select');
    assert.equal(select.props.value, fixture.CITYID);
    assert.equal(select.props.disabled, true, 'existing Building City must not be editable');
    assert.equal(select.props.onChange, undefined, 'City has no editing handler');
    assert.equal(select.props['aria-describedby'], 'building-city-immutable-hint');
    const hints = all(tree, node => node.props?.id === 'building-city-immutable-hint');
    assert.equal(hints.length, 1, 'City has a visible read-only explanation');
    assert.equal(text(hints[0]), key('cityImmutableHint'));
    assert.equal(hints[0].props.className, 'hint');
    const html = renderToStaticMarkup(select);
    assert.match(html, /^<select[^>]* disabled=""/);
    const city = cities.find(row => row.CITYID === fixture.CITYID);
    assert.ok(html.includes(`<option value="${city.CITYID}" selected="">${city.CITYNAME || city.CITY_SLUG}</option>`), html);
    assert.equal((html.match(/ selected=""/g) || []).length, 1);
}
function expectedPayload(fixture, edits = {}) {
    return {
        name: fixture.BUILDINGNAME, description: fixture.DESCRIPTION, capacity: fixture.CAPACITY,
        auto_interval: fixture.AUTO_INTERVAL_SEC, item_display_limit: fixture.ITEM_DISPLAY_LIMIT,
        show_movement_notices: null,
        system_instruction: fixture.SYSTEM_INSTRUCTION, image_path: fixture.IMAGE_PATH,
        extra_prompt_files: ['room.txt'], tool_ids: [9], city_id: fixture.CITYID, ...edits,
    };
}
function harness(buildingId = 'a') {
    const slots = [], effects = [], requests = [], notices = [], buildingReads = [];
    let cursor = 0, dirty = true, tree, closes = 0, saved = 0, failWrite = false;
    let props = { isOpen: true, buildingId, onClose: () => { closes++; }, onSaved: () => { saved++; } };
    const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
    const hooks = {
        ...React,
        useState(initial) {
            const index = cursor++;
            if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
            return [slots[index], value => {
                const next = typeof value === 'function' ? value(slots[index]) : value;
                if (!Object.is(next, slots[index])) { slots[index] = next; dirty = true; }
            }];
        },
        useRef(initial) { return slots[cursor++] ??= { current: initial }; },
        useEffect(effect, deps) {
            const index = cursor++;
            if (!same(slots[index]?.deps, deps)) {
                const previous = slots[index];
                slots[index] = { deps };
                effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = effect(); });
            }
        },
    };
    const helperModules = new Map();
    const window = new EventTarget();
    const localRequire = name => {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return require(name);
        if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
        if (name.endsWith('.module.css')) return { __esModule: true, default: new Proxy({}, { get: (_, field) => field }) };
        if (name.endsWith('/ImageUpload')) return { __esModule: true, default: () => null };
        if (name === '@/i18n/core') return { t: id => id };
        if (name === '@/i18n/useLocale') return { useLocale() {} };
        if (name === '@/lib/movementNotices' || name === '@/lib/buildingSettingsSave') {
            if (!helperModules.has(name)) {
                // Keep the real save lifecycle and event behavior, with this harness's fake HTTP.
                const module = { exports: {} };
                const source = fs.readFileSync(path.resolve(__dirname, '../src', `${name.slice(2)}.ts`), 'utf8');
                const output = ts.transpileModule(source, {
                    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
                }).outputText;
                new Function('require', 'module', 'exports', 'window', 'CustomEvent', output)(
                    localRequire, module, module.exports, window, CustomEvent,
                );
                helperModules.set(name, module.exports);
            }
            return helperModules.get(name);
        }
        if (name.endsWith('/lib/dbTable')) return { fetchAllTableRows: async table => {
            if (table === 'building') return buildingReads.shift() ?? buildings;
            if (table === 'city') return cities;
            if (table === 'tool') return [{ TOOLID: 9, TOOLNAME: 'Synthetic tool', DESCRIPTION: '' }];
            if (table === 'building_tool_link') return buildings.map(row => ({ BUILDINGID: row.BUILDINGID, TOOLID: 9 }));
            throw new Error(`Unexpected table ${table}`);
        } };
        if (name === '@/i18n/api') return { apiFetch: async (url, options) => {
            if (options?.method) {
                assert.equal(options.method, 'PUT');
                assert.match(url, /^\/api\/world\/buildings\/[ab]$/);
                requests.push({ url, body: JSON.parse(options.body) });
                return { ok: !failWrite, json: async () => ({ detail: 'Synthetic save failure' }) };
            }
            assert.ok(['/api/world/prompts/available', '/api/people/realtime-spell-catalog'].includes(url)
                || /^\/api\/world\/buildings\/(?:[ab]|missing)\/realtime-spell$/.test(url), `Unexpected GET ${url}`);
            return { ok: true, json: async () => url.endsWith('/available') ? ['room.txt'] : [] };
        } };
        throw new Error(`Unexpected dependency ${name}`);
    };
    const module = { exports: {} };
    new Function('require', 'module', 'exports', 'alert', 'console', compiled)(
        localRequire, module, module.exports, message => notices.push(message),
        { error: (...args) => notices.push(args), warn: (...args) => notices.push(args) },
    );
    function render() {
        cursor = 0; dirty = false;
        tree = module.exports.default(props);
        effects.splice(0).forEach(effect => effect());
        return tree;
    }
    return {
        render, requests, notices,
        async settle() {
            for (let i = 0; i < 20; i++) {
                if (dirty) render();
                await new Promise(setImmediate);
                if (!dirty) return tree;
            }
            throw new Error('Modal did not settle');
        },
        props(next) { props = { ...props, ...next }; dirty = true; return render(); },
        queueRead(promise) { buildingReads.push(promise); },
        writeFailure(value) { failWrite = value; },
        closed: () => closes, saved: () => saved,
    };
}

(async () => {
    let cases = 0;
    for (const fixture of buildings) {
        const h = harness(fixture.BUILDINGID);
        let tree = await h.settle();
        expectCity(tree, fixture);
        assert.equal(save(tree).props.disabled, false);
        await save(tree).props.onClick();
        assert.deepEqual(h.requests, [{ url: `/api/world/buildings/${fixture.BUILDINGID}`, body: expectedPayload(fixture) }]);
        assert.equal(h.closed(), 1); assert.equal(h.saved(), 1);
        cases++;

        const editing = harness(fixture.BUILDINGID);
        tree = await editing.settle();
        control(tree, 'text011').props.onChange({ target: { value: 'Renamed' } });
        control(tree, 'text015').props.onChange({ target: { value: 'Edited description' } });
        control(tree, 'text013').props.onChange({ target: { value: '12' } });
        tree = await editing.settle();
        editing.writeFailure(true);
        await save(tree).props.onClick();
        tree = await editing.settle();
        expectCity(tree, fixture);
        assert.match(text(tree), /Synthetic save failure/);
        assert.equal(editing.closed(), 0); assert.equal(editing.saved(), 0);
        editing.writeFailure(false);
        await save(tree).props.onClick();
        assert.equal(editing.requests.length, 2);
        for (const request of editing.requests) assert.deepEqual(request.body, expectedPayload(fixture, {
            name: 'Renamed', description: 'Edited description', capacity: 12,
        }));
        assert.equal(editing.closed(), 1); assert.equal(editing.saved(), 1);
        cases++;
    }

    // Closing without saving and reopening reloads the persisted City and fields.
    const reopened = harness();
    let tree = await reopened.settle();
    control(tree, 'text011').props.onChange({ target: { value: 'Discard me' } });
    reopened.render().props.onClick(); // Overlay close.
    assert.equal(reopened.closed(), 1); assert.equal(reopened.requests.length, 0);
    assert.equal(reopened.props({ isOpen: false }), null);
    reopened.props({ isOpen: true });
    tree = await reopened.settle();
    expectCity(tree, buildings[0]);
    assert.equal(control(tree, 'text011').props.value, 'Room a');
    await save(tree).props.onClick();
    assert.deepEqual(reopened.requests[0].body, expectedPayload(buildings[0]));
    cases++;

    // A slow read for a dismissed selection cannot overwrite the next Building's City.
    const stale = harness();
    const oldRead = deferred();
    stale.queueRead(oldRead.promise);
    await stale.settle();
    stale.props({ isOpen: false });
    stale.props({ isOpen: true, buildingId: 'b' });
    await stale.settle();
    oldRead.resolve(buildings);
    tree = await stale.settle();
    expectCity(tree, buildings[1]);
    await save(tree).props.onClick();
    assert.deepEqual(stale.requests, [{ url: '/api/world/buildings/b', body: expectedPayload(buildings[1]) }]);
    cases++;

    // The existing loaded-ID guard also rejects a save between selection and loading.
    const guarded = harness();
    await guarded.settle();
    const nextRead = deferred();
    guarded.queueRead(nextRead.promise);
    tree = guarded.props({ buildingId: 'b' });
    assert.equal(save(tree).props.disabled, true);
    await save(tree).props.onClick();
    assert.equal(guarded.requests.length, 0);
    nextRead.resolve(buildings);
    tree = await guarded.settle();
    expectCity(tree, buildings[1]);
    await save(tree).props.onClick();
    assert.deepEqual(guarded.requests[0], { url: '/api/world/buildings/b', body: expectedPayload(buildings[1]) });
    cases++;

    const missing = harness('missing');
    tree = await missing.settle();
    assert.equal(save(tree).props.disabled, true);
    await save(tree).props.onClick();
    assert.equal(missing.requests.length, 0);
    cases++;
    console.log(`${cases} building City cases passed: disabled/current-city HTML, exact payloads, normal edits, failure/retry, reopen, stale selection, and identity guards.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
