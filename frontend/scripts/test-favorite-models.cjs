const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

function loadFavoriteModels(apiFetch) {
    const source = fs.readFileSync(path.join(__dirname, '../src/lib/favoriteModels.ts'), 'utf8');
    const output = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;

    const listeners = [];
    const React = {
        useState: (init) => [typeof init === 'function' ? init() : init, (v) => listeners.push(v)],
        useEffect: (fn) => { fn(); },
    };
    const mod = { exports: {} };
    const localRequire = (name) => {
        if (name === 'react') return React;
        if (name === '@/i18n/api') return { apiFetch };
        throw new Error(`Unexpected require: ${name}`);
    };
    new Function('require', 'module', 'exports', output)(localRequire, mod, mod.exports);
    return { ...mod.exports, getLatestNotified: () => listeners[listeners.length - 1] };
}

(async () => {
    // 1. Stale GET resolving after a newer saveFavoriteModels must NOT overwrite the cache
    {
        let resolveGet;
        const getPromise = new Promise(r => { resolveGet = r; });
        const posts = [];
        const mod = loadFavoriteModels(async (url, opts) => {
            assert.equal(url, '/api/config/favorite-models');
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return { ok: true };
            }
            return { ok: true, json: async () => await getPromise };
        });

        // Subscribe a listener via useFavoriteModels
        const hookBefore = mod.useFavoriteModels(false);
        assert.deepEqual(hookBefore.favoriteModels, []);

        // Start a slow GET
        const inflightGet = mod.fetchFavoriteModels();
        // While GET is in flight, user toggles a favorite (POST succeeds)
        await mod.saveFavoriteModels(['model-a']);
        assert.deepEqual(mod.getLatestNotified(), ['model-a']);

        // Now the slow GET resolves with stale data
        resolveGet({ models: ['stale-old-model'] });
        const after = await inflightGet;
        assert.deepEqual(after, ['model-a']);
        assert.deepEqual(mod.getLatestNotified(), ['model-a']);
        assert.deepEqual(posts, [{ models: ['model-a'] }]);
    }

    // 2. Failed POST reverts only when no newer write happened
    {
        let rejectFirstPost;
        let call = 0;
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                call++;
                if (call === 1) {
                    return await new Promise((_, rej) => { rejectFirstPost = rej; });
                }
                return { ok: true };
            }
            return { ok: true, json: async () => ({ models: ['initial'] }) };
        });

        await mod.fetchFavoriteModels();
        mod.useFavoriteModels(false);

        const origErr = console.error;
        console.error = () => {};
        try {
            const firstSave = mod.saveFavoriteModels(['first']);
            await mod.saveFavoriteModels(['second']);
            rejectFirstPost(new Error('network error'));
            await firstSave;
            // Must stay ['second'], not revert to ['initial']
            assert.deepEqual(mod.getLatestNotified(), ['second']);

            // A standalone failing save reverts to ['second']
            call = 0;
            const failingSave = mod.saveFavoriteModels(['third']);
            rejectFirstPost(new Error('network error'));
            await failingSave;
            assert.deepEqual(mod.getLatestNotified(), ['second']);
        } finally {
            console.error = origErr;
        }
    }

    // 3. Toggling before initial GET completes merges with server favorites instead of wiping them
    {
        let resolveGet;
        const getPromise = new Promise(r => { resolveGet = r; });
        const posts = [];
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return { ok: true };
            }
            return { ok: true, json: async () => await getPromise };
        });

        const { toggleFavorite } = mod.useFavoriteModels(false);
        const inflightGet = mod.fetchFavoriteModels();

        // User toggles 'new-fav' and un-toggles 'existing-b' before initial GET resolves
        toggleFavorite('new-fav');
        toggleFavorite('existing-b');
        assert.deepEqual(posts, []); // Must NOT POST empty/partial list yet

        resolveGet({ models: ['existing-a', 'existing-b'] });
        await inflightGet;
        assert.deepEqual(posts, [{ models: ['existing-a', 'new-fav'] }]);
        assert.deepEqual(mod.getLatestNotified(), ['existing-a', 'new-fav']);
    }

    // 4. Failed initial GET discards queued toggles and never overwrites server favorites
    {
        const posts = [];
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                posts.push(JSON.parse(opts.body));
                return { ok: true };
            }
            return { ok: false, status: 500 };
        });

        const { toggleFavorite } = mod.useFavoriteModels(false);
        const inflightGet = mod.fetchFavoriteModels();
        toggleFavorite('new-fav');
        await inflightGet;
        assert.deepEqual(posts, []);
    }

    // 5. Failed merged POST after initial GET rolls back to fetched list, not empty array
    {
        let postCount = 0;
        const posts = [];
        const mod = loadFavoriteModels(async (_url, opts) => {
            if (opts?.method === 'POST') {
                postCount++;
                posts.push(JSON.parse(opts.body));
                if (postCount === 1) return { ok: false, status: 500 };
                return { ok: true };
            }
            return { ok: true, json: async () => ({ models: ['existing-a', 'existing-b'] }) };
        });

        const { toggleFavorite } = mod.useFavoriteModels(false);
        const inflightGet = mod.fetchFavoriteModels();
        toggleFavorite('new-fav');

        const origErr = console.error;
        console.error = () => {};
        try {
            await inflightGet;
        } finally {
            console.error = origErr;
        }

        // First POST failed, so cache must have rolled back to ['existing-a', 'existing-b']
        assert.deepEqual(mod.getLatestNotified(), ['existing-a', 'existing-b']);

        // Next toggle must build on ['existing-a', 'existing-b'], not []
        toggleFavorite('another-fav');
        await new Promise(r => setTimeout(r, 0));
        assert.deepEqual(posts[1], { models: ['existing-a', 'existing-b', 'another-fav'] });
        assert.deepEqual(mod.getLatestNotified(), ['existing-a', 'existing-b', 'another-fav']);
    }

    console.log('Favorite models cache version & race guard tests passed.');
})();
