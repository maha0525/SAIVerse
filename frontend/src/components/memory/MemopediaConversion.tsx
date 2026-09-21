'use client';
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    useLocale();
    const [preview, setPreview] = useState<Preview | null>(null);
    const [decisions, setDecisions] = useState<Record<string, Record<number, Choice>>>({});
    const [runs, setRuns] = useState<Run[]>([]);
    const [busy, setBusy] = useState<string | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<string | null>(null);
    const [confirming, setConfirming] = useState(false);
    const [forceRun, setForceRun] = useState<string | null>(null);

    const [restating, setRestating] = useState(false);
    const [restateFailed, setRestateFailed] = useState(false);
    const base = `/api/people/${personaId}/debug/memopedia-conversion`;
    const decisionsRef = useRef(decisions);
    decisionsRef.current = decisions;
    // サーバの数字が反映済みの判断セット。同じ内容の数え直しを避ける。
    // 画面の「実行」ボタンの可否にも使うので state で持つ（ref だけだと
    // 数え直しが終わってもボタンの状態が変わらない）
    const [syncedKey, setSyncedKey] = useState<string>('');
    const syncedRef = useRef<string>('');
    const syncDecisions = (key: string) => {
        syncedRef.current = key;
        setSyncedKey(key);
    };

    const call = async (path: string, init?: RequestInit) => {
        const res = await apiFetch(`${base}${path}`, init);
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
        setRestateFailed(false);
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
            syncDecisions(JSON.stringify(init));
            setPreview(shown);
            setDecisions(init);
            await loadRuns();
        } catch (e) {
            setError(e instanceof Error ? e.message : uiText("components.memory.MemopediaConversion.text001"));
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
            setResult(data.message || uiText("components.memory.MemopediaConversion.text002"));
            setPreview(null);
            setDecisions({});
            syncDecisions('');
            setConfirming(false);
            setRestateFailed(false);
            await loadRuns();
        } catch (e) {
            setError(e instanceof Error ? e.message : uiText("components.memory.MemopediaConversion.text003"));
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
            setResult(data.message || uiText("components.memory.MemopediaConversion.text004"));
            setPreview(null);
            await loadRuns();
        } catch (e) {
            // 変換より後の編集があると既定で拒否される。何を失うかを見せてから選ばせる。
            setError(e instanceof Error ? e.message : uiText("components.memory.MemopediaConversion.text005"));
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
                    syncDecisions(sent);
                    setRestateFailed(false);
                    setPreview(data);
                }
            } catch {
                // 数え直しに失敗したら、画面の数字は選択より古いまま。その状態で
                // 実行させると「見えている数字と実行内容が違う」ので、下の実行
                // ボタンを止めて確認からやり直してもらう
                if (!controller.signal.aborted) setRestateFailed(true);
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
    // 画面の数字がいまの選択を反映していないあいだは実行させない。サーバ側は
    // 送られた選択で計算し直して指紋も確かめるので危険な変換は通らないが、
    // 「画面に出ている件数」と「実行される内容」が食い違ったまま押させない
    const unsynced = JSON.stringify(decisions) !== syncedKey;
    const canApply = !!preview && preview.is_safe && !restating && !unsynced && !restateFailed;

    return (
        <div className={styles.section}>
            <div className={styles.header}>
                <FileText size={22} className={styles.icon} />
                <div>
                    <h3 data-i18n="components.memory.MemopediaConversion.text006" className={styles.title}>{uiText("components.memory.MemopediaConversion.text006")}</h3>
                    <p data-i18n="components.memory.MemopediaConversion.text007 components.memory.MemopediaConversion.text009" className={styles.description}>{uiText("components.memory.MemopediaConversion.text007")}<br />
                        <strong data-i18n="components.memory.MemopediaConversion.text008">{uiText("components.memory.MemopediaConversion.text008")}</strong>{uiText("components.memory.MemopediaConversion.text009")}</p>
                </div>
            </div>

            <button data-i18n="components.memory.MemopediaConversion.text010 components.memory.MemopediaConversion.text011" className={styles.primaryButton} onClick={handlePreview} disabled={busy !== null}>
                {busy === 'preview'
                    ? <><Loader2 size={16} className={styles.loader} />{uiText("components.memory.MemopediaConversion.text010")}</>
                    : uiText("components.memory.MemopediaConversion.text011")}
            </button>
            <p data-i18n="components.memory.MemopediaConversion.text012" className={styles.subtle}>{uiText("components.memory.MemopediaConversion.text012")}</p>

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
                        <strong data-i18n="components.memory.MemopediaConversion.text013">{uiText("components.memory.MemopediaConversion.text013")}</strong>
                        <div data-i18n="components.memory.MemopediaConversion.text014" className={styles.pendingHint}>{uiText("components.memory.MemopediaConversion.text014")}</div>
                        <div className={styles.confirmRow} style={{ marginTop: '0.5rem' }}>
                            <button data-i18n="components.memory.MemopediaConversion.text015"
                                className={styles.confirmYes}
                                onClick={() => handleRevert(forceRun, true)}
                                disabled={busy !== null}
                            >{uiText("components.memory.MemopediaConversion.text015")}</button>
                            <button data-i18n="components.memory.MemopediaConversion.text016" className={styles.confirmNo} onClick={() => setForceRun(null)}>{uiText("components.memory.MemopediaConversion.text016")}</button>
                        </div>
                    </div>
                </div>
            )}

            {result && <div className={styles.success}><Check size={16} /><span>{result}</span></div>}

            {preview && (
                <>
                    <div className={styles.summary}>
                        <div className={styles.summaryRow}>
                            <span data-i18n="components.memory.MemopediaConversion.text017 components.memory.MemopediaConversion.text018 components.memory.MemopediaConversion.text019 components.memory.MemopediaConversion.text020 components.memory.MemopediaConversion.text021 components.memory.MemopediaConversion.period" className={styles.summaryLabel}>{uiText("components.memory.MemopediaConversion.text017")}{preview.total_page_count}{uiText("components.memory.MemopediaConversion.text018")}{preview.page_count}{uiText("components.memory.MemopediaConversion.text019")}{preview.kept_body_count > 0 && (
                                    <>{uiText("components.memory.MemopediaConversion.text020")}{preview.kept_body_count}{uiText("components.memory.MemopediaConversion.text021")}</>
                                )}{uiText("components.memory.MemopediaConversion.period")}
                                {restating && (
                                    <span data-i18n="components.memory.MemopediaConversion.text022" className={styles.subtle}>{uiText("components.memory.MemopediaConversion.text022")}</span>
                                )}
                            </span>
                        </div>
                    </div>

                    {!preview.is_safe && (
                        <div className={styles.error}>
                            <AlertTriangle size={16} />
                            <span data-i18n="components.memory.MemopediaConversion.text023 components.memory.MemopediaConversion.text024 components.memory.MemopediaConversion.text025">{uiText("components.memory.MemopediaConversion.text023")}{preview.verbatim_breaches.length > 0 && (
                                    <>{uiText("components.memory.MemopediaConversion.text024")}{preview.verbatim_breaches.length}{uiText("components.memory.MemopediaConversion.text025", { p1: preview.verbatim_breaches[0].title, p2: preview.verbatim_breaches[0].detail })}</>
                                )}
                            </span>
                        </div>
                    )}

                    {preview.marks.length > 0 && (
                        <div className={styles.marks}>
                            <div data-i18n="components.memory.MemopediaConversion.text026" className={styles.marksTitle}>{uiText("components.memory.MemopediaConversion.text026")}</div>
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
                                    <strong data-i18n="components.memory.MemopediaConversion.text027">{uiText("components.memory.MemopediaConversion.text027")}</strong>
                                    <div data-i18n="components.memory.MemopediaConversion.text028 components.memory.MemopediaConversion.text029" className={styles.pendingHint}>{uiText("components.memory.MemopediaConversion.text028")}{preview.pending_count}{uiText("components.memory.MemopediaConversion.text029")}</div>
                                </div>
                            </div>

                            {preview.pending_pages.map((page) => (
                                <div key={page.page_id} className={styles.pendingPage}>
                                    <div className={styles.pendingPageHead}>
                                        <span className={styles.pageTitle}>{page.title}</span>
                                        <span className={styles.bulkButtons}>
                                            <button data-i18n="components.memory.MemopediaConversion.text030" onClick={() => setPageChoice(page, 'fragment')}>{uiText("components.memory.MemopediaConversion.text030")}</button>
                                            <button data-i18n="components.memory.MemopediaConversion.text031" onClick={() => setPageChoice(page, 'body')}>{uiText("components.memory.MemopediaConversion.text031")}</button>
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
                                                            <span data-i18n="components.memory.MemopediaConversion.text032 components.memory.MemopediaConversion.text033" className={styles.confirmedTag}>
                                                                {line.role === 'fragment' ? uiText("components.memory.MemopediaConversion.text032") : uiText("components.memory.MemopediaConversion.text033")}
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
                        <div data-i18n="components.memory.MemopediaConversion.text034 components.memory.MemopediaConversion.text035 components.memory.MemopediaConversion.text036" className={styles.applyNote}>{uiText("components.memory.MemopediaConversion.text034")}{preview.confirmed_count + decidedCount}{uiText("components.memory.MemopediaConversion.text035")}{keptCount}{uiText("components.memory.MemopediaConversion.text036")}{preview.dedup_count > 0 && (
                                <span data-i18n="components.memory.MemopediaConversion.text037 components.memory.MemopediaConversion.text038" className={styles.subtle}>{uiText("components.memory.MemopediaConversion.text037")}{preview.dedup_count}{uiText("components.memory.MemopediaConversion.text038")}</span>
                            )}
                        </div>
                        {restateFailed && (
                            <div className={styles.error}>
                                <AlertCircle size={16} />
                                <span data-i18n="components.memory.MemopediaConversion.text039">{uiText("components.memory.MemopediaConversion.text039")}</span>
                            </div>
                        )}
                        {!confirming ? (
                            <button data-i18n="components.memory.MemopediaConversion.text040 components.memory.MemopediaConversion.text041"
                                className={styles.primaryButton}
                                onClick={() => setConfirming(true)}
                                disabled={busy !== null || !canApply}
                            >
                                {restating ? uiText("components.memory.MemopediaConversion.text040") : uiText("components.memory.MemopediaConversion.text041")}
                            </button>
                        ) : (
                            <div className={styles.confirmRow}>
                                <span data-i18n="components.memory.MemopediaConversion.text042">{uiText("components.memory.MemopediaConversion.text042")}</span>
                                <button data-i18n="components.memory.MemopediaConversion.text043 components.memory.MemopediaConversion.text044"
                                    className={styles.confirmYes}
                                    onClick={handleApply}
                                    disabled={busy !== null || !canApply}
                                >
                                    {busy === 'apply'
                                        ? <><Loader2 size={14} className={styles.loader} />{uiText("components.memory.MemopediaConversion.text043")}</>
                                        : uiText("components.memory.MemopediaConversion.text044")}
                                </button>
                                <button data-i18n="components.memory.MemopediaConversion.text045" className={styles.confirmNo} onClick={() => setConfirming(false)} disabled={busy !== null}>{uiText("components.memory.MemopediaConversion.text045")}</button>
                            </div>
                        )}
                    </div>
                </>
            )}

            {runs.length > 0 && (
                <div className={styles.runs}>
                    <div data-i18n="components.memory.MemopediaConversion.text046" className={styles.runsTitle}>{uiText("components.memory.MemopediaConversion.text046")}</div>
                    {runs.map((run) => (
                        <div key={run.run_id} className={styles.runRow}>
                            <span className={styles.runId}>{run.run_id}</span>
                            <span data-i18n="components.memory.MemopediaConversion.text047 components.memory.MemopediaConversion.text048" className={styles.runInfo}>
                                {new Date(run.converted_at * 1000).toLocaleString(getFormatLocale())} /
                                {run.page_count}{uiText("components.memory.MemopediaConversion.text047")}{run.fragment_count + run.dedup_count}{uiText("components.memory.MemopediaConversion.text048")}</span>
                            <button data-i18n="components.memory.MemopediaConversion.text049 components.memory.MemopediaConversion.text050"
                                className={styles.revertButton}
                                onClick={() => handleRevert(run.run_id)}
                                disabled={busy !== null}
                            >
                                {busy === `revert:${run.run_id}`
                                    ? <><Loader2 size={14} className={styles.loader} />{uiText("components.memory.MemopediaConversion.text049")}</>
                                    : <><RotateCcw size={14} />{uiText("components.memory.MemopediaConversion.text050")}</>}
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
