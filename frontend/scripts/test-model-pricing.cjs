const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const source = fs.readFileSync(path.join(__dirname, '../src/lib/modelPricing.ts'), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const mod = { exports: {} };
new Function('exports', compiled)(mod.exports);
const rate = mod.exports.outputRateForInput;
for (const [model, threshold, base, long] of [
    ['claude-haiku-5.5', 100000, 0.5, 2.5], ['grok-4.7', 200000, 6, 12],
]) {
    const pricing = JSON.parse(fs.readFileSync(path.join(__dirname, '../../builtin_data/models', `${model}.json`))).pricing;
    assert.equal(rate(pricing, threshold), base);
    assert.equal(rate(pricing, threshold + 1), long);
}
assert.equal(rate({ output_per_1m_tokens: 5, long_context_threshold_tokens: 10 }, 11), 5);
assert.equal(rate({ output_per_1m_tokens: 5 }, 999999), 5);
assert.equal(rate(undefined, 1), undefined);
assert.equal(rate({ long_context_threshold_tokens: true, long_context_output_per_1m_tokens: 10, output_per_1m_tokens: 5 }, 11), 5);
const modal = fs.readFileSync(path.join(__dirname, '../src/components/ContextPreviewModal.tsx'), 'utf8');
assert.match(modal, /outputRateForInput\(persona\.pricing, persona\.total_input_tokens\)/);
console.log('model pricing boundary tests passed');
