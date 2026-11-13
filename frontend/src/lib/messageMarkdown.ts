const SPELL_RESULT_BLOCK_RE =
    /<details\b[^>]*class="[^"]*\bspellResult\b[^"]*"[^>]*>[\s\S]*?<\/details>/g;

// The result body sits between the summary and the closing tag.
const SPELL_RESULT_BODY_RE = /^([\s\S]*?<\/summary>)([\s\S]*?)(<\/details>)$/;
const FENCED_BODY_RE = /^([`~]{3,})[^\n]*\n[\s\S]*\n\1[`~]*$/;

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
 */
function toPlainTextBody(body: string): string {
    const trimmed = body.replace(/^\n+|\n+$/g, '');
    if (!trimmed) return body;
    // Legacy records already hold a fenced result; it is plain text already.
    if (FENCED_BODY_RE.test(trimmed)) return `\n\n${trimmed}\n\n`;
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
 * Spell results created before the Markdown-boundary fix could serialize a
 * fenced result as ``</summary>```...```</details>``.  Markdown treats the
 * final `````</details>`` line as a new opening fence whose info string is
 * ``</details>``, so all following persona prose is swallowed by the
 * disclosure.  Repair only that legacy spellResult shape at render time; the
 * persisted building message remains untouched.
 *
 * Every spell result body is then shown as plain text (see toPlainTextBody).
 * This is done at render time so past records are covered too.
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
            const repaired = block
                .replace(/<\/summary>(?=[`~]{3,})/, '</summary>\n\n')
                .replace(/([`~]{3,})<\/details>$/, '$1\n\n</details>');
            const match = repaired.match(SPELL_RESULT_BODY_RE);
            if (!match) return repaired;
            return `${match[1]}${toPlainTextBody(match[2])}${match[3]}`;
        },
    );
}
