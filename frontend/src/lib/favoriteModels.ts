'use client';

import { useEffect, useState } from 'react';
import { apiFetch } from '@/i18n/api';

let cachedFavorites: string[] = [];
let cacheVersion = 0;
const listeners = new Set<(favorites: string[]) => void>();

function notifyListeners(next: string[]) {
    cachedFavorites = next;
    listeners.forEach(listener => listener(next));
}

export async function fetchFavoriteModels(signal?: AbortSignal): Promise<string[] | null> {
    const startVersion = cacheVersion;
    const res = await apiFetch('/api/config/favorite-models', signal ? { signal } : undefined);
    if (!res.ok) return null;
    const data = await res.json();
    if (startVersion !== cacheVersion) return cachedFavorites;
    const list = Array.isArray(data?.models)
        ? data.models.filter((id: unknown): id is string => typeof id === 'string')
        : [];
    notifyListeners(list);
    return list;
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
        const current = cachedFavorites;
        const next = current.includes(modelId)
            ? current.filter(id => id !== modelId)
            : [...current, modelId];
        void saveFavoriteModels(next);
    };

    return { favoriteModels, toggleFavorite };
}
