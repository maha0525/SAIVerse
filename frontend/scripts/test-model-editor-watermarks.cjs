// Exercise the actual initialization effect, load and save handlers, with local
// state setters and a fake API. This includes edit and duplicate load -> save.
// No browser, backend, production data, or LLM is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/components/settings/ModelEditorModal.tsx'), 'utf8');
const ast = ts.createSourceFile('ModelEditorModal.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function declaration(name) {
    let found;
    function visit(node) {
        if (ts.isVariableStatement(node) && node.declarationList.declarations.some(d => d.name.getText(ast) === name)) {
            assert.equal(found, undefined, `ambiguous declaration: ${name}`);
            found = node.getText(ast);
        }
        ts.forEachChild(node, visit);
    }
    visit(ast);
    assert.ok(found, `missing declaration: ${name}`);
    return found;
}
function initializationEffect() {
    const effects = [];
    function visit(node) {
        if (ts.isCallExpression(node) && node.expression.getText(ast) === 'useEffect'
            && node.arguments[1]?.getText(ast) === '[isOpen, mode, modelKey, cloneSource]') {
            effects.push(node.arguments[0].getText(ast));
        }
        ts.forEachChild(node, visit);
    }
    visit(ast);
    assert.equal(effects.length, 1, 'expected one editor initialization effect');
    return `const initializeEditor = ${effects[0]};`;
}
function compile(source) {
    return ts.transpileModule(source,
        { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
    ).outputText;
}
const compiled = compile([
    'BASIC_FIELDS', 'DEFAULT_CONTEXT_LENGTH', 'WATERMARK_FIELDS', 'emptyWatermarks',
    'watermarkFieldFromConfig', 'applyConfig', 'loadModel', 'handleSave',
].map(declaration).join('\n'));
const compiledEffect = compile(initializationEffect());
const HIGH = 'metabolism_high_chars';
const TARGET = 'metabolism_target_chars';
function harness({ extra = {}, watermarks = {}, mode = 'create', fail = false } = {}) {
    const state = {
        key: 'test-model', model: 'test-api-model', contextLength: 128000,
        displayName: '', providerRef: '', mode,
        watermarks: { [HIGH]: '', [TARGET]: '', ...watermarks },
        extraJson: JSON.stringify(extra),
        effectiveDefaults: { [HIGH]: 100000, [TARGET]: 50000 },
    };
    const requests = [];
    const reads = [];
    let loadedModel;
    let error = null;
    const mocks = {
        uiText: (key, params) => `${key}:${JSON.stringify(params ?? {})}`,
        setSaveError: value => { error = value; },
        setWatermarks: value => { state.watermarks = value; },
        setExtraJson: value => { state.extraJson = value; },
        setKey: value => { state.key = value; },
        setModel: value => { state.model = value; },
        setDisplayName: value => { state.displayName = value; },
        setProviderRef: value => { state.providerRef = value; },
        setContextLength: value => { state.contextLength = value; },
        setSource: value => { state.source = value; },
        setLoading: value => { state.loading = value; },
        setSaving: () => {}, onSaved: () => {}, onClose: () => {},
        alert: () => { throw new Error('unexpected alert'); },
        apiFetch: async (url, options) => {
            if (!options) {
                assert.equal(url, `/api/config/models/${loadedModel.key}`);
                reads.push(url);
                return { ok: true, json: async () => structuredClone(loadedModel) };
            }
            requests.push({ url, method: options.method, body: JSON.parse(options.body) });
            return { ok: !fail, status: fail ? 500 : 200, text: async () => 'failed', json: async () => ({}) };
        },
    };
    function render() {
        // Recreate the closures from current state, as React does on render.
        const scope = { ...state, ...mocks };
        return new Function(...Object.keys(scope), `${compiled}\nreturn { applyConfig, loadModel, handleSave };`)(...Object.values(scope));
    }
    return {
        state, requests, reads, error: () => error,
        async open(flow, config) {
            assert.ok(['edit', 'duplicate'].includes(flow));
            loadedModel = { key: 'source-model', config, source: 'builtin' };
            state.mode = flow === 'edit' ? 'edit' : 'create';
            const handlers = render();
            const pendingLoads = [];
            const scope = {
                ...state, ...mocks, ...handlers, isOpen: true,
                modelKey: flow === 'edit' ? loadedModel.key : undefined,
                cloneSource: flow === 'duplicate' ? structuredClone(loadedModel) : undefined,
                loadProviderList: () => {}, loadEffectiveDefaults: () => {},
                loadModel: k => { pendingLoads.push(handlers.loadModel(k)); },
            };
            new Function(...Object.keys(scope), `${compiledEffect}\ninitializeEditor();`)(...Object.values(scope));
            await Promise.all(pendingLoads);
            assert.equal(state.key, flow === 'edit' ? 'source-model' : 'source-model-copy');
            assert.equal(reads.length, flow === 'edit' ? 1 : 0);
            assert.equal(state.source, flow === 'edit' ? 'builtin' : 'user_data');
        },
        async save() {
            await render().handleSave();
        },
    };
}
(async () => {
    for (const flow of ['edit', 'duplicate']) {
        for (const field of [HIGH, TARGET]) {
            // Strings can be accepted by runtime int(value), but cannot be
            // represented losslessly by the dedicated numeric/null inputs.
            for (const bad of ['150000', '', 'none', true, false, {}, [], { nested: [1, null] }]) {
                const config = { model: 'original-api-model', [field]: bad, custom: { kept: true } };
                const original = structuredClone(config);
                const h = harness();
                await h.open(flow, config);
                assert.equal(h.state.watermarks[field], '');
                assert.deepEqual(JSON.parse(h.state.extraJson), { [field]: bad, custom: { kept: true } },
                    `${flow} must keep unsupported ${field}=${JSON.stringify(bad)} visible`);
                await h.save();
                assert.equal(h.requests.length, 0, `${flow} must not save a silently dropped value`);
                assert.match(h.error(), /text018/);
                assert.ok(h.error().includes(field));
                assert.deepEqual(JSON.parse(h.state.extraJson)[field], bad);
                assert.deepEqual(config, original, 'loading must not mutate the original model');

                // A deliberate correction in the visible dedicated field wins.
                h.state.watermarks[field] = '75000';
                await h.save();
                assert.equal(h.requests.length, 1);
                assert.equal(h.requests[0].method, flow === 'edit' ? 'PUT' : 'POST');
                assert.equal(h.requests[0].body.config[field], 75000);
                assert.deepEqual(h.requests[0].body.config.custom, { kept: true });
                assert.deepEqual(JSON.parse(h.state.extraJson), { custom: { kept: true } });
                if (flow === 'duplicate') assert.equal(h.requests[0].body.key, 'source-model-copy');
            }
        }
        for (const watermarks of [{}, { [HIGH]: 150000, [TARGET]: 75000 }, { [HIGH]: null, [TARGET]: null }]) {
            const config = { model: 'original-api-model', context_length: 128000, ...watermarks,
                custom: { kept: true }, metabolism_low_chars: 6000 };
            const h = harness();
            await h.open(flow, config);
            await h.save();
            assert.equal(h.error(), null);
            assert.deepEqual(h.requests[0].body.config, config, `${flow} must round-trip supported values`);
            for (const field of [HIGH, TARGET]) assert.equal(Object.hasOwn(JSON.parse(h.state.extraJson), field), false);
        }
        // Correcting or explicitly removing the retained JSON also recovers.
        for (const correction of [{ [HIGH]: 150000 }, {}]) {
            const h = harness();
            await h.open(flow, { model: 'original-api-model', [HIGH]: '150000' });
            await h.save();
            await h.save();
            assert.equal(h.requests.length, 0);
            assert.equal(JSON.parse(h.state.extraJson)[HIGH], '150000');
            h.state.extraJson = JSON.stringify(correction);
            await h.save();
            assert.equal(h.error(), null);
            assert.equal(h.requests.length, 1);
            assert.equal(Object.hasOwn(h.requests[0].body.config, HIGH), Object.hasOwn(correction, HIGH));
            assert.equal(h.requests[0].body.config[HIGH], correction[HIGH]);
        }
    }
    for (const mode of ['create', 'edit']) {
        const h = harness({ mode, extra: { [HIGH]: 150000, [TARGET]: 75000, custom: { kept: true } } });
        await h.save();
        assert.equal(h.requests.length, 1);
        assert.equal(h.requests[0].method, mode === 'create' ? 'POST' : 'PUT');
        assert.equal(h.requests[0].body.config[HIGH], 150000);
        assert.equal(h.requests[0].body.config[TARGET], 75000);
        assert.deepEqual(h.requests[0].body.config.custom, { kept: true });
        assert.deepEqual(h.state.watermarks, { [HIGH]: '150000', [TARGET]: '75000' });
        assert.deepEqual(JSON.parse(h.state.extraJson), { custom: { kept: true } });
    }
    const nulls = harness({ extra: { [HIGH]: null, [TARGET]: null } });
    await nulls.save();
    assert.equal(nulls.requests[0].body.config[HIGH], null);
    assert.equal(nulls.state.watermarks[HIGH], 'none');

    // Visible fields win, even when the discarded JSON duplicate is invalid.
    const explicit = harness({ extra: { [HIGH]: 'invalid', [TARGET]: 999999 }, watermarks: { [HIGH]: '200000', [TARGET]: 'none' } });
    await explicit.save();
    assert.equal(explicit.requests[0].body.config[HIGH], 200000);
    assert.equal(explicit.requests[0].body.config[TARGET], null);
    assert.equal(explicit.state.extraJson, '{}');

    for (const bad of ['150000', true, false, {}, [], -1, 0, 1.5, 1e100]) {
        const h = harness({ extra: { [HIGH]: bad } });
        await h.save();
        assert.equal(h.requests.length, 0, `must reject ${JSON.stringify(bad)}`);
        assert.match(h.error(), /text018/);
        assert.deepEqual(JSON.parse(h.state.extraJson), { [HIGH]: bad });
    }
    const whitespace = harness({ extra: { [HIGH]: 150000 }, watermarks: { [HIGH]: '  ' } });
    await whitespace.save();
    assert.equal(whitespace.requests[0].body.config[HIGH], 150000);

    // Ordering uses effective defaults and imported values, before API submission.
    const ordering = harness({ extra: { [HIGH]: 10000 } });
    await ordering.save();
    assert.equal(ordering.requests.length, 0);
    assert.match(ordering.error(), /text019/);
    assert.equal(ordering.state.watermarks[HIGH], '10000');
    assert.equal(ordering.state.extraJson, '{}');
    ordering.state.watermarks[HIGH] = '';
    await ordering.save();
    assert.equal(ordering.requests.length, 1);
    assert.equal(Object.hasOwn(ordering.requests[0].body.config, HIGH), false);

    // Failed requests and repeated saves cannot resurrect an imported value.
    const retry = harness({ fail: true, extra: { [HIGH]: null, [TARGET]: null, custom: 7 } });
    await retry.save();
    retry.state.watermarks[HIGH] = '';
    retry.state.watermarks[TARGET] = '';
    await retry.save();
    assert.equal(retry.requests.length, 2);
    assert.equal(Object.hasOwn(retry.requests[1].body.config, HIGH), false);
    assert.equal(Object.hasOwn(retry.requests[1].body.config, TARGET), false);
    assert.equal(retry.requests[1].body.config.custom, 7);

    // Absence and retired fields keep their established behavior.
    const untouched = harness({ extra: { metabolism_low_chars: 6000 } });
    await untouched.save();
    assert.equal(Object.hasOwn(untouched.requests[0].body.config, HIGH), false);
    assert.equal(untouched.requests[0].body.config.metabolism_low_chars, 6000);
    console.log('Model editor watermark edit/duplicate load -> save, import, validation, precedence, and retry tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
