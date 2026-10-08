"use client";

import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/i18n/api';
import {
    applyMovementNoticeChange,
    didGlobalMovementNoticeSaveFail,
    isGlobalMovementNoticeSavePending,
    MOVEMENT_NOTICES_SAVE_STATE_CHANGED,
    MOVEMENT_NOTICES_CHANGED,
    MovementNoticeChange,
    MovementNoticeSettings,
} from '@/lib/movementNotices';

/** Keep display preferences separate from history, pagination and persona state. */
export function useMovementNoticeSettings() {
    const [settings, setSettings] = useState<MovementNoticeSettings | null>(null);
    const [loadError, setLoadError] = useState(false);
    const [saving, setSaving] = useState(isGlobalMovementNoticeSavePending);
    const [saveError, setSaveError] = useState(didGlobalMovementNoticeSaveFail);
    const generation = useRef(0);
    const active = useRef(false);
    const request = useRef<AbortController | null>(null);

    const refresh = useCallback(async () => {
        // A remounted control must wait for the prior write, not fetch a pre-save snapshot.
        if (isGlobalMovementNoticeSavePending()) return;
        request.current?.abort();
        const controller = new AbortController();
        request.current = controller;
        const current = ++generation.current;
        try {
            const response = await apiFetch('/api/config/movement-notices', {
                cache: 'no-store', signal: controller.signal,
            });
            if (!response.ok) throw new Error(`Movement notice settings: ${response.status}`);
            const data: MovementNoticeSettings = await response.json();
            if (!active.current || current !== generation.current) return;
            setSettings(data);
            setLoadError(false);
        } catch {
            if (!active.current || current !== generation.current || controller.signal.aborted) return;
            // Retain the last known display choice. The settings control shows the failure.
            setLoadError(true);
        }
    }, []);

    useEffect(() => {
        active.current = true;
        const version = generation;
        const onChanged = (event: Event) => {
            // A read started before a save must not undo that successful save.
            ++generation.current;
            request.current?.abort();
            const change = (event as CustomEvent<MovementNoticeChange>).detail;
            setSettings(previous => applyMovementNoticeChange(previous, change));
            if ('settings' in change) setLoadError(false);
            else void refresh();
        };
        const onSaveStateChanged = () => {
            const pending = isGlobalMovementNoticeSavePending();
            setSaving(pending);
            setSaveError(didGlobalMovementNoticeSaveFail());
            ++generation.current;
            request.current?.abort();
            if (!pending) void refresh();
        };
        const onVisible = () => { if (document.visibilityState === 'visible') void refresh(); };
        window.addEventListener(MOVEMENT_NOTICES_CHANGED, onChanged);
        window.addEventListener(MOVEMENT_NOTICES_SAVE_STATE_CHANGED, onSaveStateChanged);
        window.addEventListener('focus', refresh);
        document.addEventListener('visibilitychange', onVisible);
        void refresh();
        const timer = window.setInterval(onVisible, 15_000);
        return () => {
            active.current = false;
            ++version.current;
            request.current?.abort();
            window.clearInterval(timer);
            window.removeEventListener(MOVEMENT_NOTICES_CHANGED, onChanged);
            window.removeEventListener(MOVEMENT_NOTICES_SAVE_STATE_CHANGED, onSaveStateChanged);
            window.removeEventListener('focus', refresh);
            document.removeEventListener('visibilitychange', onVisible);
        };
    }, [refresh]);

    return { settings, loadError, refresh, saving, saveError };
}
