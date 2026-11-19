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
 * 取り直しは一度に一つだけ走らせる。走っている最中に、別の取り直しの要求・SSE の
 * 知らせ・自分で閉じる操作のどれかが来たら、その取り直しの結果は古い可能性がある
 * ので採らず、終わった後でもう一度取り直す。画面に出す一覧は、いつも「最中に何も
 * 起きなかった取り直し」の結果なので、サーバーの閉じていない知らせと一致する。
 * もう一度取り直す前には少し待つ (0.3 秒から倍々、最大 5 秒) — 知らせが届き続けても
 * 取り直しを休みなく繰り返さない。
 * 取り直しが失敗したら、成功するまで間隔を空けてやり直す (2 秒から倍々、最大 30 秒)。
 *
 * 一覧の `title` / `message` は `apiFetch` / `parseUIEvent` が表示用の文に変えた
 * もの (取り出すたびに今の表示言語で読む)。オブジェクトを展開して複製しない。
 */
export function useRuntimeAlerts(backendConnected: boolean) {
    const [alerts, setAlerts] = useState<RuntimeAlert[]>([]);
    const mountedRef = useRef(true);
    const inFlightRef = useRef(false);
    // 走っている取り直しの最中に何かが起きた (結果を採らずにもう一度取り直す)。
    const staleRef = useRef(false);
    // 閉じた知らせの番号。閉じた後の再発はサーバーで新しい番号の行になるので、
    // 閉じた番号に後から届いた「更新」は古い知らせとして捨てる。
    const dismissedIdsRef = useRef<Set<number>>(new Set());
    const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const retryDelayRef = useRef(2000);

    const clearRetry = () => {
        if (retryTimerRef.current) {
            clearTimeout(retryTimerRef.current);
            retryTimerRef.current = null;
        }
    };

    const refetch = useCallback(async (): Promise<void> => {
        if (inFlightRef.current) {
            staleRef.current = true;
            return;
        }
        inFlightRef.current = true;
        clearRetry();
        let staleWait = 300;
        try {
            for (;;) {
                staleRef.current = false;
                let fetched: RuntimeAlert[];
                try {
                    const res = await apiFetch('/api/system/runtime-alerts');
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    fetched = Array.isArray(data?.alerts) ? data.alerts : [];
                } catch (error) {
                    // 裏側がまだ起動していないことがある。間隔を空けてやり直す。
                    console.warn('[runtime-alerts] failed to fetch the list', error);
                    if (mountedRef.current && !retryTimerRef.current) {
                        const delay = retryDelayRef.current;
                        retryDelayRef.current = Math.min(delay * 2, 30000);
                        retryTimerRef.current = setTimeout(() => {
                            retryTimerRef.current = null;
                            void refetch();
                        }, delay);
                    }
                    return;
                }
                if (!mountedRef.current) return;
                if (staleRef.current) {
                    await new Promise(resolve => setTimeout(resolve, staleWait));
                    staleWait = Math.min(staleWait * 2, 5000);
                    if (!mountedRef.current) return;
                    continue;
                }
                retryDelayRef.current = 2000;
                const dismissedIds = dismissedIdsRef.current;
                setAlerts(fetched.filter(a => !dismissedIds.has(a.id)));
                return;
            }
        } finally {
            inFlightRef.current = false;
        }
    }, []);

    useEffect(() => {
        mountedRef.current = true;
        return () => {
            mountedRef.current = false;
            clearRetry();
        };
    }, []);

    useEffect(() => {
        if (backendConnected) void refetch();
    }, [backendConnected, refetch]);

    const handleEvent = useCallback((event: RuntimeAlertEvent) => {
        if (!mountedRef.current) return;
        if (inFlightRef.current) staleRef.current = true;
        if (event.action === 'upsert' && event.alert) {
            const incoming = event.alert;
            setAlerts(prev => {
                const rest = prev.filter(a => a.id !== incoming.id);
                return dismissedIdsRef.current.has(incoming.id) ? rest : [incoming, ...rest];
            });
        } else if (event.action === 'dismiss') {
            dismissedIdsRef.current.add(event.id);
            setAlerts(prev => prev.filter(a => a.id !== event.id));
        }
    }, []);

    const dismiss = useCallback(async (id: number) => {
        if (inFlightRef.current) staleRef.current = true;
        dismissedIdsRef.current.add(id);
        setAlerts(prev => prev.filter(a => a.id !== id));
        let failed = false;
        try {
            const res = await apiFetch(`/api/system/runtime-alerts/${id}/dismiss`, { method: 'POST' });
            // 404 は既に無い知らせ。それ以外の失敗は、閉じられていない。
            if (!res.ok && res.status !== 404) {
                console.warn('[runtime-alerts] dismiss failed', res.status);
                failed = true;
            }
        } catch (error) {
            console.warn('[runtime-alerts] dismiss failed', error);
            failed = true;
        }
        if (failed) {
            // 閉じられていないので「閉じた」の記録を外し、一覧を取り直して戻す。
            dismissedIdsRef.current.delete(id);
            if (mountedRef.current) void refetch();
        }
    }, [refetch]);

    return { alerts, handleEvent, refetch, dismiss };
}
