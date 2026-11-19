const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function loadFavoriteModels(apiFetch) {
    const source = fs.readFileSync(path.join(__dirname, '../src/lib/favoriteModels.ts'), 'utf8');
    const output = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;

    const snapshots = [];
    const hookReact = {
        useState: (init) => {
            const initial = typeof init === 'function' ? init() : init;
            snapshots.push(initial);
            return [initial, (v) => snapshots.push(v)];
        },
        useEffect: (fn) => { fn(); },
    };
    const mod = { exports: {} };
    const localRequire = (name) => {
        if (name === 'react') return hookReact;
        if (name === '@/i18n/api') return { apiFetch };
        throw new Error(`Unexpected require: ${name}`);
    };
    new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
    return {
        ...mod.exports,
        getLatestSnapshot: () => snapshots[snapshots.length - 1],
    };
}

function loadModelPicker() {
    const messages = require('../src/i18n/messages.json');
    const source = fs.readFileSync(path.join(__dirname, '../src/components/common/ModelPicker.tsx'), 'utf8');
    const output = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;
    const mod = { exports: {} };
    const localRequire = (name) => {
        if (name === 'react') return React;
        if (name === 'react/jsx-runtime') return require(name);
        if (name === 'lucide-react') return new Proxy({}, { get: () => () => null });
        if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, k) => String(k) }) };
        if (name === '@/i18n/core') return { t: (k) => messages[k]?.ja ?? k };
        if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
        throw new Error(`Unexpected require: ${name}`);
    };
    new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
    return mod.exports.default;
}

(async () => {
    // 1. GET -> POST race: slow GET resolving after a newer POST must not overwrite cache
    {
        const getGate = deferred();
        const posts = [];
        const mod = loadFavoriteModels(async (url, opts) => {
            assert.equal(url, '/api/config/favorite-models');
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return { ok: true };
            }
            return { ok: true, json: async () => await getGate.promise };
        });

        mod.useFavoriteModels(false);
        const inflightGet = mod.fetchFavoriteModels();
        await mod.saveFavoriteModels(['model-a']);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['model-a'], ready: true });

        getGate.resolve({ models: ['stale-old-model'] });
        const after = await inflightGet;
        assert.deepEqual(after, ['model-a']);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['model-a'], ready: true });
        assert.deepEqual(posts, [{ models: ['model-a'] }]);
    }

    // 2. POST -> GET race: GET triggered while POST is in flight must not read pre-POST data or clobber cache
    {
        let getCalls = 0;
        const postGate = deferred();
        const posts = [];
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return await postGate.promise;
            }
            getCalls++;
            return { ok: true, json: async () => ({ models: ['initial'] }) };
        });

        await mod.fetchFavoriteModels();
        const hook = mod.useFavoriteModels(false);
        assert.equal(getCalls, 1);

        // Start POST ['initial', 'added'] (in flight)
        const savePromise = mod.saveFavoriteModels(['initial', 'added']);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['initial', 'added'], ready: true });

        // Another modal opens while POST is in flight
        const duringPost = await mod.fetchFavoriteModels();
        assert.equal(getCalls, 1, 'must not issue a stale GET while POST is in flight');
        assert.deepEqual(duringPost, ['initial', 'added']);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['initial', 'added'], ready: true });

        // POST finishes
        postGate.resolve({ ok: true });
        await savePromise;

        // Next toggle builds on ['initial', 'added'], not stale ['initial']
        hook.toggleFavorite('second');
        await new Promise(r => setImmediate(r));
        assert.deepEqual(posts, [
            { models: ['initial', 'added'] },
            { models: ['initial', 'added', 'second'] },
        ]);
    }

    // 3. Pre-load guard: toggleFavorite is ignored before initial GET completes (and when initial GET fails);
    //    empty initial GET ({ models: [] }) still marks favoritesReady=true
    {
        const getGate = deferred();
        const posts = [];
        let failGet = true;
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return { ok: true };
            }
            if (failGet) return { ok: false, status: 500 };
            return { ok: true, json: async () => await getGate.promise };
        });

        const hook = mod.useFavoriteModels(false);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: [], ready: false });

        // Initial GET fails
        await mod.fetchFavoriteModels();
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: [], ready: false });
        hook.toggleFavorite('should-not-post');
        assert.deepEqual(posts, []);

        // Retry GET (slow) -> still not ready until resolved
        failGet = false;
        const retryGet = mod.fetchFavoriteModels();
        hook.toggleFavorite('still-not-ready');
        assert.deepEqual(posts, []);

        // Resolves with empty list -> ready becomes true!
        getGate.resolve({ models: [] });
        await retryGet;
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: [], ready: true });

        // Now toggle works
        hook.toggleFavorite('first-fav');
        await new Promise(r => setImmediate(r));
        assert.deepEqual(posts, [{ models: ['first-fav'] }]);
    }

    // 4. Serialized rapid writes & failure rollbacks (never concurrent POSTs, never roll back to unsaved state)
    {
        const postGates = [];
        const posts = [];
        let inflightPosts = 0;
        let maxConcurrentPosts = 0;
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                inflightPosts++;
                maxConcurrentPosts = Math.max(maxConcurrentPosts, inflightPosts);
                posts.push(JSON.parse(opts.body));
                const g = deferred();
                postGates.push(g);
                try {
                    return await g.promise;
                } finally {
                    inflightPosts--;
                }
            }
            return { ok: true, json: async () => ({ models: ['base'] }) };
        });

        await mod.fetchFavoriteModels();
        const hook = mod.useFavoriteModels(false);

        const origErr = console.error;
        console.error = () => {};
        try {
            // Rapidly toggle x, y, z while first POST is in flight -> coalesced into 2 serialized POSTs
            hook.toggleFavorite('x');
            hook.toggleFavorite('y');
            hook.toggleFavorite('z');
            assert.equal(posts.length, 1);
            assert.deepEqual(posts[0], { models: ['base', 'x'] });
            assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['base', 'x', 'y', 'z'], ready: true });

            // Case 4a: First POST fails, second POST ALSO fails -> must roll back to 'base' (NOT ['base', 'x']!)
            postGates[0].resolve({ ok: false, status: 500 });
            await new Promise(r => setImmediate(r));
            assert.equal(posts.length, 2);
            assert.deepEqual(posts[1], { models: ['base', 'x', 'y', 'z'] });

            postGates[1].resolve({ ok: false, status: 500 });
            await new Promise(r => setImmediate(r));
            assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['base'], ready: true });

            // Case 4b: First POST succeeds, second POST fails -> must roll back to first POST's confirmed state
            hook.toggleFavorite('ok-one');
            hook.toggleFavorite('bad-two');
            assert.equal(posts.length, 3);
            postGates[2].resolve({ ok: true });
            await new Promise(r => setImmediate(r));
            assert.equal(posts.length, 4);
            postGates[3].resolve({ ok: false, status: 500 });
            await new Promise(r => setImmediate(r));
            assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['base', 'ok-one'], ready: true });

            assert.equal(maxConcurrentPosts, 1, 'POST requests must be strictly serialized');
        } finally {
            console.error = origErr;
        }
    }

    // 5. Out-of-order concurrent GETs & aborted GET
    {
        const get1 = deferred();
        const get2 = deferred();
        let n = 0;
        const mod = loadFavoriteModels(async () => {
            n++;
            if (n === 1) return { ok: true, json: async () => await get1.promise };
            if (n === 2) return { ok: true, json: async () => await get2.promise };
            return { ok: true, json: async () => ({ models: ['aborted-data'] }) };
        });

        mod.useFavoriteModels(false);
        const p1 = mod.fetchFavoriteModels();
        const p2 = mod.fetchFavoriteModels();

        get2.resolve({ models: ['newer'] });
        await p2;
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['newer'], ready: true });

        get1.resolve({ models: ['older'] });
        await p1;
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['newer'], ready: true });

        // Aborted GET ignored even if fake transport still resolves
        const ac = new AbortController();
        ac.abort();
        await mod.fetchFavoriteModels(ac.signal);
        assert.deepEqual(mod.getLatestSnapshot(), { favorites: ['newer'], ready: true });

        // Malformed 200 response does not clobber cache
        const modBad = loadFavoriteModels(async () => ({ ok: true, json: async () => ({ unexpected: true }) }));
        modBad.useFavoriteModels(false);
        assert.equal(await modBad.fetchFavoriteModels(), null);
        assert.deepEqual(modBad.getLatestSnapshot(), { favorites: [], ready: false });
    }

    // 6. ModelPicker renders star buttons disabled when favoriteDisabled=true
    {
        const ModelPicker = loadModelPicker();
        const models = [{ id: 'm1', name: 'Model 1', picker_group: 'openai', is_current: true }];
        const disabledHtml = renderToStaticMarkup(React.createElement(ModelPicker, {
            models,
            value: 'm1',
            onChange: () => {},
            favorites: [],
            onToggleFavorite: () => {},
            favoriteDisabled: true,
            alwaysOpen: true,
        }));
        assert.match(disabledHtml, /class="favoriteBtn\s*"[^>]*disabled=""/);

        const enabledHtml = renderToStaticMarkup(React.createElement(ModelPicker, {
            models,
            value: 'm1',
            onChange: () => {},
            favorites: ['m1'],
            onToggleFavorite: () => {},
            favoriteDisabled: false,
            alwaysOpen: true,
        }));
        assert.ok(!/class="favoriteBtn[^"]*"[^>]*disabled/.test(enabledHtml));
    }

    console.log('Favorite models state machine, serialization, race guards & picker tests passed.');
})();
