// Executes the actual applyModelChange body with controlled, local fetch promises.
// No browser, network, model changes, or production writes are involved.
// Run: node tests/audit_20260730_model_change.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const ts = require('typescript');
const source = fs.readFileSync(new URL('../frontend/src/components/ChatOptions.tsx', import.meta.url), 'utf8');
const start = source.indexOf('    const applyModelChange = async');
const end = source.indexOf('    const handleParamChange', start);
assert(start >= 0 && end > start);
const code = ts.transpileModule(source.slice(start, end), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

let finishJson;
let jsonStarted;
const started = new Promise(resolve => { jsonStarted = resolve; });
const body = new Promise(resolve => { finishJson = resolve; });
const seq = { current: 1 };
let selected = 'model-a';
let server = 'model-a';
const noop = () => {};
const mock = {
    modelChangeSeqRef: seq,
    modelChangeClientIdRef: { current: 'audit-client' },
    modelResyncTimerRef: { current: null },
    models: [],
    setCurrentModel: value => { selected = value; },
    onModelChange: noop, setParamSpecs: noop, setParams: noop,
    setMaxImageEmbeds: noop, setMaxImageEmbedsDefault: noop,
    setContextStatus: noop, setContextStatusReload: noop, setCacheConfig: noop,
    setError: noop, fetchData: async () => {},
    fetch: async (url, options) => {
        if (url === '/api/config/cache') return { ok: true, json: async () => ({}) };
        const request = JSON.parse(options.body);
        server = request.model || null;
        if (request.seq === 1) return {
            ok: true, status: 200,
            json: () => { jsonStarted(); return body; },
        };
        return { ok: true, status: 200, json: async () => ({ current_model: null }) };
    },
};
const apply = new Function(...Object.keys(mock), code + '\nreturn applyModelChange;')(...Object.values(mock));
const first = apply('model-a', 1);
await started; // Old response passed the seq check but its JSON is still pending.
seq.current = 2;
selected = ''; // User selects automatic mode via handleModelChange.
finishJson({ current_model: 'model-a' });
await first;
assert.equal(selected, 'model-a'); // Old body overwrote the newer selection.
await apply('', 2); // The real chain now sends the newer automatic selection.
assert.equal(server, null);
assert.equal(selected, 'model-a'); // Success with null does not repair the display.
console.log(JSON.stringify({ server_model: server, displayed_model: selected,
    latest_seq: seq.current, reproduced: true }, null, 2));
