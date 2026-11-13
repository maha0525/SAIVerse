const SPELL_RESULT_BLOCK_RE =
    /<details\b[^>]*class="[^"]*\bspellResult\b[^"]*"[^>]*>[\s\S]*?<\/details>/g;

// The result body sits between the summary (or the opening tag, if a record
// has no summary) and the closing tag.  This always matches a block found by
// SPELL_RESULT_BLOCK_RE, so no result body reaches the renderer as Markdown.
const SPELL_RESULT_BODY_RE =
    /^(<details\b[^>]*>(?:[\s\S]*?<\/summary>)?)([\s\S]*?)(<\/details>)$/;

/**
 * Turn one spell result body into a fenced block so it renders as plain text.
 *
 * A spell result is whatever the tool returned — for read_url_outline that is
 * a third-party web page converted to Markdown.  Rendered as Markdown, its
 * ``![](https://...)`` images are fetched by the user's browser from that
 * third-party site every time the chat is shown (weight, and the user's IP /
 * viewing time reach a site the persona merely read), and its links become
 * clickable.  The result is for the user to check what the spell returned, so
 * it is shown verbatim instead.  The persona's own copy of the result does not
 * pass through here.
 *
 * The backend HTML-escapes ``& < >`` in the body (sea/runtime_llm.py
 * ``_build_spell_user_only_block``).  Code blocks show entities literally, so
 * those three are decoded first (``&amp;`` last, the inverse of the encoder).
 *
 * The whole body is always wrapped, with a fence longer than any backtick run
 * inside it, so nothing in the body can close the block early.  A body that
 * already looks fenced is wrapped too: telling one fenced block apart from
 * several blocks with Markdown between them is exactly where an image could
 * slip out (PR #481 review).
 */
function toPlainTextBody(body: string): string {
    const trimmed = body.replace(/^\n+|\n+$/g, '');
    if (!trimmed) return body;
    const text = trimmed
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
    const longestRun = Math.max(0, ...(text.match(/`+/g) || []).map((run) => run.length));
    const fence = '`'.repeat(Math.max(3, longestRun + 1));
    return `\n\n${fence}text\n${text}\n${fence}\n\n`;
}

/**
 * Prepare persisted chat content for the shared ReactMarkdown renderer.
 *
 * Every spell result body is shown as plain text (see toPlainTextBody).  This
 * is done at render time so past records are covered too; the persisted
 * building message remains untouched.  It also covers records from before the
 * Markdown-boundary fix (``</summary>```...```</details>``, where the final
 * fence line used to swallow the persona prose after the disclosure): their
 * old fence lines simply become part of the plain text.
 */
export function prepareMessageMarkdown(text: string): string {
    if (!text) return text;

    // Strip the real <user_only> wrappers first, while the spell result bodies
    // are still HTML-escaped: a result that itself contains the text
    // "<user_only>" (e.g. a fetched page) must survive verbatim.
    const withoutWrappers = text
        .replace(/<user_only(?:\s[^>]*)?>/g, '')
        .replace(/<\/user_only>/g, '');

    return withoutWrappers.replace(
        SPELL_RESULT_BLOCK_RE,
        (block) => {
            const match = block.match(SPELL_RESULT_BODY_RE);
            if (!match) return block;
            return `${match[1]}${toPlainTextBody(match[2])}${match[3]}`;
        },
    );
}
