// The spinner text while a reply is pending: real lib/chatProgress.ts against the
// real message table. No server, browser, persona or LLM calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const messages = JSON.parse(fs.readFileSync(path.join(__dirname, '../src/i18n/messages.json'), 'utf8'));
const translate = (key, params = {}) => {
    assert.ok(messages[key], `message key exists: ${key}`);
    return messages[key].ja.replace(/\{(\w+)\}/g, (match, name) => (name in params ? String(params[name]) : match));
};
const compiled = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/lib/chatProgress.ts'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const mod = { exports: {} };
new Function('require', 'module', 'exports', compiled)(name => {
    if (name === '@/i18n/core') return { t: translate };
    return require(name);
}, mod, mod.exports);
const { describeChatProgress, progressTextForStatusEvent } = mod.exports;

// Developer breadcrumbs from playbook nodes never reach the screen.
assert.equal(progressTextForStatusEvent({ type: 'status', content: 'track_user_conversation / main_line_response', playbook: 'track_user_conversation', node: 'main_line_response' }), null);
assert.equal(progressTextForStatusEvent({ type: 'status', content: 'meta_user > track_user_conversation / main_line_response', playbook_chain: 'meta_user > track_user_conversation', node: 'main_line_response' }), null);
assert.equal(progressTextForStatusEvent({ type: 'status', content: 'meta_user > track_user_conversation / ', playbook_chain: 'meta_user > track_user_conversation' }), null);
// The stream's opening flush keeps whatever the send already shows.
assert.equal(progressTextForStatusEvent({ type: 'status', content: 'processing' }), null);
// Server-authored notices for the user pass through as written.
assert.equal(progressTextForStatusEvent({ type: 'status', content: '現在のコンテキストが長すぎるため、記憶の整理を行っています…' }), '現在のコンテキストが長すぎるため、記憶の整理を行っています…');

// Phases win over any breadcrumb content the runner's wrapper adds.
assert.equal(progressTextForStatusEvent({ type: 'status', phase: 'building_request', provider: 'OpenAI', content: 'a > b / ', playbook_chain: 'a > b' }), 'OpenAIサーバーへの送信データを構築しています');
assert.equal(progressTextForStatusEvent({ type: 'status', phase: 'awaiting_response', provider: 'OpenAI' }), 'OpenAIサーバーからの応答を待っています');
assert.equal(progressTextForStatusEvent({ type: 'status', phase: 'awaiting_response' }), 'AIサーバーからの応答を待っています');
assert.equal(progressTextForStatusEvent({ type: 'status', phase: 'running_tool', tool: '記憶のページを読む', persona_name: 'エリス' }), '「記憶のページを読む」を実行しています');
assert.equal(progressTextForStatusEvent({ type: 'status', phase: 'something_new' }), null, 'unknown phases leave the display alone');

assert.equal(describeChatProgress('recording'), 'あなたの発言をデータベースに記録しています');
assert.equal(describeChatProgress('uploading'), '添付ファイルをアップロードしています');
assert.equal(describeChatProgress('replying', { persona: 'エリス' }), 'エリスが返事をしています');
assert.equal(describeChatProgress('replying', { persona: '  ' }), '返事をしています');
assert.equal(describeChatProgress('thinking', { persona: 'エリス' }), 'エリスが考えています');
assert.equal(describeChatProgress('building_request', {}), 'AIサーバーへの送信データを構築しています');

// The page reads progress through the module, never the old developer labels.
const page = fs.readFileSync(path.join(__dirname, '../src/app/page.tsx'), 'utf8');
for (const legacy of ["'Processing...' : event.content", "setLoadingStatus('Thinking...')", "setLoadingStatus('Streaming...')", '`Running ${event.name}...`']) {
    assert.ok(!page.includes(legacy), `legacy spinner label removed: ${legacy}`);
}
// FileReader.readAsDataURL already yields a data URL; prefixing it again broke the
// optimistic image until the server row arrived.
assert.ok(!page.includes('data:${a.mimeType};base64,${a.base64}'), 'optimistic attachments use the data URL as-is');
assert.match(page, /readAsDataURL\(file\)/);
// The user bubble never falls back to a hard-coded English name.
assert.ok(!page.includes("msg.role === 'user' ? 'You'"), 'no hard-coded You fallback');

console.log('Chat progress: developer breadcrumbs hidden, phases localized with provider/persona names, server notices passed through, optimistic attachments and user name fixed.');
