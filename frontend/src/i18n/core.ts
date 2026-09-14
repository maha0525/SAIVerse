import messages from './messages.json';
import locales from './locales.json';

export type Locale = keyof typeof locales;
export const availableLocales = locales;
export const isLocale = (value: unknown): value is Locale => typeof value === 'string' && Object.hasOwn(locales, value);
export type Parameters = Record<string, unknown>;
export const LOCALE_STORAGE_KEY = 'saiverse-ui-language';
const catalog: Record<string, Record<Locale, string>> = messages;
let locale: Locale = 'ja';
const listeners = new Set<() => void>();
export const getLocale = () => locale;
export const getServerLocale = (): Locale => 'ja';
export const subscribeLocale = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
export function setLocale(value: Locale, persist = true) {
    if (!isLocale(value)) return;
    if (persist && typeof window !== 'undefined') {
        try { localStorage.setItem(LOCALE_STORAGE_KEY, value); }
        catch (error) { console.warn('[i18n] Cannot save display language', error); }
    }
    if (typeof document !== 'undefined') document.documentElement.lang = value;
    if (locale === value) return;
    locale = value;
    console.debug('[i18n] Display language changed:', value);
    listeners.forEach(listener => listener());
}
export function translate(key: string, params: Parameters = {}, language: Locale = locale): string {
    const entry = catalog[key];
    if (!entry) { console.warn('[i18n] Missing message:', key); return key; }
    return (entry[language] ?? entry.ja).replace(/(?<!\$)\{(\w+)\}/g, (match, name: string) =>
        Object.prototype.hasOwnProperty.call(params, name) ? String(params[name] ?? '') : match);
}
export const t = translate;
export const getFormatLocale = () => locales[locale].format;
export function formatNumber(value: number, options?: Intl.NumberFormatOptions) { return new Intl.NumberFormat(getFormatLocale(), options).format(value); }
