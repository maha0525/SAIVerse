'use client';
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { LOCALE_STORAGE_KEY, setLocale, isLocale } from './core';

export default function LocaleBridge() {
    const [hint, setHint] = useState<{ keys: string; x: number; y: number } | null>(null);
    const [developerMode, setDeveloperMode] = useState(false);
    useEffect(() => {
        const controller = new AbortController();
        fetch('/api/config/developer-mode', { signal: controller.signal })
            .then(response => response.ok ? response.json() : null)
            .then(data => { if (data) setDeveloperMode(data.enabled === true); })
            .catch(error => { if (!controller.signal.aborted) console.debug('[i18n] Inspector setting unavailable', error); });
        const update = (event: Event) => setDeveloperMode((event as CustomEvent<boolean>).detail === true);
        window.addEventListener('saiverse-developer-mode', update);
        return () => { controller.abort(); window.removeEventListener('saiverse-developer-mode', update); };
    }, []);
    useEffect(() => {
        try { const saved = localStorage.getItem(LOCALE_STORAGE_KEY); if (isLocale(saved)) setLocale(saved, false); }
        catch (error) { console.warn('[i18n] Cannot read display language', error); }
        const sync = (event: StorageEvent) => { if (event.key === LOCALE_STORAGE_KEY) setLocale(isLocale(event.newValue) ? event.newValue : 'ja', false); };
        window.addEventListener('storage', sync);
        return () => window.removeEventListener('storage', sync);
    }, []);
    useEffect(() => {
        if (!developerMode) return;
        const inspect = (event: PointerEvent) => {
            const element = event.target instanceof Element ? event.target.closest('[data-i18n]') : null;
            const keys = element?.getAttribute('data-i18n');
            setHint(keys ? { keys, x: Math.min(event.clientX + 12, window.innerWidth - 320), y: Math.min(event.clientY + 18, window.innerHeight - 90) } : null);
        };
        const clear = () => setHint(null);
        window.addEventListener('pointermove', inspect);
        window.addEventListener('blur', clear);
        document.addEventListener('pointerleave', clear);
        return () => { window.removeEventListener('pointermove', inspect); window.removeEventListener('blur', clear); document.removeEventListener('pointerleave', clear); };
    }, [developerMode]);
    if (!developerMode || !hint) return null;
    return createPortal(<div role="tooltip" style={{ position: 'fixed', left: Math.max(0, hint.x), top: Math.max(0, hint.y), zIndex: 2147483647, pointerEvents: 'none', background: '#18202c', color: '#fff', padding: 8, borderRadius: 4, fontSize: 12, maxWidth: 310, overflowWrap: 'anywhere' }}>frontend/src/i18n/messages.json<br />{hint.keys}</div>, document.body);
}
