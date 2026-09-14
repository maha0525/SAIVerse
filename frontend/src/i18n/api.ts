import { t } from './core';

/** Only explicitly marked system messages are translated. User content stays verbatim. */
export function decodeUIMessages(value: unknown): any {
    if (Array.isArray(value)) return value.map(decodeUIMessages);
    if (!value || typeof value !== 'object') return value;
    const object = value as Record<string, unknown>;
    if (typeof object.$ui === 'string' && object.params && typeof object.params === 'object'
        && Object.keys(object).every(key => key === '$ui' || key === 'params')) {
        return t(object.$ui, decodeUIMessages(object.params));
    }
    const decoded: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(object)) {
        // These fields carry authored or structured data, not display messages.
        if (['content', 'metadata', 'system_prompt', 'nodes_json', 'schema_json', 'input_schema'].includes(key)) {
            decoded[key] = item;
        } else if (item && typeof item === 'object' && '$ui' in item) {
            // Retain the envelope behind a getter so fetched labels also follow a
            // language switch without fetching again or touching persona state.
            Object.defineProperty(decoded, key, { enumerable: true, get: () => decodeUIMessages(item) });
        } else {
            decoded[key] = decodeUIMessages(item);
        }
    }
    return decoded;
}

/** Preserve Response streaming, status and headers; decode only JSON UI envelopes. */
export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
    const response = await fetch(input, init);
    const json = response.json.bind(response);
    response.json = async () => decodeUIMessages(await json());
    return response;
}

export function parseUIEvent(text: string): any {
    return decodeUIMessages(JSON.parse(text));
}
