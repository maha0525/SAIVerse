"use client";

/**
 * できごと (画面 A) の直リンク用ページ。
 *
 * タイムライン本体は components/EventsTimeline.tsx に切り出されており、
 * 通常の導線 (Sidebar) からは EventsModal としてチャット画面の上に開く
 * (ページ遷移すると書きかけメッセージが消えるため)。このページは
 * /events を直接開いた時のための薄いラッパーとして残している。
 *
 * 設計正典: docs/intent/persona_cognition/life_concept_map.md §8.1
 */

import { ArrowLeft, Newspaper } from 'lucide-react';
import styles from './page.module.css';
import EventsTimeline from '@/components/EventsTimeline';

export default function EventsPage() {
    return (
        <div className={styles.container}>
            <div className={styles.inner}>
                <div className={styles.header}>
                    <button
                        className={styles.backButton}
                        onClick={() => { window.location.href = '/'; }}
                    >
                        <ArrowLeft size={16} />
                        戻る
                    </button>
                    <h1 className={styles.title}><Newspaper size={20} /> できごと</h1>
                    <div style={{ width: '80px' }} />
                </div>

                <EventsTimeline />
            </div>
        </div>
    );
}
