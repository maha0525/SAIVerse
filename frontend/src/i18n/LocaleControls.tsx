'use client';
import { t, setLocale, availableLocales, type Locale } from './core';
import { useLocale } from './useLocale';
export default function LocaleControls() {
    const locale = useLocale();
    return <div style={{ padding: '12px 0' }}>
        <label data-i18n="language.ui">{t('language.ui')} <select value={locale} onChange={event => setLocale(event.target.value as Locale)}>
            {Object.entries(availableLocales).map(([code, language]) => <option key={code} value={code}>{language.name}</option>)}
        </select></label>
        <p data-i18n="language.help">{t('language.help')}</p>
    </div>;
}
