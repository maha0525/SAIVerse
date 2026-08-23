// 手帳 (アクティビティ + メモ) とタスク帳の読み口。
// 正典: docs/intent/autonomous_behavior_v3.md §13「手帳とタスク帳の読み口 (v0.3)」。
//
// 目的は「見えないと直せない」— 手帳とタスク帳はペルソナが自分で書く自己像の
// 一部で、スルースのたびに本人が読み返す。ユーザーから見えないと、間違った
// 約束や的外れな「やりたいこと」を抱え込んでも気づけない。
//
// v0.3 は読むだけ。編集・削除ボタンは置かない (訂正の口は v0.4 で、手帳の器の
// 作り直しとタスク帳の CAS との整合を決めてから)。
import React, { useState, useEffect } from 'react';
import { Notebook, Handshake, CalendarClock, User } from 'lucide-react';
import styles from './PocketbookViewer.module.css';

interface PocketbookMemo {
    id: number;
    date: string;                       // 'YYYY-MM-DD'
    kind: 'did' | 'want' | string;
    text: string;
    span_start_id: string | null;
    span_end_id: string | null;
}

interface PocketbookActivity {
    id: number;
    name: string;
    status: 'open' | 'closed' | string;
    origin: string;
    born_at: number;
    closed_at: number | null;
    last_memo_date: string | null;
    memos: PocketbookMemo[];
}

interface TaskBookItem {
    task_id: string;
    persona_id: string;
    content: string;
    due_at: number | null;
    counterpart: string | null;
    origin: string;
    status: string;
}

interface PocketbookViewerProps {
    personaId: string;
}

const MEMO_KIND_LABEL: Record<string, string> = {
    want: 'やりたい',
    did: 'やった',
};

// アクティビティの出自 (activities.origin の閉語彙)。
const ORIGIN_LABEL: Record<string, string> = {
    sluice: 'ペルソナが書いた',
    user: 'ユーザーが立てた',
    initial: 'キャラクター作成時',
    migration: '以前のデータからの引き継ぎ',
};

const TASK_STATUS_LABEL: Record<string, string> = {
    open: 'まだ終わっていない',
    done: 'やり終えた',
    withdrawn: '取り下げた',
};

// 相手 (counterpart) は 'user' / ペルソナ ID / 'system' 等。既知の値だけ訳す。
const COUNTERPART_LABEL: Record<string, string> = {
    user: 'ユーザー',
    system: 'システム',
};

function epochToDate(epoch: number | null): string | null {
    if (!epoch) return null;
    const d = new Date(epoch * 1000);
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
}

export default function PocketbookViewer({ personaId }: PocketbookViewerProps) {
    const [activities, setActivities] = useState<PocketbookActivity[]>([]);
    const [tasks, setTasks] = useState<TaskBookItem[]>([]);
    const [includeClosed, setIncludeClosed] = useState(false);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        let cancelled = false;
        const load = async () => {
            setIsLoading(true);
            setError(null);
            try {
                const [pbRes, tbRes] = await Promise.all([
                    fetch(
                        `/api/people/${personaId}/pocketbook?include_closed=${includeClosed}`
                    ),
                    fetch(`/api/people/${personaId}/task-book`),
                ]);
                if (!pbRes.ok) throw new Error(`HTTP ${pbRes.status}`);
                if (!tbRes.ok) throw new Error(`HTTP ${tbRes.status}`);
                const pbData = await pbRes.json();
                const tbData = await tbRes.json();
                if (cancelled) return;
                setActivities(pbData.activities || []);
                setTasks(tbData.tasks || []);
            } catch (e) {
                if (!cancelled) setError(`手帳を読み込めませんでした (${e})`);
            } finally {
                if (!cancelled) setIsLoading(false);
            }
        };
        load();
        return () => { cancelled = true; };
    }, [personaId, includeClosed]);

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <Notebook size={16} />
                <span>
                    ペルソナが自分で書いている手帳です。メモ欄と約束の欄があり、ここでは読むだけで、書き換えはできません。
                </span>
            </div>

            {isLoading && <div className={styles.notice}>読み込み中...</div>}
            {error && <div className={styles.error}>{error}</div>}

            {!isLoading && !error && (
                <>
                    <section className={styles.section}>
                        <div className={styles.sectionHeader}>
                            <h4 className={styles.sectionTitle}>
                                <Notebook size={14} />
                                メモ欄 (やりたい・やった)
                            </h4>
                            <label className={styles.toggle}>
                                <input
                                    type="checkbox"
                                    checked={includeClosed}
                                    onChange={(e) => setIncludeClosed(e.target.checked)}
                                />
                                閉じたものも見る
                            </label>
                        </div>

                        {activities.length === 0 ? (
                            <div className={styles.emptyNote}>
                                まだ手帳に何も書かれていません。
                            </div>
                        ) : (
                            <ul className={styles.activityList}>
                                {activities.map((a) => (
                                    <li
                                        key={a.id}
                                        className={`${styles.activityCard} ${a.status === 'closed' ? styles.activityClosed : ''}`}
                                    >
                                        <div className={styles.activityHead}>
                                            <span className={styles.activityName}>{a.name}</span>
                                            <span className={styles.badge}>
                                                {a.status === 'closed' ? '閉じた' : '開いている'}
                                            </span>
                                            <span className={styles.badgeMuted}>
                                                {ORIGIN_LABEL[a.origin] || a.origin}
                                            </span>
                                        </div>
                                        {a.memos.length === 0 ? (
                                            <div className={styles.emptyNote}>
                                                このアクティビティにはまだメモがありません。
                                            </div>
                                        ) : (
                                            <ul className={styles.memoList}>
                                                {a.memos.map((m) => (
                                                    <li key={m.id} className={styles.memoItem}>
                                                        <span className={styles.memoDate}>{m.date}</span>
                                                        <span
                                                            className={`${styles.memoKind} ${m.kind === 'want' ? styles.memoKindWant : styles.memoKindDid}`}
                                                        >
                                                            {MEMO_KIND_LABEL[m.kind] || m.kind}
                                                        </span>
                                                        <span className={styles.memoText}>{m.text}</span>
                                                    </li>
                                                ))}
                                            </ul>
                                        )}
                                    </li>
                                ))}
                            </ul>
                        )}
                    </section>

                    <section className={styles.section}>
                        <h4 className={styles.sectionTitle}>
                            <Handshake size={14} />
                            約束の欄
                        </h4>
                        {tasks.length === 0 ? (
                            <div className={styles.emptyNote}>開いている約束はありません。</div>
                        ) : (
                            <ul className={styles.taskList}>
                                {tasks.map((t) => (
                                    <li key={t.task_id} className={styles.taskItem}>
                                        <span className={styles.taskContent}>{t.content}</span>
                                        <div className={styles.taskMeta}>
                                            <span className={styles.taskMetaItem}>
                                                <CalendarClock size={12} />
                                                {epochToDate(t.due_at) || '期限なし'}
                                            </span>
                                            {t.counterpart && (
                                                <span className={styles.taskMetaItem}>
                                                    <User size={12} />
                                                    {COUNTERPART_LABEL[t.counterpart] || t.counterpart}
                                                </span>
                                            )}
                                            <span className={styles.badgeMuted}>
                                                {TASK_STATUS_LABEL[t.status] || t.status}
                                            </span>
                                        </div>
                                    </li>
                                ))}
                            </ul>
                        )}
                    </section>
                </>
            )}
        </div>
    );
}
