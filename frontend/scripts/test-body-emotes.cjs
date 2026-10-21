// Actual TS modules + actual ReactMarkdown / remark / rehype pipeline (same
// plugins and order as page.tsx); no backend.
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
const { rehypeBodyEmote } = loadTs('../src/lib/rehypeBodyEmote.ts');
const EMOTES = ['smile', 'nod', 'joy', 'hug', 'happy', 'wave', 'think', 'surprised', 'laugh'];

(async () => {
  const { default: Markdown } = await import('react-markdown');
  const { default: gfm } = await import('remark-gfm');
  const { default: breaks } = await import('remark-breaks');
  const { default: raw } = await import('rehype-raw');
  const { default: sanitize, defaultSchema } = await import('rehype-sanitize');
  const schema = { ...defaultSchema, attributes: { ...defaultSchema.attributes,
    span: [...(defaultSchema.attributes?.span || []), 'className'],
    code: [...(defaultSchema.attributes?.code || []), 'className'] } };
  const render = text => renderToStaticMarkup(React.createElement(Markdown, {
    remarkPlugins: [gfm, breaks],
    rehypePlugins: [raw, [sanitize, schema], rehypeBodyEmote],
    children: prepare(text),
  }));
  const span = name => `<span class="body-emote">(${name})</span>`;
  // Code contents stay verbatim: neither a real span nor an escaped tag string.
  const assertVerbatimCode = (html, marker) => {
    assert(!html.includes('class="body-emote"'), html);
    assert(!html.includes('&lt;span'), html);
    assert(html.includes(marker.replace(/"/g, '&quot;')), html);
  };

  // page.tsx must use the same plugin, after sanitize.
  const page = fs.readFileSync(path.join(__dirname, '../src/app/page.tsx'), 'utf8');
  assert(/MARKDOWN_REHYPE_PLUGINS[^=]*=\s*\[rehypeRaw,\s*\[rehypeSanitize,\s*sanitizeSchema\],\s*rehypeBodyEmote\]/.test(page),
    'page.tsx MARKDOWN_REHYPE_PLUGINS must end with rehypeBodyEmote after sanitize');

  for (const name of EMOTES) {
    const original = `こんにちは {"body_emote": "${name}"} 続き`;
    const html = render(original);
    assert(html.includes(`こんにちは ${span(name)} 続き`), html);
    assert(!html.includes('body_emote'), html);
    assert(original.includes('body_emote')); // render-only transformation
  }

  assert.equal(prepare('普通の文'), '普通の文');
  assert.equal(render('普通の文'), '<p>普通の文</p>');
  assert(render('{"body_emote":"\\u006e\\u006f\\u0064"}').includes(span('nod')));

  // Not allowlisted / malformed / injection attempts stay as text.
  const unknown = render('{"body_emote": "dance"}');
  assert(!unknown.includes('body-emote') && unknown.includes('body_emote'), unknown);
  const broken = render('{"body_emote": "smi\\le"}');
  assert(!broken.includes('class="body-emote"'), broken);
  const attack = render('{"body_emote":"<script>alert(1)</script>"} <script>alert(2)</script>');
  assert(!attack.includes('<script>') && !attack.includes('class="body-emote"'), attack);

  const marker = '{"body_emote": "smile"}';
  const cases = {
    inline: `書き方は \`${marker}\` だよ`,
    multilineInline: `書き方は \`前\n${marker}\n後\` だよ`,
    fenced: `\`\`\`\n${marker}\n\`\`\``,
    fourBacktickFenceAroundThree: `\`\`\`\`\n\`\`\`\n${marker}\n\`\`\`\n\`\`\`\``,
    unclosedFence: `説明\n\n\`\`\`json\n${marker}\n`,
    indented: `説明\n\n    ${marker}\n`,
    tildeFence: `~~~\n${marker}\n~~~`,
  };
  for (const [label, text] of Object.entries(cases)) {
    const html = render(text);
    assert(html.includes('<code'), `${label}: ${html}`);
    assertVerbatimCode(html, marker);
    // Outside code the marker still renders in the same message.
    const mixed = render(`${text}\n\n後 {"body_emote": "joy"}`);
    if (label === 'unclosedFence') {
      // An unclosed fence swallows the rest of the message: still verbatim.
      assertVerbatimCode(mixed, marker);
    } else {
      assert(mixed.includes(span('joy')), `${label}: ${mixed}`);
      assert.equal(mixed.split('class="body-emote"').length, 2, `${label}: ${mixed}`);
      assert(!mixed.includes('&lt;span'), `${label}: ${mixed}`);
    }
  }

  const css = fs.readFileSync(path.join(__dirname, '../src/app/globals.css'), 'utf8');
  assert(css.includes('.body-emote') && /\.body-emote\s*\{[^}]*font-size: 0\.8em/.test(css));
  console.log('body emote markdown: 9 markers render as subdued spans; code, unknown names and injections stay verbatim');
})().catch(error => { console.error(error); process.exitCode = 1; });
