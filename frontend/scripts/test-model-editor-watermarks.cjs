// Exercise the actual save handler, with local state setters and a fake API.
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
const compiled = ts.transpileModule(
    ['WATERMARK_FIELDS', 'watermarkFieldFromConfig', 'handleSave'].map(declaration).join('\n'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } },
).outputText;
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
    let error = null;
    const mocks = {
        uiText: (key, params) => `${key}:${JSON.stringify(params ?? {})}`,
        setSaveError: value => { error = value; },
        setWatermarks: value => { state.watermarks = value; },
        setExtraJson: value => { state.extraJson = value; },
        setSaving: () => {}, onSaved: () => {}, onClose: () => {},
        alert: () => { throw new Error('unexpected alert'); },
        apiFetch: async (url, options) => {
            requests.push({ url, method: options.method, body: JSON.parse(options.body) });
            return { ok: !fail, status: fail ? 500 : 200, text: async () => 'failed', json: async () => ({}) };
        },
    };
    return {
        state, requests, error: () => error,
        async save() {
            // Recreate the closure from current state, as React does on render.
            const scope = { ...state, ...mocks };
            const save = new Function(...Object.keys(scope), `${compiled}\nreturn handleSave;`)(...Object.values(scope));
            await save();
        },
    };
}
(async () => {
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
    console.log('Model editor watermark import, validation, precedence, and retry tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
