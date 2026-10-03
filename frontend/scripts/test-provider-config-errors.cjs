// Render the real management panels with isolated data; no browser/API/LLM runs.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const source = path.resolve(__dirname, '../src');
const messages = require('../src/i18n/messages.json');
const error = { path: '/isolated/user_data/providers/example.json', source: 'user_data', reason: 'protocol must name a supported protocol' };
function render(file, rows, fetchOverride) {
    const updates = [];
    const cache = new Map();
    let stateIndex = 0;
    const react = {
        ...React,
        useState: initial => {
            const index = stateIndex++;
            return [index === 0 ? rows : initial, value => { updates[index] = value; }];
        },
        useEffect: () => {}, useCallback: value => value, useRef: value => ({ current: value }),
    };
    function load(file) {
        if (cache.has(file)) return cache.get(file);
        const module = { exports: {} };
        const output = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
            compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true }
        }).outputText;
        const localRequire = name => {
            if (name === 'react') return react;
            if (name === '@/i18n/core') return { t: key => messages[key].ja };
            if (name === '@/i18n/useLocale') return { useLocale: () => 'ja' };
            if (name === '@/i18n/api') return { apiFetch: fetchOverride || (() => { throw new Error('Network forbidden'); }) };
            if (name.endsWith('.css')) return { __esModule: true, default: new Proxy({}, { get: (_, key) => key }) };
            if (name.startsWith('.') && name.includes('Modal')) return { __esModule: true, default: () => null };
            if (name.startsWith('.')) return load(path.resolve(path.dirname(file), `${name}.tsx`));
            return require(name);
        };
        new Function('require', 'module', 'exports', output)(localRequire, module, module.exports);
        cache.set(file, module.exports);
        return module.exports;
    }
    const tree = load(path.join(source, 'components/settings', file)).default();
    return { html: renderToStaticMarkup(tree), tree, updates };
}
const providerView = render('ProviderManagementPanel.tsx', [
    { id: 'example', display_name: 'example', protocol: 'invalid', source: 'user_data', config_error: error },
    { id: 'healthy', display_name: 'Healthy provider', protocol: 'openai_compat', source: 'builtin', builtin: true },
]);
const modelView = render('ModelManagementPanel.tsx', [
    { id: 'model', name: 'Visible unavailable model', provider: 'invalid', config_error: error },
    { id: 'healthy-model', name: 'Healthy model', provider: 'openai' },
]);
const providers = providerView.html, models = modelView.html;
for (const html of [providers, models]) {
    assert.ok(html.includes('role="alert"'));
    assert.ok(html.includes(error.path));
    assert.ok(html.includes(error.reason));
    assert.ok(html.includes(messages['providerConfig.invalid'].ja));
    assert.ok(html.includes(messages['providerConfig.repair'].ja));
}
assert.ok(providers.includes('Healthy provider'));
assert.ok(models.includes('Healthy model'));
assert.ok(models.includes('Visible unavailable model'));
assert.equal((providers.split('Healthy provider')[0].match(/disabled=""/g) || []).length, 2);
console.log('Provider/model panels render disabled definitions, safe diagnostics, and unrelated healthy rows.');


function findReload(element) {
    if (!React.isValidElement(element)) return null;
    if (element.props['data-i18n'] === 'components.settings.ProviderManagementPanel.text013') return element;
    return React.Children.toArray(element.props.children).map(findReload).find(Boolean);
}
(async () => {
    const requests = [];
    let finish;
    const view = render('ProviderManagementPanel.tsx', [], (url, init) => {
        requests.push({ url, init });
        return new Promise(resolve => { finish = resolve; });
    });
    const button = findReload(view.tree);
    assert.ok(button);
    const first = button.props.onClick();
    await button.props.onClick();  // a second click while the first is in flight
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/providers/reload');
    assert.equal(requests[0].init.method, 'POST');
    const repaired = [{ id: 'example', available: true, config_error: null }];
    finish({ ok: true, json: async () => repaired });
    await first;
    assert.equal(view.updates[0], repaired);
    const retry = button.props.onClick();
    assert.equal(requests.length, 2);  // successful completion releases the guard
    finish({ ok: false, status: 500 });
    const originalError = console.error;
    console.error = () => {};
    try { await retry; } finally { console.error = originalError; }
    assert.ok(view.updates.includes(messages['providerConfig.reloadFailed'].ja));
    const afterFailure = button.props.onClick();
    assert.equal(requests.length, 3);  // failure also releases the guard
    finish({ ok: true, json: async () => repaired });
    await afterFailure;
    console.log('Provider reload uses POST, restores repaired definitions, prevents double submission, and can retry failures.');
})().catch(error => { console.error(error); process.exitCode = 1; });
