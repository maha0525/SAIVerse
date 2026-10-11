// Actual TS preparation + actual ReactMarkdown/rehype sanitize; no backend.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const source = fs.readFileSync(path.join(__dirname, '../src/lib/messageMarkdown.ts'), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const moduleObject = { exports: {} };
new Function('module', 'exports', compiled)(moduleObject, moduleObject.exports);
const prepare = moduleObject.exports.prepareMessageMarkdown;

(async () => {
  const { default: Markdown } = await import('react-markdown');
  const { default: raw } = await import('rehype-raw');
  const { default: sanitize, defaultSchema } = await import('rehype-sanitize');
  const schema = { ...defaultSchema, attributes: { ...defaultSchema.attributes,
    span: [...(defaultSchema.attributes?.span || []), 'className'] } };
  for (const name of ['smile', 'nod', 'joy', 'hug', 'happy', 'wave', 'think', 'surprised', 'laugh']) {
    const original = `こんにちは {"body_emote": "${name}"} 続き`;
    const prepared = prepare(original);
    const html = renderToStaticMarkup(React.createElement(Markdown, {
      rehypePlugins: [raw, [sanitize, schema]], children: prepared,
    }));
    assert(html.includes(`<span class="body-emote">(${name})</span>`), html);
    assert(!html.includes('body_emote'));
    assert(original.includes('body_emote')); // render-only transformation
  }
  assert.equal(prepare('普通の文'), '普通の文');
  assert(prepare('{"body_emote":"\\u006e\\u006f\\u0064"}').includes('(nod)'));
  const attack = prepare('{"body_emote":"<script>alert(1)</script>"}');
  const html = renderToStaticMarkup(React.createElement(Markdown, { rehypePlugins: [raw, [sanitize, schema]], children: attack }));
  assert(!html.includes('<script>'));
  const css = fs.readFileSync(path.join(__dirname, '../src/app/globals.css'), 'utf8');
  assert(css.includes('.body-emote') && /\.body-emote\s*\{[^}]*font-size: 0\.8em/.test(css));
  console.log('body emote markdown: all 9 markers render safely with class and subdued CSS');
})().catch(error => { console.error(error); process.exitCode = 1; });
