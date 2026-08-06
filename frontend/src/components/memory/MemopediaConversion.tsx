'use client';

/**
 * v0.2.x 以前の Memopedia を v0.3.x 用へ変換する（本文 → Fragment）。
 *
 * 設計: docs/intent/memopedia_body_to_fragment.md
 *
 * 判定は三段。①編集来歴が「抽出器が足した」と裏づけた行は自動で Fragment、
 * ②記法だけが根拠の行は保留、③保留行は 1 行ずつユーザーが決める。
 * 画面では保留行を最初から全チェック（=移行）で出し、ユーザーは手書きの行の
 * チェックを外す。エンジン側の契約（判断を渡さない行は本文に残る）は変えず、
 * チェック状態を明示的な判断として常に送る。
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
    /** pending=判断が要る / fragment=移行する（文脈） / body=本文に残る（文脈） */
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
    total_page_count: number;
    page_count: number;
    fragment_count: number;
    /** 同じ内容の Fragment が既にあり、新しくは作らず本文から抜くだけの行数 */
    dedup_count: number;
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
    dedup_count: number;
}

/** 保留行をすべて「移行する」に初期化した判断セット。 */
const initialDecisions = (p: Preview): Record<string, Record<number, Choice>> => {
    const init: Record<string, Record<number, Choice>> = {};
    p.pending_pages.forEach((page) => {
        const lines: Record<number, Choice> = {};
        page.blocks.forEach((b) =>
            b.lines.filter((l) => l.role === 'pending')
                .forEach((l) => { lines[l.line_no] = 'fragment'; })
        );
        if (Object.keys(lines).length > 0) init[page.page_id] = lines;
    });
    return init;
};

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
    // サーバの数字が反映済みの判断セット。同じ内容の数え直しを避ける
    const syncedRef = useRef<string>('');

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
            /* 一覧が取れなくても確認・実行はできる */
        }
    };

    const handlePreview = async () => {
        setBusy('preview');
        setError(null);
        setResult(null);
        setConfirming(false);
        try {
            const data: Preview = await call('/preview');
            // 保留行は全チェック（=移行）が初期状態。画面に出す数字も
            // 最初からその前提で数えたものを出す
            const init = initialDecisions(data);
            let shown = data;
            if (Object.keys(init).length > 0) {
                shown = await call('/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decisions: init }),
                });
            }
            syncedRef.current = JSON.stringify(init);
            setPreview(shown);
            setDecisions(init);
            await loadRuns();
        } catch (e) {
            setError(e instanceof Error ? e.message : '確認に失敗しました');
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

    // チェックを外すと、移行するページ数や行数も変わる。最初の数字を出したままだと
    // 自分の選択で何が起きるか見えないので、選択のたびに数え直す。
    useEffect(() => {
        if (!preview) return;
        if (Object.keys(decisions).length === 0) return;
        if (JSON.stringify(decisions) === syncedRef.current) return;
        // 選択が連打されたら前のリクエストを中断する。遅れて返ってきた古い数字が
        // 新しい選択の結果を上書きすると、画面と実行内容が食い違う。
        const controller = new AbortController();
        const timer = setTimeout(async () => {
            setRestating(true);
            try {
                const sent = JSON.stringify(decisionsRef.current);
                const data = await call('/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ decisions: decisionsRef.current }),
                    signal: controller.signal,
                });
                if (!controller.signal.aborted) {
                    syncedRef.current = sent;
                    setPreview(data);
                }
            } catch {
                /* 数字の更新に失敗しても、選択と実行は続けられる */
            } finally {
                if (!controller.signal.aborted) setRestating(false);
            }
        }, 600);
        return () => {
            clearTimeout(timer);
            controller.abort();
        };
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
        decisions[pageId]?.[lineNo] ?? 'fragment';

    const decidedCount = Object.values(decisions).reduce(
        (n, page) => n + Object.values(page).filter((c) => c === 'fragment').length, 0
    );
    const keptCount = preview ? preview.pending_count - decidedCount : 0;

    return (
        <div className={styles.section}>
            <div className={styles.header}>
                <FileText size={22} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>v0.2.x の Memopedia を v0.3.x 用に変換</h3>
                    <p className={styles.description}>
                        旧バージョンで自動生成された Memopedia ページを、記憶想起・検索に適した形式に変換します。
                        費用は掛かりません。
                        <br />
                        <strong>変換中は、このペルソナのメモリーを他の画面から編集しないでください。</strong>
                        書き込みがぶつかると、変換が終わるまで待たされるか失敗します。
                    </p>
                </div>
            </div>

            <button className={styles.primaryButton} onClick={handlePreview} disabled={busy !== null}>
                {busy === 'preview'
                    ? <><Loader2 size={16} className={styles.loader} /> 確認しています...</>
                    : '変換対象を確認'}
            </button>
            <p className={styles.subtle}>
                確認した後、変換を実行可能になります。確認処理はデータを一切変更しません。
            </p>

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
                                全 {preview.total_page_count} ページ中 {preview.page_count} ページに対して移行処理を行います
                                {preview.kept_body_count > 0 && (
                                    <>（うち {preview.kept_body_count} 件は手動での編集内容を保持します）</>
                                )}。
                                {restating && (
                                    <span className={styles.subtle}>選択を反映して数え直しています…</span>
                                )}
                            </span>
                        </div>
                    </div>

                    {!preview.is_safe && (
                        <div className={styles.error}>
                            <AlertTriangle size={16} />
                            <span>
                                変換の前後で本文の内容が一致しないため、実行できません（データは変更されていません）。
                                {preview.verbatim_breaches.length > 0 && (
                                    <> ずれのあるページ {preview.verbatim_breaches.length} 枚
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
                                    <strong>自動生成かどうか確認できなかった行があります。</strong>
                                    <div className={styles.pendingHint}>
                                        この {preview.pending_count} 行に、あなたやペルソナが手動で書いた文が混入していないか確認してください。
                                        移行したくない行はチェックを外してください。外した行は本文に残ります。
                                        薄い字の行は前後の文脈で、判断は要りません。
                                    </div>
                                </div>
                            </div>

                            {preview.pending_pages.map((page) => (
                                <div key={page.page_id} className={styles.pendingPage}>
                                    <div className={styles.pendingPageHead}>
                                        <span className={styles.pageTitle}>{page.title}</span>
                                        <span className={styles.bulkButtons}>
                                            <button onClick={() => setPageChoice(page, 'fragment')}>
                                                すべてチェック
                                            </button>
                                            <button onClick={() => setPageChoice(page, 'body')}>
                                                すべて外す
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
                                                    {line.role === 'pending' ? (
                                                        <label className={styles.lineCheck}>
                                                            <input
                                                                type="checkbox"
                                                                checked={choiceOf(page.page_id, line.line_no) === 'fragment'}
                                                                onChange={(e) => setChoice(
                                                                    page.page_id, line.line_no,
                                                                    e.target.checked ? 'fragment' : 'body',
                                                                )}
                                                            />
                                                            <span className={styles.lineText}>{line.content}</span>
                                                        </label>
                                                    ) : (
                                                        <>
                                                            <span className={styles.lineText}>{line.content}</span>
                                                            <span className={styles.confirmedTag}>
                                                                {line.role === 'fragment' ? '移行します' : '本文に残ります'}
                                                            </span>
                                                        </>
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
                            移行対象: {preview.confirmed_count + decidedCount} 行　保留: {keptCount} 行
                            {preview.dedup_count > 0 && (
                                <span className={styles.subtle}>
                                    移行対象のうち {preview.dedup_count} 行は同じ内容が記録済みのため、重複させずにまとめます。
                                </span>
                            )}
                        </div>
                        {!confirming ? (
                            <button
                                className={styles.primaryButton}
                                onClick={() => setConfirming(true)}
                                disabled={busy !== null || !preview.is_safe}
                            >
                                変換を実行
                            </button>
                        ) : (
                            <div className={styles.confirmRow}>
                                <span>本当に実行しますか？</span>
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
                    <div className={styles.runsTitle}>実行履歴</div>
                    {runs.map((run) => (
                        <div key={run.run_id} className={styles.runRow}>
                            <span className={styles.runId}>{run.run_id}</span>
                            <span className={styles.runInfo}>
                                {new Date(run.converted_at * 1000).toLocaleString('ja-JP')} ／
                                {run.page_count} ページ・{run.fragment_count + run.dedup_count} 行を移行
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
