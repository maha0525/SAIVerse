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
function applyEvent(
    list: RuntimeAlert[],
    event: RuntimeAlertEvent,
    dismissedIds: Set<number>,
): RuntimeAlert[] {
    if (event.action === 'upsert' && event.alert) {
        const incoming = event.alert;
        // 閉じた知らせの番号に後から届いた「更新」は捨てる。閉じた後の再発は
        // サーバーで新しい番号の行になるので、同じ番号の更新は古い知らせ。
        if (dismissedIds.has(incoming.id)) return list.filter(a => a.id !== incoming.id);
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
    const dismissedIdsRef = useRef<Set<number>>(new Set());
    // 走っている取り直しの開始時点。覚えた知らせが要るのは、どれかの取り直しが
    // 走っている間だけ — 一つも走っていなければ捨ててよい (件数の上限では切らない。
    // 上限で切ると、取り直しの間に届いた分が落ちて画面から消える)。
    // 取り直しごとに別の番号を振る (同じ瞬間に始まった二つを一つに潰さない)。
    const inflightStartsRef = useRef<Map<number, number>>(new Map());
    const refetchIdRef = useRef(0);
    // 画面に出した一覧のうち、いちばん新しく始まった取り直しの番号。これより前に
    // 始まった取り直しの結果が遅れて届いても、新しい一覧を古い一覧で上書きしない。
    const committedRefetchIdRef = useRef(0);
    const recentEventsRef = useRef<{ seq: number; event: RuntimeAlertEvent }[]>([]);

    const remember = useCallback((event: RuntimeAlertEvent) => {
        if (event.action === 'dismiss') dismissedIdsRef.current.add(event.id);
        eventSeqRef.current += 1;
        if (inflightStartsRef.current.size === 0) {
            recentEventsRef.current = [];
            return;
        }
        recentEventsRef.current.push({ seq: eventSeqRef.current, event });
    }, []);

    const refetch = useCallback(async () => {
        const startedAt = eventSeqRef.current;
        refetchIdRef.current += 1;
        const refetchId = refetchIdRef.current;
        inflightStartsRef.current.set(refetchId, startedAt);
        try {
            const res = await apiFetch('/api/system/runtime-alerts');
            if (!res.ok) return;
            const data = await res.json();
            const fetched: RuntimeAlert[] = Array.isArray(data?.alerts) ? data.alerts : [];
            if (refetchId < committedRefetchIdRef.current) return;
            committedRefetchIdRef.current = refetchId;
            const later = recentEventsRef.current.filter(e => e.seq > startedAt);
            const dismissedIds = dismissedIdsRef.current;
            setAlerts(later.reduce(
                (list, e) => applyEvent(list, e.event, dismissedIds),
                fetched.filter(a => !dismissedIds.has(a.id)),
            ));
        } catch (error) {
            // 裏側がまだ起動していないことがある。繋がり直したときに取り直す。
            console.warn('[runtime-alerts] failed to fetch the list', error);
        } finally {
            inflightStartsRef.current.delete(refetchId);
            if (inflightStartsRef.current.size === 0) {
                recentEventsRef.current = [];
            } else {
                const oldest = Math.min(...Array.from(inflightStartsRef.current.values()));
                recentEventsRef.current = recentEventsRef.current.filter(e => e.seq > oldest);
            }
        }
    }, []);

    useEffect(() => {
        if (backendConnected) refetch();
    }, [backendConnected, refetch]);

    const handleEvent = useCallback((event: RuntimeAlertEvent) => {
        remember(event);
        setAlerts(prev => applyEvent(prev, event, dismissedIdsRef.current));
    }, [remember]);

    const dismiss = useCallback(async (id: number) => {
        // 自分で閉じた分も「閉じた」として覚える (取り直しの結果に戻らないように)。
        const closed: RuntimeAlertEvent = { type: 'runtime_alert', action: 'dismiss', id };
        remember(closed);
        setAlerts(prev => applyEvent(prev, closed, dismissedIdsRef.current));
        try {
            const res = await apiFetch(`/api/system/runtime-alerts/${id}/dismiss`, { method: 'POST' });
            // 404 は既に無い知らせ。それ以外の失敗は、閉じられていないので一覧を取り直して戻す。
            if (!res.ok && res.status !== 404) {
                console.warn('[runtime-alerts] dismiss failed', res.status);
                // 閉じられていないので「閉じた」の記録を外し、一覧から戻す。
                // 走っている取り直しが重ね直さないよう、覚えた分からも消す。
                dismissedIdsRef.current.delete(id);
                recentEventsRef.current = recentEventsRef.current.filter(e => e.event !== closed);
                refetch();
            }
        } catch (error) {
            console.warn('[runtime-alerts] dismiss failed', error);
            dismissedIdsRef.current.delete(id);
            recentEventsRef.current = recentEventsRef.current.filter(e => e.event !== closed);
            refetch();
        }
    }, [refetch, remember]);

    return { alerts, handleEvent, refetch, dismiss };
}
