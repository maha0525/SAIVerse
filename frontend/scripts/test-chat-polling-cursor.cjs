// Verify that chat polling cursors are bound to the active viewKey and never leak
// across room/view switches, and that empty rooms poll without `after` until a real ID arrives.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const pollingTs = fs.readFileSync(path.join(__dirname, '../src/lib/chatPolling.ts'), 'utf8');
const compiledPolling = ts.transpileModule(pollingTs, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
}).outputText;
const pollingMod = { exports: {} };
new Function('require', 'module', 'exports', compiledPolling)(require, pollingMod, pollingMod.exports);
const {
    resolveChatViewTarget,
    pickNewestRealMessageId,
    buildPollingCursor,
    buildPollUrl,
} = pollingMod.exports;

// 1. Building view cursor binding and stale cursor rejection
const targetA = resolveChatViewTarget('mira_room', null, 'mira_room', false);
const targetB = resolveChatViewTarget('skyrim_city_a', null, 'mira_room', false);
assert.deepEqual(targetA, { kind: 'building', buildingId: 'mira_room', viewKey: 'building:mira_room' });
assert.deepEqual(targetB, { kind: 'building', buildingId: 'skyrim_city_a', viewKey: 'building:skyrim_city_a' });

const cursorA = buildPollingCursor(targetA, [{ id: 'mira_city_a_room:777' }]);
assert.deepEqual(cursorA, { viewKey: 'building:mira_room', messageId: 'mira_city_a_room:777' });

// Switching to empty room B clears cursor and polls without `after`
const emptyCursorB = buildPollingCursor(targetB, []);
assert.equal(emptyCursorB, null);
assert.equal(
    buildPollUrl(targetB, emptyCursorB, 50),
    '/api/chat/history?limit=50&building_id=skyrim_city_a',
);

// Even if a stale cursor from room A were passed, buildPollUrl ignores it for room B
assert.equal(
    buildPollUrl(targetB, cursorA, 50),
    '/api/chat/history?limit=50&building_id=skyrim_city_a',
);

// Once room B receives a real message, polling switches to `after=...`
const cursorB = buildPollingCursor(targetB, [{ id: 'skyrim_city_a:4' }]);
assert.deepEqual(cursorB, { viewKey: 'building:skyrim_city_a', messageId: 'skyrim_city_a:4' });
assert.equal(
    buildPollUrl(targetB, cursorB, 50),
    '/api/chat/history?after=skyrim_city_a%3A4&limit=50&building_id=skyrim_city_a',
);

// 2. Optimistic temp-* IDs are skipped when selecting cursor
assert.equal(pickNewestRealMessageId([{ id: 'msg-1' }, { id: 'temp-99' }]), 'msg-1');
assert.equal(pickNewestRealMessageId([{ id: 'temp-99' }]), undefined);

// 3. Game session log view vs building view isolation
const gameTarget = resolveChatViewTarget(
    'reg_room_1',
    { region_id: 'reg_1', inside: true },
    'reg_room_1',
    false,
);
assert.deepEqual(gameTarget, { kind: 'game', regionId: 'reg_1', viewKey: 'game:reg_1' });
const gameCursor = buildPollingCursor(gameTarget, [{ id: 'g-10' }]);
assert.equal(
    buildPollUrl(gameTarget, gameCursor, 50),
    '/api/world/regions/reg_1/game/log?after=g-10&limit=50',
);

// Peeking at another building while inside a game switches to building view without reusing game cursor
const peekOtherBuilding = resolveChatViewTarget(
    'other_room',
    { region_id: 'reg_1', inside: true },
    'reg_room_1',
    false,
);
assert.equal(
    buildPollUrl(peekOtherBuilding, gameCursor, 50),
    '/api/chat/history?limit=50&building_id=other_room',
);

// Peeking at session log from outside a region
const peekSessionLog = resolveChatViewTarget(
    'outside_room',
    { region_id: 'reg_1', inside: false },
    'outside_room',
    true,
);
assert.equal(
    buildPollUrl(peekSessionLog, null, 50),
    '/api/world/regions/reg_1/game/log?limit=50',
);

// 4. Exercise the actual polling block in page.tsx
const page = fs.readFileSync(path.join(__dirname, '../src/app/page.tsx'), 'utf8');
const pollStart = page.indexOf('    // Polling for new messages');
const pollEnd = page.indexOf('    // 停止ボタンの判断材料', pollStart);
assert.ok(pollStart > 0 && pollEnd > pollStart, 'polling block in page.tsx is present');

const compiledPollBlock = ts.transpileModule(page.slice(pollStart, pollEnd), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

const deferred = () => {
    let resolve, reject;
    const promise = new Promise((a, b) => { resolve = a; reject = b; });
    return { promise, resolve, reject };
};
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
    const state = { messages: [] };
    const calls = [];
    let intervalFn = null;
    const refs = {
        currentBuildingIdRef: { current: 'skyrim_city_a' },
        activeGameRef: { current: null },
        serverCurrentBuildingIdRef: { current: 'skyrim_city_a' },
        sessionLogPeekRef: { current: false },
        isProcessingRef: { current: false },
    };

    const env = {
        ...refs,
        isHistoryLoaded: true,
        messages: state.messages,
        resolveChatViewTarget,
        buildPollingCursor,
        buildPollUrl,
        useRef: init => ({ current: init }),
        useEffect: fn => {
            const ret = fn();
            if (typeof ret === 'function') return ret;
        },
        setInterval: fn => { intervalFn = fn; return 1; },
        clearInterval: () => {},
        setMessages: updater => {
            state.messages = typeof updater === 'function' ? updater(state.messages) : updater;
        },
        setHasMore: () => {},
        setMoveTrigger: () => {},
        console: { log() {}, error() {} },
        apiFetch: url => {
            const p = deferred();
            calls.push({ url, ...p });
            return p.promise;
        },
    };

    new Function(...Object.keys(env), compiledPollBlock)(...Object.values(env));
    assert.equal(typeof intervalFn, 'function', 'polling interval callback registered');

    // Empty room polls without `after`
    const p1 = intervalFn();
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/chat/history?limit=50&building_id=skyrim_city_a');
    calls[0].resolve({
        ok: true,
        json: async () => ({ history: [{ id: 'skyrim:1', role: 'system', content: 'Mira entered' }] }),
    });
    await p1;
    await tick();
    assert.equal(state.messages.length, 1);
    assert.equal(state.messages[0].id, 'skyrim:1');

    // Stale poll response after switching room is discarded
    const p2 = intervalFn();
    assert.equal(calls.length, 2);
    refs.currentBuildingIdRef.current = 'another_room';
    calls[1].resolve({
        ok: true,
        json: async () => ({ history: [{ id: 'skyrim:2', role: 'assistant', content: 'stale' }] }),
    });
    await p2;
    assert.equal(state.messages.length, 1, 'response from old room must not be appended after room switch');

    console.log('Chat polling cursor & empty-room polling tests passed.');
})();
