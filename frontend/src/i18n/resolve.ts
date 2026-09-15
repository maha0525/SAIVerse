import { getLocale } from './core';

export type I18nTextValue = string | Record<string, string> | null | undefined;

export function normalizeI18nDict(
    value: I18nTextValue,
    altEn?: string | null,
    altJa?: string | null,
): Record<string, string> {
    const result: Record<string, string> = {};

    if (value && typeof value === 'object') {
        for (const [k, v] of Object.entries(value)) {
            if (typeof v === 'string' && v.trim()) {
                result[k.trim()] = v.trim();
            }
        }
    } else if (typeof value === 'string' && value.trim()) {
        result.ja = value.trim();
    }

    if (altJa && altJa.trim() && !result.ja) {
        result.ja = altJa.trim();
    }

    if (altEn && altEn.trim() && !result.en) {
        result.en = altEn.trim();
    }

    return result;
}

export function resolveI18nText(
    value: I18nTextValue,
    targetLang?: string,
    altEn?: string | null,
    altJa?: string | null,
    fallback = '',
): string {
    const lang = targetLang || getLocale();
    const dict = normalizeI18nDict(value, altEn, altJa);

    // 1. Target language match
    if (dict[lang]?.trim()) {
        return dict[lang].trim();
    }

    // 2. English (lingua franca) fallback when target is not en
    if (lang !== 'en' && dict.en?.trim()) {
        return dict.en.trim();
    }

    // 3. Japanese (base language) fallback
    if (dict.ja?.trim()) {
        return dict.ja.trim();
    }

    // 4. Any first non-empty value
    for (const v of Object.values(dict)) {
        if (v && v.trim()) return v.trim();
    }

    // 5. If value was string and nothing in dict, return original string
    if (typeof value === 'string' && value.trim()) {
        return value.trim();
    }

    return fallback;
}
