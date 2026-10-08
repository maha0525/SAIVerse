// Exercise the actual page fetch callback and continuation component with fake HTTP.
// No server, browser, production history or persona calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const page = fs.readFileSync(path.join(__dirname, '../src/app/page.tsx'), 'utf8');
const callbackStart = page.indexOf('    const fetchHistory = ');
const callbackEnd = page.indexOf('    // Region RPG: active_game', callbackStart);
assert.ok(callbackStart > 0 && callbackEnd > callbackStart, 'the actual history callback is present');
const callback = ts.transpileModule(page.slice(callbackStart, callbackEnd), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const startupStart = page.indexOf('        // Fetch current building_id for multi-device safety');
const startupEnd = page.indexOf('        // Fetch saved playbook setting', startupStart);
assert.ok(startupStart > 0 && startupEnd > startupStart);
const startup = ts.transpileModule(page.slice(startupStart, startupEnd), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const initialHasMore = page.match(/const \[hasMore, setHasMore\] = useState\((true|false)\)/)?.[1] === 'true';
assert.equal(initialHasMore, false, 'no room has confirmed older history at startup');
assert.match(page, /<HistoryContinuation[\s\S]*?hasMore=\{hasMore\}[\s\S]*?onLoad=\{fetchHistory\}/);
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/components/HistoryContinuation.tsx'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
}).outputText;
const moduleResult = { exports: {} };
new Function('require', 'module', 'exports', compiled)(name => {
    if (name === '@/i18n/core') return { t: key => key };
    if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
    if (name.endsWith('.css')) return { __esModule: true, default: {} };
    return require(name);
}, moduleResult, moduleResult.exports);
const HistoryContinuation = moduleResult.exports.default;
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const message = id => ({ id, role: 'host', is_movement_notice: true, content: 'synthetic movement' });

function harness() {
    const state = { hasMore: initialHasMore, ready: false, loading: false, messages: [] };
    const calls = [], renders = [], timers = [];
    const refs = {
        currentBuildingIdRef: { current: 'new-room' },
        historyRequestGenerationRef: { current: 0 },
        chatAreaRef: { current: null },
        previousScrollHeightRef: { current: 0 },
        activeGameRef: { current: null },
        serverCurrentBuildingIdRef: { current: 'new-room' },
        sessionLogPeekRef: { current: false },
    };
    const render = () => renders.push(renderToStaticMarkup(React.createElement(HistoryContinuation, {
        oldestId: state.messages[0]?.id, hasMore: state.hasMore, ready: state.ready,
        loading: state.loading, onLoad: async () => {},
    })));
    const set = key => value => { state[key] = typeof value === 'function' ? value(state[key]) : value; render(); };
    const env = {
        ...refs, URLSearchParams, encodeURIComponent,
        setHasMore: set('hasMore'), setIsHistoryLoaded: set('ready'),
        setIsLoadingMore: set('loading'), setMessages: set('messages'),
        setBackendConnected: () => {}, updateAddonMetadata: () => {},
        setTimeout: fn => { timers.push(fn); }, console: { log() {}, error() {} },
        apiFetch: url => { const pending = deferred(); calls.push({ url, ...pending }); return pending.promise; },
    };
    const fetch = new Function(...Object.keys(env), `${callback}; return fetchHistory;`)(...Object.values(env));
    env.fetchHistory = fetch;
    env.setCurrentBuildingId = () => {};
    env.setLostBuildingNotice = () => {};
    env.updateServerBuildingId = bid => { refs.serverCurrentBuildingIdRef.current = bid; };
    env.userDisplayNameRef = { current: '' };
    env.userAvatarRef = { current: null };
    env.applyActiveGame = game => { refs.activeGameRef.current = game; };
    env.fetchBuildingInfo = () => {};
    const start = () => new Function(...Object.keys(env), startup)(...Object.values(env));
    const answer = (index, history, has_more) => calls[index].resolve({ ok: true, json: async () => ({ history, ...(has_more === undefined ? {} : { has_more }) }) });
    render();
    return { state, refs, calls, renders, fetch, start, answer, timers, flushTimers: () => timers.splice(0).forEach(fn => fn()) };
}

(async () => {
    // Exercise the actual mount/status chain, not only a callback with pre-filled refs.
    for (const game of [null, { region_id: 'game-one', inside: true }]) {
        const initial = harness();
        initial.refs.currentBuildingIdRef.current = null;
        initial.start();
        assert.equal(initial.calls.length, 1);
        assert.equal(initial.calls[0].url, '/api/user/status');
        initial.calls[0].resolve({ ok: true, json: async () => ({ current_building_id: 'home', active_game: game }) });
        await tick();
        assert.equal(initial.calls.length, 2, 'normal and game startup both request history after status');
        assert.ok(initial.calls[1].url.startsWith(game ? '/api/world/regions/game-one/game/log?' : '/api/chat/history?'));
        if (!game) assert.ok(initial.calls[1].url.includes('building_id=home'));
        initial.answer(1, [message('first')], false);
        await tick(); initial.flushTimers();
        assert.equal(initial.state.ready, true);
        assert.equal(initial.state.messages[0].id, 'first');
        assert.ok(initial.renders.every(html => !html.includes('<button')));
    }

    // Brand-new room: not even one disabled continuation button before/during/after its response.
    const fresh = harness();
    const freshLoad = fresh.fetch();
    assert.equal(fresh.state.hasMore, false);
    await tick();
    fresh.answer(0, [message('first-and-only')], false);
    await freshLoad; fresh.flushTimers();
    assert.ok(fresh.renders.every(html => !html.includes('<button')), 'a room without older history never shows the button');

    // Missing has_more is not server confirmation, even with a full 20-row page.
    const absent = harness();
    const absentLoad = absent.fetch();
    absent.answer(0, Array.from({ length: 20 }, (_, n) => message(`row-${n}`)));
    await absentLoad; absent.flushTimers();
    assert.ok(absent.renders.every(html => !html.includes('<button')));

    const current = harness();
    const first = current.fetch();
    const body = deferred();
    current.calls[0].resolve({ ok: true, json: () => body.promise });
    await tick();
    assert.equal(current.state.hasMore, false, 'headers alone do not confirm older history');
    body.resolve({ history: [message('middle')], has_more: true });
    await first; current.flushTimers();
    assert.ok(current.renders.at(-1).includes('<button'), 'confirmed hidden-only history retains its continuation');
    const older = current.fetch('middle');
    assert.ok(current.calls[1].url.includes('before=middle'));
    current.answer(1, [message('oldest')], false);
    await older;
    assert.equal(current.state.hasMore, false);
    assert.ok(!current.renders.at(-1).includes('<button'));

    // A delayed old room's answer or ready timer cannot enable B or a newer A view.
    for (const returnToA of [false, true]) {
        const race = harness();
        race.refs.currentBuildingIdRef.current = 'a';
        const oldA = race.fetch();
        race.refs.currentBuildingIdRef.current = 'b';
        const b = race.fetch();
        race.answer(1, [message('b-only')], false); await b;
        if (returnToA) {
            race.refs.currentBuildingIdRef.current = 'a';
            const newA = race.fetch();
            race.answer(2, [message('a-only')], false); await newA;
        }
        race.answer(0, [message('stale-a')], true); await oldA;
        race.flushTimers();
        assert.equal(race.state.hasMore, false);
        assert.equal(race.state.messages[0].id, returnToA ? 'a-only' : 'b-only');
        assert.ok(race.renders.every(html => !html.includes('<button')));
    }
    const switchRoom = harness();
    const a = switchRoom.fetch(); switchRoom.answer(0, [message('a-old')], true); await a;
    switchRoom.refs.currentBuildingIdRef.current = 'b';
    const b = switchRoom.fetch();
    assert.equal(switchRoom.state.hasMore, false, 'a new view immediately invalidates the old confirmation');
    switchRoom.flushTimers();
    assert.equal(switchRoom.state.ready, false, 'old room ready timers do not complete the new load');
    switchRoom.answer(1, [], false); await b; switchRoom.flushTimers();
    assert.ok(!switchRoom.renders.at(-1).includes('<button'));
    console.log('History pagination: current-room server confirmation, new-room absence, raw cursor continuation and stale response guards passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
