// Spell results render as plain text: no external images, no links, no Markdown.
// Actual TS module + actual ReactMarkdown / remark / rehype pipeline (same plugins
// and the same sanitize additions as page.tsx); no backend.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function loadTs(relativePath) {
  const source = fs.readFileSync(path.join(__dirname, relativePath), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const moduleObject = { exports: {} };
  new Function('module', 'exports', 'require', compiled)(moduleObject, moduleObject.exports, require);
  return moduleObject.exports;
}

const { prepareMessageMarkdown: prepare } = loadTs('../src/lib/messageMarkdown.ts');

// Same shape as sea/runtime_llm.py _build_spell_user_only_block (body is
// HTML-escaped for & < > there).
const escape = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const spellBlock = (result, cls = 'spellResult') =>
  `<user_only alt="ページ概要">\n/spell name='read_url_outline' args={"url": "https://saiverse.net/"}\n` +
  `<details class="${cls}"><summary class="spellSummary"><span class="spellIcon">` +
  `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">` +
  `<path d="M12 2L15 8z"/></svg></span><span>ページ概要</span></summary>\n\n` +
  `${escape(result)}\n\n</details>\n</user_only>\n`;

(async () => {
  const { default: Markdown } = await import('react-markdown');
  const { default: gfm } = await import('remark-gfm');
  const { default: breaks } = await import('remark-breaks');
  const { default: raw } = await import('rehype-raw');
  const { default: sanitize, defaultSchema } = await import('rehype-sanitize');
  const schema = {
    ...defaultSchema,
    tagNames: [...(defaultSchema.tagNames || []), 'svg', 'path'],
    attributes: {
      ...defaultSchema.attributes,
      details: [...(defaultSchema.attributes?.details || []), 'className'],
      summary: [...(defaultSchema.attributes?.summary || []), 'className'],
      span: [...(defaultSchema.attributes?.span || []), 'className'],
      code: [...(defaultSchema.attributes?.code || []), 'className'],
      svg: ['width', 'height', 'viewBox', 'fill', 'stroke', 'strokeWidth', 'stroke-width', 'className'],
      path: ['d', 'fill', 'stroke', 'strokeWidth', 'stroke-width'],
    },
  };
  const render = text => renderToStaticMarkup(React.createElement(Markdown, {
    remarkPlugins: [gfm, breaks],
    rehypePlugins: [raw, [sanitize, schema]],
    children: prepare(text),
  }));
  const decodeHtml = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');

  // A read_url_outline result: Markdown with images, links, headings, HTML-ish text.
  const result = [
    '## https://saiverse.net/ の内容（全 4786 文字）',
    '',
    '[Skip to content](#VPContent)',
    '![SAIVerse](https://saiverse.net/logo.png)',
    '# SAIVerse',
    '- [ガイド](https://saiverse.net/guide/why-saiverse)',
    'https://example.test/bare',
    '<script>alert(1)</script> & a < b > c, already &lt;escaped&gt;',
    'code: ```inner fence```',
  ].join('\n');
  const html = render(`前の本文\n\n${spellBlock(result)}\n後の本文`);

  // Inside the result box (the /spell line above it is the persona's own
  // invocation, not the result, and is out of scope here).
  const box = html.match(/<details class="spellResult">[\s\S]*?<\/details>/);
  assert(box, html);
  assert(!box[0].includes('<img'), `no image is rendered: ${box[0]}`);
  assert(!box[0].includes('<a '), `no link is rendered: ${box[0]}`);
  assert(!/<h[1-6]/.test(box[0]), `no heading is rendered: ${box[0]}`);
  assert(!html.includes('<script'), html);
  // The result text is shown verbatim, character for character.
  const pre = html.match(/<pre><code class="language-text">([\s\S]*?)<\/code><\/pre>/);
  assert(pre, `result is one plain-text block: ${html}`);
  assert.equal(decodeHtml(pre[1]).replace(/\n$/, ''), result);
  // Prose around the spell block is still Markdown.
  assert(html.includes('<p>前の本文</p>') && html.includes('<p>後の本文</p>'), html);

  // PR #481 review: a result that itself contains user_only tags keeps them;
  // only the real wrapper around the spell block is removed.
  const tagged = render(spellBlock('前 <user_only alt="sample">visible</user_only> 後'));
  const taggedPre = tagged.match(/<pre><code class="language-text">([\s\S]*?)<\/code><\/pre>/);
  assert.equal(decodeHtml(taggedPre[1]).replace(/\n$/, ''), '前 <user_only alt="sample">visible</user_only> 後');
  assert(!/<p>[^<]*user_only/.test(tagged.split('<details')[0]), `real wrapper is removed: ${tagged}`);

  // The misfire variant is shown the same way.
  const error = render(spellBlock('エラー: [詳細](https://x.test/)', 'spellResult spellResultError'));
  const errorBox = error.match(/<details class="spellResult spellResultError">[\s\S]*?<\/details>/)[0];
  assert(!errorBox.includes('<a ') && errorBox.includes('エラー: [詳細](https://x.test/)'), errorBox);

  // Legacy records whose result already is a fenced block keep one fence
  // (and the boundary repair still keeps later prose outside the disclosure).
  const legacy = '<details class="spellResult"><summary class="spellSummary">x</summary>```\n![a](https://x.test/a.png)\n```</details>\n\n後の本文';
  const legacyHtml = render(legacy);
  assert(!legacyHtml.includes('<img') && legacyHtml.includes('![a](https://x.test/a.png)'), legacyHtml);
  assert(/<\/details>[\s\S]*<p>後の本文<\/p>/.test(legacyHtml), legacyHtml);

  // Messages without spell results are untouched.
  assert.equal(prepare('普通の文 ![a](https://x.test/a.png)'), '普通の文 ![a](https://x.test/a.png)');

  // The CSS strips the code-block look inside the spell result box.
  const css = fs.readFileSync(path.join(__dirname, '../src/app/page.module.css'), 'utf8');
  assert(/\.cardBody :global\(\.spellResult\) pre,\s*\.cardBody :global\(\.spellResult\) pre code\s*\{[^}]*font-family: inherit/.test(css));

  console.log('spell result markdown: results render as verbatim plain text without images or links');
})().catch(error => { console.error(error); process.exitCode = 1; });
