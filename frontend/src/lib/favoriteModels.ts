'use client';

import { useEffect, useState } from 'react';
import { apiFetch } from '@/i18n/api';

let cachedFavorites: string[] = [];
let cacheVersion = 0;
let hasLoaded = false;
let inflightFetch: Promise<string[] | null> | null = null;
let inflightFetchId = 0;
let pendingToggles: string[] = [];
const listeners = new Set<(favorites: string[]) => void>();

function notifyListeners(next: string[]) {
    cachedFavorites = next;
    listeners.forEach(listener => listener(next));
}

function applyToggle(list: string[], modelId: string): string[] {
    return list.includes(modelId)
        ? list.filter(id => id !== modelId)
        : [...list, modelId];
}

export async function fetchFavoriteModels(signal?: AbortSignal): Promise<string[] | null> {
    const startVersion = cacheVersion;
    const fetchId = ++inflightFetchId;
    const promise = (async () => {
        try {
            const res = await apiFetch('/api/config/favorite-models', signal ? { signal } : undefined);
            if (!res.ok) {
                if (!hasLoaded) pendingToggles = [];
                return null;
            }
            const data = await res.json();
            if (startVersion !== cacheVersion) {
                pendingToggles = [];
                return cachedFavorites;
            }
            const list = Array.isArray(data?.models)
                ? data.models.filter((id: unknown): id is string => typeof id === 'string')
                : [];
            hasLoaded = true;
            if (pendingToggles.length > 0) {
                const queued = pendingToggles;
                pendingToggles = [];
                const merged = queued.reduce(applyToggle, list);
                await saveFavoriteModels(merged);
                return merged;
            }
            notifyListeners(list);
            return list;
        } catch (e) {
            if (!hasLoaded) pendingToggles = [];
            throw e;
        } finally {
            if (inflightFetchId === fetchId) {
                inflightFetch = null;
            }
        }
    })();
    inflightFetch = promise;
    return promise;
}

export async function saveFavoriteModels(models: string[]): Promise<void> {
    const writeVersion = ++cacheVersion;
    const previous = cachedFavorites;
    notifyListeners(models);
    try {
        const res = await apiFetch('/api/config/favorite-models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ models }),
        });
        if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
        }
    } catch (e) {
        if (cacheVersion === writeVersion) {
            notifyListeners(previous);
        }
        console.error('Failed to save favorite models', e);
    }
}

/**
 * お気に入りモデルの一覧を購読し、全画面の ModelPicker で共有するフック。
 * どこかの画面で星を切り替えると、開いている他の画面にも即座に反映される。
 */
export function useFavoriteModels(enabled = true) {
    const [favoriteModels, setFavoriteModels] = useState<string[]>(cachedFavorites);

    useEffect(() => {
        listeners.add(setFavoriteModels);
        setFavoriteModels(cachedFavorites);
        return () => {
            listeners.delete(setFavoriteModels);
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
        // 初回取得が完了する前に星が押された場合、空のキャッシュで POST すると
        // 既存のお気に入りが消えるため、取得完了後にサーバーの一覧へマージして保存する。
        if (!hasLoaded) {
            pendingToggles = applyToggle(pendingToggles, modelId);
            if (!inflightFetch) {
                fetchFavoriteModels().catch(e => {
                    console.error('Failed to load favorite models', e);
                });
            }
            return;
        }
        const next = applyToggle(cachedFavorites, modelId);
        void saveFavoriteModels(next);
    };

    return { favoriteModels, toggleFavorite };
}
