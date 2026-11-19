"use client";
import { apiFetch } from '@/i18n/api';

import { useCallback, useEffect, useRef, useState } from 'react';
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
function applyEvent(list: RuntimeAlert[], event: RuntimeAlertEvent): RuntimeAlert[] {
    if (event.action === 'upsert' && event.alert) {
        const incoming = event.alert;
        return [incoming, ...list.filter(a => a.id !== incoming.id)];
    }
    if (event.action === 'dismiss') {
        return list.filter(a => a.id !== event.id);
    }
    return list;
}

export function useRuntimeAlerts(backendConnected: boolean) {
    const [alerts, setAlerts] = useState<RuntimeAlert[]>([]);
    // 一覧の取り直しの最中に届いた知らせ (出た・閉じた) を、届いた順に覚えておく。
    // 取り直した一覧は取り直しを始めた時点の内容なので、そのまま置き換えると、
    // その間に SSE で届いた新しい知らせが消え、閉じた知らせが戻る。取り直しの
    // 結果に、始めた後に届いた分を重ね直してから表示する。
    const eventSeqRef = useRef(0);
    const recentEventsRef = useRef<{ seq: number; event: RuntimeAlertEvent }[]>([]);

    const remember = useCallback((event: RuntimeAlertEvent) => {
        eventSeqRef.current += 1;
        recentEventsRef.current.push({ seq: eventSeqRef.current, event });
        if (recentEventsRef.current.length > 200) {
            recentEventsRef.current = recentEventsRef.current.slice(-200);
        }
    }, []);

    const refetch = useCallback(async () => {
        const startedAt = eventSeqRef.current;
        try {
            const res = await apiFetch('/api/system/runtime-alerts');
            if (!res.ok) return;
            const data = await res.json();
            const fetched: RuntimeAlert[] = Array.isArray(data?.alerts) ? data.alerts : [];
            const later = recentEventsRef.current.filter(e => e.seq > startedAt);
            setAlerts(later.reduce((list, e) => applyEvent(list, e.event), fetched));
        } catch (error) {
            // 裏側がまだ起動していないことがある。繋がり直したときに取り直す。
            console.warn('[runtime-alerts] failed to fetch the list', error);
        }
    }, []);

    useEffect(() => {
        if (backendConnected) refetch();
    }, [backendConnected, refetch]);

    const handleEvent = useCallback((event: RuntimeAlertEvent) => {
        remember(event);
        setAlerts(prev => applyEvent(prev, event));
    }, [remember]);

    const dismiss = useCallback(async (id: number) => {
        // 自分で閉じた分も「閉じた」として覚える (取り直しの結果に戻らないように)。
        const closed: RuntimeAlertEvent = { type: 'runtime_alert', action: 'dismiss', id };
        remember(closed);
        setAlerts(prev => applyEvent(prev, closed));
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
    }, [refetch, remember]);

    return { alerts, handleEvent, refetch, dismiss };
}
