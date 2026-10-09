/* Run explicitly: node scripts/test-pulse-timeline-theme.cjs
 * Isolated React element/handler regression checks, not a browser visual test. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const css = fs.readFileSync(path.join(__dirname, '../src/components/memory/PulseTimelineViewer.module.css'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../src/components/memory/PulseTimelineViewer.tsx'), 'utf8');
assert.doesNotMatch(source, /#[0-9a-f]{3,8}\b|rgba?\(/i, 'Timeline inline styles must follow theme variables');
for (const token of ['--timeline-main', '--timeline-sub', '--timeline-warning', '--timeline-gap', '--timeline-discardable', '--timeline-spell']) {
    assert.equal(css.split(`${token}:`).length - 1, 2, `${token}: light default and dark override`);
}
assert.match(css, /\[data-theme="dark"\]/);
assert.match(css, /\.viewer option\s*\{[^}]*color: var\(--text-primary\);[^}]*background: var\(--bg-tertiary\)/);
assert.match(css, /overflow-wrap: anywhere/);
assert.match(css, /overflow-x: hidden/);
const longText = 'Synthetic 日本語 message with a long identifier '.repeat(80);
const items = [{ pulse_id: 'fixture-pulse', line_roles: ['main_line', 'sub_line', 'meta_judgment'], message_count: 1, last_created_at: 1790967600 }];
const detail = { messages: [{ entry_id: 'fixture-msg', role: 'assistant', content: longText, created_at: 1790967600, line_role: 'main_line', scope: 'committed', spell_origin_id: 'fixture-spell', spell_seq: 1 }], prompts: [{ node_id: 'fixture-msg', content: longText, created_at: 1790967600 }], gap_messages: [{ entry_id: 'fixture-gap', role: 'user', content: longText, created_at: 1790967590 }] };
let cursor = 0;
const state = [];
const requests = [];
let deferDetail = false;
let resolveDetail;
const hooks = { ...React, useEffect() {}, useCallback: callback => callback, useState(initial) {
    const i = cursor++;
    if (!(i in state)) state[i] = initial;
    return [state[i], value => { state[i] = typeof value === 'function' ? value(state[i]) : value; }];
} };
const moduleObject = { exports: {} };
const customRequire = name => {
    if (name === 'react') return hooks;
    if (name === '@/i18n/useLocale') return { useLocale() {} };
    if (name === '@/i18n/core') return { getFormatLocale: () => 'en-US', t: key => key };
    if (name.endsWith('.css')) return { viewer: 'viewer', content: 'content' };
    if (name === '@/i18n/api') return { apiFetch: async (url, options) => { requests.push({ url, options }); if (deferDetail && !url.endsWith('?limit=200')) await new Promise(resolve => { resolveDetail = resolve; }); return { ok: true, json: async () => url.endsWith('?limit=200') ? { items } : detail }; } };
    return require(name);
};
new Function('require', 'module', 'exports', ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText)(customRequire, moduleObject, moduleObject.exports);
const render = () => { cursor = 0; return moduleObject.exports.default({ personaId: 'synthetic-only' }); };
const nodes = tree => {
    if (!tree || typeof tree !== 'object') return [];
    if (Array.isArray(tree)) return tree.flatMap(nodes);
    return [tree, ...nodes(tree.props?.children)];
};
const find = (tree, predicate) => nodes(tree).find(predicate);
const header = tree => find(tree, n => n.type === 'div' && n.props.onClick);
(async () => {
    let tree = render();
    await find(tree, n => n.type === 'button').props.onClick();
    tree = render();
    assert.equal(tree.props.className, 'viewer');
    await header(tree).props.onClick();
    tree = render();
    assert.equal(nodes(tree).filter(n => n.type === 'select').length, 2);
    assert.equal(nodes(tree).filter(n => n.props?.className === 'content').length, 3, 'message, prompt and gap text wrap');
    const selectors = nodes(tree).filter(n => n.type === 'select');
    assert.deepEqual(nodes(selectors[0].props.children).map(n => n.props.value), ['main_line', 'sub_line', 'meta_judgment']);
    selectors[0].props.onChange({ target: { value: 'sub_line' } });
    tree = render();
    assert.equal(find(tree, n => n.type === 'select').props.value, 'sub_line');
    assert.equal(nodes(tree).filter(n => n.type === 'button').length, 2, 'pending edit exposes Save');
    await header(tree).props.onClick();
    assert.equal(nodes(render()).filter(n => n.type === 'select').length, 0, 'collapse');
    await header(render()).props.onClick();
    assert.equal(find(render(), n => n.type === 'select').props.value, 'sub_line', 'reopen preserves pending edit');
    assert.equal(requests.filter(r => !r.url.endsWith('?limit=200')).length, 1, 'reopen uses cached detail');
    state.length = 0;
    deferDetail = true;
    await find(render(), n => n.type === 'button').props.onClick();
    const pending = header(render()).props.onClick();
    await header(render()).props.onClick();
    resolveDetail();
    await pending;
    assert.equal(nodes(render()).filter(n => n.type === 'select').length, 0, 'late detail response must not reopen a collapsed card');
    await header(render()).props.onClick();
    assert.equal(nodes(render()).filter(n => n.type === 'select').length, 2, 'reopen after interrupted load uses completed detail');
    assert.ok(requests.every(r => !r.options?.method), 'test never writes or invokes a job');
    console.log('Pulse timeline theme and isolated expand/edit/collapse/reopen checks passed. Browser visuals remain a separate check.');
})().catch(error => { console.error(error); process.exitCode = 1; });
