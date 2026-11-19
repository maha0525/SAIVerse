"use client";
import { apiFetch } from '@/i18n/api';

import { useCallback, useEffect, useState } from 'react';
import type { RuntimeAlert, RuntimeAlertEvent } from './useAddonEvents';

/**
 * 動作中に見つけた、部屋に結びつかない警告の一覧を持つフック
 * (docs/intent/runtime_alerts.md)。画面の右下に、ユーザーが閉じるまで残す。
 *
 * - 起きた瞬間の知らせは常設 SSE (`useAddonEvents` の `onRuntimeAlert`) で届くので、
 *   `handleEvent` に渡す。同じ知らせ (同じ id) は書き換える。
 * - SSE は繋がっていない間の知らせを保持しないので、開いたとき・裏側と繋がり直した
 *   とき (`backendConnected`)・SSE が繋ぎ直したとき (`refetch` を `onOpen` に渡す) に
 *   `GET /api/system/runtime-alerts` で一覧を取り直す。
 * - 閉じると `POST /api/system/runtime-alerts/{id}/dismiss`。サーバーが SSE で
 *   「閉じた」を送るので、ほかのタブからも消える。
 *
 * 一覧の `title` / `message` は `apiFetch` / `parseUIEvent` が表示用の文に変えた
 * もの (取り出すたびに今の表示言語で読む)。オブジェクトを展開して複製しない。
 */
export function useRuntimeAlerts(backendConnected: boolean) {
    const [alerts, setAlerts] = useState<RuntimeAlert[]>([]);

    const refetch = useCallback(async () => {
        try {
            const res = await apiFetch('/api/system/runtime-alerts');
            if (!res.ok) return;
            const data = await res.json();
            setAlerts(Array.isArray(data?.alerts) ? data.alerts : []);
        } catch (error) {
            // 裏側がまだ起動していないことがある。繋がり直したときに取り直す。
            console.warn('[runtime-alerts] failed to fetch the list', error);
        }
    }, []);

    useEffect(() => {
        if (backendConnected) refetch();
    }, [backendConnected, refetch]);

    const handleEvent = useCallback((event: RuntimeAlertEvent) => {
        if (event.action === 'upsert' && event.alert) {
            const incoming = event.alert;
            setAlerts(prev => [incoming, ...prev.filter(a => a.id !== incoming.id)]);
        } else if (event.action === 'dismiss') {
            setAlerts(prev => prev.filter(a => a.id !== event.id));
        }
    }, []);

    const dismiss = useCallback(async (id: number) => {
        setAlerts(prev => prev.filter(a => a.id !== id));
        try {
            const res = await apiFetch(`/api/system/runtime-alerts/${id}/dismiss`, { method: 'POST' });
            // 404 は既に無い知らせ。それ以外の失敗は、閉じられていないので一覧を取り直して戻す。
            if (!res.ok && res.status !== 404) {
                console.warn('[runtime-alerts] dismiss failed', res.status);
                refetch();
            }
        } catch (error) {
            console.warn('[runtime-alerts] dismiss failed', error);
            refetch();
        }
    }, [refetch]);

    return { alerts, handleEvent, refetch, dismiss };
}
