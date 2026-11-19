'use client';

import { useEffect, useState } from 'react';
import { apiFetch } from '@/i18n/api';

export interface FavoriteModelsSnapshot {
    favorites: string[];
    ready: boolean;
}

/** サーバーから確定できた最新の一覧。null は初回取得がまだ成功していない状態。 */
let serverFavorites: string[] | null = null;
/** 画面に表示している一覧 (楽観更新を含む)。 */
let displayedFavorites: string[] = [];
/** 次にサーバーへ送るべき目標一覧。null は未送信の変更なし。 */
let pendingTarget: string[] | null = null;
/** POST が進行中なら true。 */
let isSaving = false;
/** POST を直列実行している進行中のフラッシュ処理。 */
let activeFlush: Promise<void> | null = null;
/** ローカル書き込みの開始・完了ごとに増やす世代番号。 */
let writeEpoch = 0;
/** 複数 GET の前後逆転を防ぐ通し番号。 */
let nextFetchSeq = 0;
let appliedFetchSeq = 0;

let currentSnapshot: FavoriteModelsSnapshot = { favorites: [], ready: false };
const listeners = new Set<(snapshot: FavoriteModelsSnapshot) => void>();

function publish(nextFavorites: string[]) {
    displayedFavorites = nextFavorites;
    const nextReady = serverFavorites !== null;
    if (currentSnapshot.favorites === nextFavorites && currentSnapshot.ready === nextReady) {
        return;
    }
    currentSnapshot = { favorites: nextFavorites, ready: nextReady };
    listeners.forEach(listener => listener(currentSnapshot));
}

function applyToggle(list: string[], modelId: string): string[] {
    return list.includes(modelId)
        ? list.filter(id => id !== modelId)
        : [...list, modelId];
}

function hasInflightOrPendingWrite(): boolean {
    return isSaving || pendingTarget !== null;
}

function flushWrites(): Promise<void> {
    if (activeFlush) return activeFlush;
    isSaving = true;
    let resolveFlush!: () => void;
    const promise = new Promise<void>(resolve => {
        resolveFlush = resolve;
    });
    activeFlush = promise;

    (async () => {
        try {
            while (pendingTarget !== null) {
                const target = pendingTarget;
                pendingTarget = null;
                try {
                    const res = await apiFetch('/api/config/favorite-models', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ models: target }),
                    });
                    if (!res.ok) {
                        throw new Error(`HTTP ${res.status}`);
                    }
                    serverFavorites = target;
                    writeEpoch++;
                    if (pendingTarget === null) {
                        publish(displayedFavorites);
                    }
                } catch (e) {
                    writeEpoch++;
                    console.error('Failed to save favorite models', e);
                    // 後続の更新が積まれていなければ、サーバー確定済みの値へ戻す
                    if (pendingTarget === null) {
                        publish(serverFavorites ?? []);
                    }
                }
            }
        } finally {
            isSaving = false;
            activeFlush = null;
            resolveFlush();
        }
    })();

    return promise;
}

export async function fetchFavoriteModels(signal?: AbortSignal): Promise<string[] | null> {
    // 書き込みが進行中または待機中のときに GET を送ると、サーバー側の
    // 保存前の一覧を読んでしまい得るため、取得せず現在の表示をそのまま返す。
    if (hasInflightOrPendingWrite()) {
        return displayedFavorites;
    }
    const startEpoch = writeEpoch;
    const seq = ++nextFetchSeq;
    const res = await apiFetch('/api/config/favorite-models', signal ? { signal } : undefined);
    if (!res.ok) return null;
    const data = await res.json();
    // 取得開始後に書き込みが開始・完了した、またはより新しい GET が既に適用済み、
    // あるいは呼び出し側が中断済みの場合は、この取得結果を破棄する。
    if (
        signal?.aborted
        || hasInflightOrPendingWrite()
        || startEpoch !== writeEpoch
        || seq < appliedFetchSeq
    ) {
        return displayedFavorites;
    }
    const rawModels = Array.isArray(data?.models)
        ? data.models
        : Array.isArray(data)
            ? data
            : null;
    if (rawModels === null) return null;
    const list = rawModels.filter((id: unknown): id is string => typeof id === 'string');
    appliedFetchSeq = seq;
    serverFavorites = list;
    publish(list);
    return list;
}

export function saveFavoriteModels(models: string[]): Promise<void> {
    const next = [...models];
    writeEpoch++;
    pendingTarget = next;
    publish(next);
    return flushWrites();
}

/**
 * お気に入りモデルの一覧を購読し、全画面の ModelPicker で共有するフック。
 * どこかの画面で星を切り替えると、開いている他の画面にも即座に反映される。
 * 初回取得が完了するまでは favoritesReady=false となり、既存のお気に入りを
 * 空配列で上書きしないよう切り替え操作を無効化する。
 */
export function useFavoriteModels(enabled = true) {
    const [snapshot, setSnapshot] = useState<FavoriteModelsSnapshot>(() => currentSnapshot);

    useEffect(() => {
        listeners.add(setSnapshot);
        setSnapshot(currentSnapshot);
        return () => {
            listeners.delete(setSnapshot);
        };
    }, []);

    useEffect(() => {
        if (!enabled) return;
        const controller = new AbortController();
        fetchFavoriteModels(controller.signal).catch(e => {
            if (e instanceof DOMException && e.name === 'AbortError') return;
            console.error('Failed to load favorite models', e);
        });
        return () => controller.abort();
    }, [enabled]);

    const toggleFavorite = (modelId: string) => {
        if (serverFavorites === null) return;
        const next = applyToggle(displayedFavorites, modelId);
        void saveFavoriteModels(next);
    };

    return {
        favoriteModels: snapshot.favorites,
        favoritesReady: snapshot.ready,
        toggleFavorite,
    };
}
