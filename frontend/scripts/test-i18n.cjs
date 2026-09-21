const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const cache = new Map();
function load(file) {
    file = path.resolve(__dirname, '../src/i18n', file);
    if (cache.has(file)) return cache.get(file);
    const module = { exports: {} };
    cache.set(file, module.exports);
    const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true }
    }).outputText;
    const localRequire = name => name.endsWith('.json') ? require(path.resolve(path.dirname(file), name)) : load(name + '.ts');
    new Function('require', 'module', 'exports', output)(localRequire, module, module.exports);
    cache.set(file, module.exports);
    return module.exports;
}
const core = load('core.ts'), api = load('api.ts');
const saved = new Map();
global.window = {};
global.localStorage = { setItem: (key, value) => saved.set(key, value) };
global.document = { documentElement: { lang: 'ja' } };
let notifications = 0;
const unsubscribe = core.subscribeLocale(() => notifications++);
const key = 'language.ui';
const data = api.decodeUIMessages({ label: { $ui: key, params: {} }, content: { $ui: key, params: {} }, name: '原文', metadata: { nested: { $ui: key, params: {} } } });
assert.equal(data.label, core.t(key, {}, 'ja'));
core.setLocale('en');
assert.equal(document.documentElement.lang, 'en');
assert.equal(saved.get(core.LOCALE_STORAGE_KEY), 'en');
assert.equal(notifications, 1);
assert.equal(data.label, core.t(key, {}, 'en'));
assert.equal(data.content.$ui, key);
assert.equal(data.metadata.nested.$ui, key);
assert.equal(data.name, '原文');
assert.equal(core.getFormatLocale(), 'en-US');
const nested = api.decodeUIMessages({ $ui: 'api.tutorial.geminiRequired', params: { role: { $ui: 'api.tutorial.text014', params: {} } } });
assert.ok(nested.includes(core.t('api.tutorial.text014')));
assert.ok(!nested.includes('[object Object]'));
const params = { p1: '<script>literal</script>{p2}' };
const interpolated = core.t('api.people.config.text003', params);
assert.ok(interpolated.includes(params.p1)); // One pass; user text never becomes markup or another placeholder.
assert.equal(core.t('components.ActionsPanel.text019'), '${name}');
core.setLocale('invalid');
assert.equal(core.getLocale(), 'en');
unsubscribe();
core.setLocale('ja', false);
assert.equal(notifications, 1);
assert.equal(saved.get(core.LOCALE_STORAGE_KEY), 'en');
console.log('Locale persistence, reactive messages, interpolation, and authored-data boundaries passed.');
