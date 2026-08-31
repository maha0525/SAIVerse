const SPELL_RESULT_BLOCK_RE =
    /<details\b[^>]*class="[^"]*\bspellResult\b[^"]*"[^>]*>[\s\S]*?<\/details>/g;

/**
 * Prepare persisted chat content for the shared ReactMarkdown renderer.
 *
 * Spell results created before the Markdown-boundary fix could serialize a
 * fenced result as ``</summary>```...```</details>``.  Markdown treats the
 * final `````</details>`` line as a new opening fence whose info string is
 * ``</details>``, so all following persona prose is swallowed by the
 * disclosure.  Repair only that legacy spellResult shape at render time; the
 * persisted building message remains untouched.
 */
export function prepareMessageMarkdown(text: string): string {
    if (!text) return text;

    const withLegacyBoundaries = text.replace(
        SPELL_RESULT_BLOCK_RE,
        (block) => block
            .replace(/<\/summary>(?=[`~]{3,})/, '</summary>\n\n')
            .replace(/([`~]{3,})<\/details>$/, '$1\n\n</details>'),
    );

    return withLegacyBoundaries
        .replace(/<user_only(?:\s[^>]*)?>/g, '')
        .replace(/<\/user_only>/g, '');
}
