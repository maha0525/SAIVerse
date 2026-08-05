'use client';

/**
 * v0.2.x 以前の Memopedia を v0.3.x 用へ変換する（本文 → Fragment）。
 *
 * 設計: docs/intent/memopedia_body_to_fragment.md
 *
 * 判定は三段。①編集来歴が「抽出器が足した」と裏づけた行は自動で Fragment、
 * ②記法だけが根拠の行は保留、③保留行は 1 行ずつユーザーが決める。
 * 判断を渡さなかった保留行は本文に残る（機械が代わりに決めない）。
 */
import { useEffect, useRef, useState } from 'react';
import {
    AlertCircle, AlertTriangle, Check, FileText, Loader2, RotateCcw,
} from 'lucide-react';
import styles from './MemopediaConversion.module.css';

type Choice = 'fragment' | 'body';

interface PendingLine {
    line_no: number;
    content: string;
    /** pending=判断が要る / fragment=記録あり / body=そのまま本文に残る（文脈） */
    role: 'fragment' | 'pending' | 'body';
}

interface PendingBlock {
    date: string | null;
    lines: PendingLine[];
    has_pending: boolean;
}

interface PendingPage {
    page_id: string;
    title: string;
    category: string | null;
    blocks: PendingBlock[];
}

interface Mark {
    kind: string;
    page_title: string;
    line_no: number;
    text: string;
    note: string;
}

interface VerbatimBreach {
    page_id: string;
    title: string;
    detail: string;
}

interface Preview {
    fingerprint: string;
    decided_count: number;
    verbatim_breaches: VerbatimBreach[];
    page_count: number;
    fragment_count: number;
    confirmed_count: number;
    pending_count: number;
    emptied_count: number;
    kept_body_count: number;
    is_safe: boolean;
    conservation: {
        before_lines: number;
        after_lines: number;
        lost_count: number;
        gained_count: number;
        lost_samples: string[];
        gained_samples: string[];
    };
    marks: Mark[];
    pending_pages: PendingPage[];
}

interface Run {
    run_id: string;
    converted_at: number;
    page_count: number;
    fragment_count: number;
}

export default function MemopediaConversion({ personaId }: { personaId: string }) {
    const [preview, setPreview] = useState<Preview | null>(null);
    const [decisions, setDecisions] = useState<Record<string, Record<number, Choice>>>({});
    const [runs, setRuns] = useState<Run[]>([]);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<string | null>(null);
    const [confirming, setConfirming] = useState(false);
    const [forceRun, setForceRun] = useState<string | null>(null);

    const [restating, setRestating] = useState(false);
    const base = `/api/people/${personaId}/debug/memopedia-conversion`;
    const decisionsRef = useRef(decisions);
    decisionsRef.current = decisions;

    const call = async (path: string, init?: RequestInit) => {
        const res = await fetch(`${base}${path}`, init);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
        return data;
    };

    const loadRuns = async () => {
        try {
            const data = await call('/runs');
            setRuns(data.runs || []);
        } catch {
            /* 一覧が取れなくても下見・実行はできる */
        }
    };

    const handlePreview = async () => {
        setBusy('preview');
        setError(null);
        setResult(null);
        setConfirming(false);
        try {
            const data = await call('/preview');
            setPreview(data);
            setDecisions({});
            await loadRuns();
        } catch (e) {
            setError(e instanceof Error ? e.message : '下見に失敗しました');
        } finally {
            setBusy(null);
        }
    };

    const handleApply = async () => {
        setBusy('apply');
        setError(null);
        try {
            const data = await call('/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ decisions, fingerprint: preview?.fingerprint }),
            });
            setResult(data.message || '変換しました');
            setPreview(null);
            setDecisions({});
            setConfirming(false);
            await loadRuns();
        } catch (e) {
            setError(e instanceof Error ? e.message : '変換に失敗しました');
        } finally {
            setBusy(null);
        }
    };

    const handleRevert = async (runId: string, force = false) => {
        setBusy(`revert:${runId}`);
        setError(null);
        setForceRun(null);
        try {
            const data = await call('/revert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ run_id: runId, force }),
            });
            setResult(data.message || '取り消しました');
            setPreview(null);
            await loadRuns();
        } catch (e) {
            // 変換より後の編集があると既定で拒否される。何を失うかを見せてから選ばせる。
            setError(e instanceof Error ? e.message : '取り消しに失敗しました');
            if (!force) setForceRun(runId);
        } finally {
            setBusy(null);
        }
    };

    // 保留行を選ぶと、変換されるページ数や本文が空になるページ数も変わる。
    // 最初の下見の数字を出したままだと、自分の選択で何が起きるか見えない。
    useEffect(() => {
        if (!preview) return;
        if (Object.keys(decisions).length === 0) return;
        const timer = setTimeout(async () => {
            setRestating(true);
            try {
                const data = await call('/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decisions: decisionsRef.current }),
                });
                setPreview(data);
            } catch {
                /* 数字の更新に失敗しても、選択と実行は続けられる */
            } finally {
                setRestating(false);
            }
        }, 600);
        return () => clearTimeout(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [decisions]);

    const setChoice = (pageId: string, lineNo: number, choice: Choice) => {
        setDecisions((prev) => ({
            ...prev,
            [pageId]: { ...(prev[pageId] || {}), [lineNo]: choice },
        }));
    };

    const setPageChoice = (page: PendingPage, choice: Choice) => {
        const all: Record<number, Choice> = {};
        page.blocks.forEach((b) =>
            b.lines.filter((l) => l.role === 'pending').forEach((l) => { all[l.line_no] = choice; })
        );
        setDecisions((prev) => ({ ...prev, [page.page_id]: all }));
    };

    const choiceOf = (pageId: string, lineNo: number): Choice =>
        decisions[pageId]?.[lineNo] ?? 'body';

    const decidedCount = Object.values(decisions).reduce(
        (n, page) => n + Object.values(page).filter((c) => c === 'fragment').length, 0
    );

    return (
        <div className={styles.section}>
            <div className={styles.header}>
                <FileText size={22} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>v0.2.x の Memopedia を v0.3.x 用に変換</h3>
                    <p className={styles.description}>
                        ページ本文に積まれた日付ブロックを Fragment（箇条書き 1 行 = 1 件）へ移します。
                        文字を移すだけで、要約も言い換えもしません（LLM を呼びません）。
                        実行後は実行単位で丸ごと取り消せます。
                        <br />
                        <strong>変換中は、このペルソナのメモリーを他の画面から編集しないでください。</strong>
                        書き込みがぶつかると、変換が終わるまで待たされるか失敗します。
                    </p>
                </div>
            </div>

            <button className={styles.primaryButton} onClick={handlePreview} disabled={busy !== null}>
                {busy === 'preview'
                    ? <><Loader2 size={16} className={styles.loader} /> 下見しています...</>
                    : '下見する（ページと Fragment は変えません）'}
            </button>

            {error && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}

            {forceRun && (
                <div className={styles.pendingHeader}>
                    <AlertTriangle size={16} className={styles.warnIcon} />
                    <div>
                        <strong>変換より後の編集ごと変換前へ戻しますか？</strong>
                        <div className={styles.pendingHint}>
                            戻すと、その編集で書かれた内容も消えます。
                        </div>
                        <div className={styles.confirmRow} style={{ marginTop: '0.5rem' }}>
                            <button
                                className={styles.confirmYes}
                                onClick={() => handleRevert(forceRun, true)}
                                disabled={busy !== null}
                            >
                                承知のうえで戻す
                            </button>
                            <button className={styles.confirmNo} onClick={() => setForceRun(null)}>
                                やめる
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {result && <div className={styles.success}><Check size={16} /><span>{result}</span></div>}

            {preview && (
                <>
                    <div className={styles.summary}>
                        <div className={styles.summaryRow}>
                            <span className={styles.summaryLabel}>
                                ① 来歴が裏づけた行（そのまま Fragment に）
                                <span className={styles.subtle}>
                                    　日付の分かる箇条書きで、<em>同じ文を会話から自動で書き出した記録が残っている</em>もの。
                                    本文から移しても、ページ配下の Fragment として残ります
                                </span>
                            </span>
                            <span className={styles.summaryValue}>{preview.confirmed_count} 行</span>
                        </div>
                        <div className={styles.summaryRow}>
                            <span className={styles.summaryLabel}>
                                ② 記法だけが根拠の行（<strong>誰が書いたか未確定</strong>・要判断）
                            </span>
                            <span className={`${styles.summaryValue} ${preview.pending_count ? styles.warn : ''}`}>
                                {preview.pending_count} 行
                            </span>
                        </div>
                        <div className={styles.summaryRow}>
                            <span className={styles.summaryLabel}>
                                本文が変わるページ
                                {restating && <span className={styles.subtle}>　選択を反映して数え直しています…</span>}
                            </span>
                            <span className={styles.summaryValue}>
                                {preview.page_count} 枚（本文が空になる {preview.emptied_count} / 本文が残る {preview.kept_body_count}）
                            </span>
                        </div>
                        <div className={styles.summaryRow}>
                            <span className={styles.summaryLabel}>逐語の検算（行が消えない・増えない）</span>
                            <span className={`${styles.summaryValue} ${preview.is_safe ? styles.ok : styles.bad}`}>
                                {preview.conservation.before_lines} 行 → {preview.conservation.after_lines} 行
                                {preview.is_safe
                                    ? '（消失 0・捏造 0）'
                                    : `（消えた ${preview.conservation.lost_count} / 現れた ${preview.conservation.gained_count}）`}
                            </span>
                        </div>
                    </div>

                    {!preview.is_safe && (
                        <div className={styles.error}>
                            <AlertTriangle size={16} />
                            <span>
                                逐語の検算に通らないため実行できません。
                                {preview.verbatim_breaches.length > 0 && (
                                    <> 原文とずれたページ {preview.verbatim_breaches.length} 枚
                                    （例: {preview.verbatim_breaches[0].title} — {preview.verbatim_breaches[0].detail}）</>
                                )}
                            </span>
                        </div>
                    )}

                    {preview.marks.length > 0 && (
                        <div className={styles.marks}>
                            <div className={styles.marksTitle}>気づいた点</div>
                            {preview.marks.map((m, i) => (
                                <div key={i} className={styles.markRow}>
                                    <span className={styles.markKind}>{m.kind}</span>
                                    <span className={styles.markPage}>{m.page_title}:{m.line_no}</span>
                                    <span className={styles.markNote}>{m.note}</span>
                                </div>
                            ))}
                        </div>
                    )}

                    {preview.pending_pages.length > 0 && (
                        <div className={styles.pendingArea}>
                            <div className={styles.pendingHeader}>
                                <AlertTriangle size={16} className={styles.warnIcon} />
                                <div>
                                    <strong>
                                        この {preview.pending_count} 行を、Fragment へ移してよいか決めてください。
                                    </strong>
                                    <div className={styles.pendingHint}>
                                        日付の分かる箇条書きの形をしていますが、同じ文を自動で書き出した記録が見つかりません。
                                        <strong>本文に残しておきたいものが混ざっていないか</strong>を見て、1 行ずつ選んでください。
                                        選ばなかった行は本文に残ります。
                                        薄い字の行は判断の要らない行で、同じブロックの前後を文脈として並べています。
                                    </div>
                                </div>
                            </div>

                            {preview.pending_pages.map((page) => (
                                <div key={page.page_id} className={styles.pendingPage}>
                                    <div className={styles.pendingPageHead}>
                                        <span className={styles.pageTitle}>{page.title}</span>
                                        <span className={styles.bulkButtons}>
                                            <button onClick={() => setPageChoice(page, 'fragment')}>
                                                すべて Fragment に
                                            </button>
                                            <button onClick={() => setPageChoice(page, 'body')}>
                                                すべて本文に残す
                                            </button>
                                        </span>
                                    </div>

                                    {page.blocks.map((block, bi) => (
                                        <div key={bi} className={styles.block}>
                                            <div className={styles.blockDate}>{block.date}</div>
                                            {block.lines.map((line) => (
                                                <div
                                                    key={line.line_no}
                                                    className={
                                                        line.role === 'pending' ? styles.lineRow : styles.lineRowConfirmed
                                                    }
                                                >
                                                    <span className={styles.lineText}>{line.content}</span>
                                                    {line.role === 'pending' ? (
                                                        <span className={styles.lineChoice}>
                                                            <button
                                                                className={
                                                                    choiceOf(page.page_id, line.line_no) === 'fragment'
                                                                        ? styles.choiceActive : styles.choice
                                                                }
                                                                onClick={() => setChoice(page.page_id, line.line_no, 'fragment')}
                                                            >
                                                                Fragment に
                                                            </button>
                                                            <button
                                                                className={
                                                                    choiceOf(page.page_id, line.line_no) === 'body'
                                                                        ? styles.choiceActive : styles.choice
                                                                }
                                                                onClick={() => setChoice(page.page_id, line.line_no, 'body')}
                                                            >
                                                                本文に残す
                                                            </button>
                                                        </span>
                                                    ) : (
                                                        <span className={styles.confirmedTag}>
                                                            {line.role === 'fragment' ? '記録あり' : '本文に残ります'}
                                                        </span>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    ))}
                                </div>
                            ))}
                        </div>
                    )}

                    <div className={styles.applyArea}>
                        <div className={styles.applyNote}>
                            実行すると Fragment {preview.confirmed_count + decidedCount} 件を作ります
                            （来歴の裏づけ {preview.confirmed_count} + 判断済み {decidedCount}）。
                            {preview.pending_count - decidedCount > 0 && (
                                <> 残り {preview.pending_count - decidedCount} 行は本文に残します。</>
                            )}
                        </div>
                        {!confirming ? (
                            <button
                                className={styles.primaryButton}
                                onClick={() => setConfirming(true)}
                                disabled={busy !== null || !preview.is_safe}
                            >
                                実行する
                            </button>
                        ) : (
                            <div className={styles.confirmRow}>
                                <span>本当に実行しますか？（あとで取り消せます）</span>
                                <button className={styles.confirmYes} onClick={handleApply} disabled={busy !== null}>
                                    {busy === 'apply'
                                        ? <><Loader2 size={14} className={styles.loader} /> 変換中...</>
                                        : 'はい、実行する'}
                                </button>
                                <button className={styles.confirmNo} onClick={() => setConfirming(false)} disabled={busy !== null}>
                                    やめる
                                </button>
                            </div>
                        )}
                    </div>
                </>
            )}

            {runs.length > 0 && (
                <div className={styles.runs}>
                    <div className={styles.runsTitle}>取り消せる変換</div>
                    {runs.map((run) => (
                        <div key={run.run_id} className={styles.runRow}>
                            <span className={styles.runId}>{run.run_id}</span>
                            <span className={styles.runInfo}>
                                {new Date(run.converted_at * 1000).toLocaleString('ja-JP')} ／
                                {run.page_count} ページ・Fragment {run.fragment_count} 件
                            </span>
                            <button
                                className={styles.revertButton}
                                onClick={() => handleRevert(run.run_id)}
                                disabled={busy !== null}
                            >
                                {busy === `revert:${run.run_id}`
                                    ? <><Loader2 size={14} className={styles.loader} /> 取り消し中...</>
                                    : <><RotateCcw size={14} /> この変換を取り消す</>}
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
