'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useEffect, useRef, useState } from 'react';
import styles from './TermHint.module.css';

/**
 * 機構名 (Track / Task) の横に置く小さな (?) アイコン + 説明ツールチップ。
 *
 * - デスクトップ: ホバーで表示 / マウスが離れると閉じる
 * - タッチ: タップでトグル、外側タップで閉じる
 * - 文言はこのファイルの TERM_HINTS に集約 (将来ドキュメントへの
 *   リンクに差し替える時もここ 1 箇所を変えれば済む)
 */

export const TERM_HINTS = {
    get track() { return uiText("components.common.TermHint.text001"); },
    get task() { return uiText("components.common.TermHint.text002"); },
} as const;

export type TermHintKey = keyof typeof TERM_HINTS;

const TERM_LABELS: Record<TermHintKey, string> = {
    track: 'Track',
    task: 'Task',
};

interface TermHintProps {
    term: TermHintKey;
}

export default function TermHint({ term }: TermHintProps) {
    useLocale();
    const [open, setOpen] = useState(false);
    const rootRef = useRef<HTMLSpanElement>(null);

    // タッチ時: 開いている間だけ「外側タップで閉じる」リスナーを張る
    useEffect(() => {
        if (!open) return;
        const handleOutside = (e: MouseEvent | TouchEvent) => {
            if (rootRef.current && e.target instanceof Node && !rootRef.current.contains(e.target)) {
                setOpen(false);
            }
        };
        document.addEventListener('mousedown', handleOutside);
        document.addEventListener('touchstart', handleOutside);
        return () => {
            document.removeEventListener('mousedown', handleOutside);
            document.removeEventListener('touchstart', handleOutside);
        };
    }, [open]);

    return (
        <span
            ref={rootRef}
            className={styles.root}
            // pointerType で判定し、タッチ由来の合成 mouseenter で
            // 「開いた直後に click のトグルで閉じる」事故を避ける
            onPointerEnter={e => { if (e.pointerType === 'mouse') setOpen(true); }}
            onPointerLeave={e => { if (e.pointerType === 'mouse') setOpen(false); }}
        >
            <button data-i18n="components.common.TermHint.text003"
                type="button"
                className={styles.icon}
                aria-label={uiText("components.common.TermHint.text003", { p1: TERM_LABELS[term] })}
                aria-expanded={open}
                onClick={e => { e.stopPropagation(); setOpen(v => !v); }}
            >
                ?
            </button>
            {open && (
                <span className={styles.tooltip} role="tooltip">
                    {TERM_HINTS[term]}
                </span>
            )}
        </span>
    );
}
