"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from "react";
import { Radio } from "lucide-react";

import styles from "./ActiveClientIndicator.module.css";

/**
 * 「このタブがアクティブクライアントタブ」を示す視覚インジケータ。
 *
 * アクティブ時のみ Radio アイコンを表示（非アクティブ時は何も出さない）。
 * クリックは不可、表示のみ。アクティブ判定は `useActiveClientTab` による
 * 最終操作時刻 + BroadcastChannel 排他に基づく。
 *
 * 将来他アドオンの client_actions でも同じ「アクティブ状態」を参照する
 * 前提のため、アイコン・文言は音声依存にしていない。
 */
export function ActiveClientIndicator({ isActive }: { isActive: boolean }) {
    useLocale();
    if (!isActive) return null;
    return (
        <span data-i18n="components.ActiveClientIndicator.text001 components.ActiveClientIndicator.text002"
            className={styles.indicator}
            title={uiText("components.ActiveClientIndicator.text001")}
            aria-label={uiText("components.ActiveClientIndicator.text002")}
        >
            <Radio size={18} />
        </span>
    );
}
