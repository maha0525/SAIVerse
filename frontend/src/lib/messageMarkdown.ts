const SPELL_RESULT_BLOCK_RE =
    /<details\b[^>]*class="[^"]*\bspellResult\b[^"]*"[^>]*>[\s\S]*?<\/details>/g;

const BODY_EMOTE_RE = /\{\s*"body_emote"\s*:\s*"(?:\\.|[^"\\])*"\s*\}/g;
const BODY_EMOTES = new Set(['smile', 'nod', 'joy', 'hug', 'happy', 'wave', 'think', 'surprised', 'laugh']);

function displayBodyEmote(marker: string): string {
    try {
        const name = JSON.parse(marker).body_emote;
        return BODY_EMOTES.has(name) ? `<span class="body-emote">(${name})</span>` : marker;
    } catch {
        return marker;
    }
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
        .replace(BODY_EMOTE_RE, displayBodyEmote)
        .replace(/<user_only(?:\s[^>]*)?>/g, '')
        .replace(/<\/user_only>/g, '');
}
