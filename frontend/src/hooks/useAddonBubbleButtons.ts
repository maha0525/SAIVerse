"use client";
import { apiFetch } from '@/i18n/api';

import { useEffect, useState } from 'react';
import type { BubbleButtonDef } from '@/components/AddonBubbleButtons';
import type { AddonInfo } from '@/types/addon';

/**
 * 有効なアドオンの吹き出しボタン定義を集めるフック。
 *
 * /api/addon/ を 1 回だけ取得し、有効なアドオンの bubble_buttons のうち
 * ``surface`` の画面に出すものだけを返す。``show_in`` が未指定 (バックエンドは
 * null で配る) のボタンは ["chat"] 扱い — 既存アドオンのボタンが記憶の画面へ
 * 無差別に出ないように。
 */
export function useAddonBubbleButtons(surface: 'chat' | 'memory'): BubbleButtonDef[] {
    const [buttons, setButtons] = useState<BubbleButtonDef[]>([]);

    useEffect(() => {
        let cancelled = false;
        apiFetch('/api/addon/')
            .then((r) => r.ok ? r.json() : [])
            .then((addons: Array<Pick<AddonInfo, 'addon_name' | 'is_enabled'> & Partial<Pick<AddonInfo, 'ui_extensions'>>>) => {
                const collected: BubbleButtonDef[] = [];
                for (const addon of addons) {
                    if (!addon.is_enabled) continue;
                    for (const btn of addon.ui_extensions?.bubble_buttons ?? []) {
                        if (!(btn.show_in ?? ['chat']).includes(surface)) continue;
                        collected.push({ ...btn, addon_name: addon.addon_name });
                    }
                }
                if (!cancelled) setButtons(collected);
            })
            .catch(() => {/* addon APIが無い環境では無視 */});
        return () => { cancelled = true; };
    }, [surface]);

    return buttons;
}
