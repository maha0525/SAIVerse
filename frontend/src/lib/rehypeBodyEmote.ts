import type { Element, ElementContent, Root, RootContent } from 'hast';

/**
 * Show the Godot virtual-body emote marker `{"body_emote": "smile"}` as a
 * subdued `(smile)` in chat bubbles.
 *
 * This runs on the parsed HTML tree (after rehype-raw and rehype-sanitize), not
 * on the Markdown source, so code is excluded by structure: nothing inside a
 * `<code>` or `<pre>` element is touched.  A persona explaining the syntax in
 * inline code, fenced blocks (any fence length, closed or still streaming),
 * indented code blocks, or multi-line code spans keeps it verbatim.
 *
 * Only the nine allowlisted names become a `<span class="body-emote">`; any
 * other name or malformed JSON stays as the original text.  The span is created
 * after sanitizing, so its content is limited to these fixed names.  The
 * persisted message text is never changed.
 */
const BODY_EMOTE_RE = /\{\s*"body_emote"\s*:\s*"(?:\\.|[^"\\])*"\s*\}/;
const BODY_EMOTES = new Set(['smile', 'nod', 'joy', 'hug', 'happy', 'wave', 'think', 'surprised', 'laugh']);
const SKIPPED_TAGS = new Set(['code', 'pre']);

function emoteName(marker: string): string | null {
    try {
        const name: unknown = JSON.parse(marker).body_emote;
        return typeof name === 'string' && BODY_EMOTES.has(name) ? name : null;
    } catch {
        return null;
    }
}

function splitText(value: string): ElementContent[] | null {
    const pieces: ElementContent[] = [];
    let last = 0;
    let replaced = false;
    const re = new RegExp(BODY_EMOTE_RE.source, 'g');
    let match: RegExpExecArray | null;
    while ((match = re.exec(value)) !== null) {
        const name = emoteName(match[0]);
        if (name === null) continue;
        const start = match.index;
        if (start > last) pieces.push({ type: 'text', value: value.slice(last, start) });
        pieces.push({
            type: 'element',
            tagName: 'span',
            properties: { className: ['body-emote'] },
            children: [{ type: 'text', value: `(${name})` }],
        });
        last = start + match[0].length;
        replaced = true;
    }
    if (!replaced) return null;
    if (last < value.length) pieces.push({ type: 'text', value: value.slice(last) });
    return pieces;
}

function transform(parent: Root | Element): void {
    const next: Array<RootContent | ElementContent> = [];
    let changed = false;
    for (const child of parent.children) {
        if (child.type === 'text') {
            const pieces = splitText(child.value);
            if (pieces) {
                next.push(...pieces);
                changed = true;
                continue;
            }
        } else if (child.type === 'element' && !SKIPPED_TAGS.has(child.tagName)) {
            transform(child);
        }
        next.push(child);
    }
    if (changed) parent.children = next as typeof parent.children;
}

export function rehypeBodyEmote() {
    return (tree: Root) => {
        transform(tree);
    };
}
