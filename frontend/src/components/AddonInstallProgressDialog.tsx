'use client';
import { apiFetch, parseUIEvent } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useEffect, useRef, useState } from 'react';
import { CheckCircle2, AlertCircle, AlertTriangle, Info, X } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './AddonInstallProgressDialog.module.css';

/**
 * - install / update: prepare で質問を受け取ったあとの confirm (答えを付ける)
 * - uninstall
 * - options: 導入済みのアドオンで、導入時の質問に選択肢を足す
 */
export type CatalogOperation = 'install' | 'update' | 'uninstall' | 'options';

interface Props {
    addonId: string;
    displayName: string;
    operation: CatalogOperation;
    deleteData?: boolean;
    /** install / update / options で送る答え ({question_id: [choice_id, ...]}) */
    answers?: Record<string, string[]>;
    onClose: () => void;
}

/** 操作ごとの SSE の口と body。旧 POST /install・/update は使わない。 */
function buildRequest(
    operation: CatalogOperation,
    addonId: string,
    deleteData: boolean | undefined,
    answers: Record<string, string[]> | undefined,
): { path: string; body: Record<string, unknown> } {
    switch (operation) {
        case 'install':
            return {
                path: '/api/addon-catalog/install/confirm',
                body: { addon_id: addonId, answers: answers ?? {} },
            };
        case 'update':
            return {
                path: '/api/addon-catalog/update/confirm',
                body: { addon_id: addonId, answers: answers ?? {} },
            };
        case 'options':
            return {
                path: `/api/addon-catalog/installed/${encodeURIComponent(addonId)}/options`,
                body: { answers: answers ?? {} },
            };
        case 'uninstall': {
            const body: Record<string, unknown> = { addon_id: addonId };
            if (deleteData != null) body.delete_data = deleteData;
            return { path: '/api/addon-catalog/uninstall', body };
        }
    }
}

// サーバーは進捗の行が出ない間も 10 秒ごとに keepalive (SSE のコメント行) を送る
// (api/routes/addon_catalog.py の _SSE_KEEPALIVE_SEC)。これだけ何も届かなければ、
// 途中の中継が接続を切ったとみなす。中継が切っても、ブラウザ側の接続は
// 閉じられずに残ることがある (読み取りが終わりもせず失敗もしない) ので、
// 終わりや失敗を待つだけでは切断に気づけない。
const STALL_TIMEOUT_MS = 35_000;
const STALL_CHECK_INTERVAL_MS = 5_000;
// 切断後に「まだ走っているか」を問い合わせる間隔
const STATUS_POLL_INTERVAL_MS = 3_000;

interface LogEntry {
    id: number;
    phase: string;
    message: string;
    step_index?: number;
    step_total?: number;
}

interface FinishedState {
    ok: boolean;
    error?: string;
    restart_required?: boolean;
    manifest?: { name: string; version: string; setup_version: number };
    /** 切断後に終わりを見届けたが、どう終わったかは分からない (結果の記録が無い) */
    unknown?: boolean;
}

/** GET /api/addon-catalog/operations/{addon_id} の答え */
interface OperationStatus {
    running: boolean;
    operation_id: string | null;
    last_result: {
        operation_id?: string;
        ok?: boolean;
        error?: string;
        restart_required?: boolean;
        manifest?: { name: string; version: string; setup_version: number };
    } | null;
}

function opLabel(op: CatalogOperation): string {
    switch (op) {
        case 'install': return uiText("components.AddonInstallProgressDialog.text001");
        case 'update': return uiText("components.AddonInstallProgressDialog.text002");
        case 'uninstall': return uiText("components.AddonInstallProgressDialog.text003");
        case 'options': return uiText("components.AddonInstallProgressDialog.text014");
    }
}

export default function AddonInstallProgressDialog({
    addonId,
    displayName,
    operation,
    deleteData,
    answers,
    onClose,
}: Props) {
    useLocale();
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [finished, setFinished] = useState<FinishedState | null>(null);
    const [streamError, setStreamError] = useState<string | null>(null);
    const [currentStep, setCurrentStep] = useState<{ index: number; total: number } | null>(null);
    // SSE が途中で切れた (以後は状態の問い合わせで終わりを待つ)
    const [connectionLost, setConnectionLost] = useState(false);
    const logContainerRef = useRef<HTMLDivElement | null>(null);
    const nextLogIdRef = useRef(1);
    const abortRef = useRef<AbortController | null>(null);
    // 確認の要求 (confirm 等の POST) を送ったか。1 つの小窓につき 1 回だけ送る。
    const requestSentRef = useRef(false);
    // 片付けで予約した中断 (StrictMode の擬似的な付け外しでは取り消す)
    const pendingAbortRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    // サーバーが started で知らせた操作の id (切断後の結果の照合に使う)
    const operationIdRef = useRef<string | null>(null);
    const finishedRef = useRef(false);
    // 読み取り中か / 最後に何か (keepalive を含む) が届いた時刻
    const streamingRef = useRef(false);
    const lastDataAtRef = useRef(0);
    // 無通信を見て、こちらから読み取りを打ち切った
    const stalledRef = useRef(false);

    // SSE 接続を開始する。
    //
    // 確認の要求は 1 つの小窓につき 1 回だけ送る。React の開発モード (StrictMode) は
    // effect を「実行 → 片付け → 実行」と二度回すので、素直に書くと 1 回目の要求を
    // 片付けで中断し、2 回目を送り直す — サーバーには同じ操作の要求が二つ届きうる。
    // そこで、要求は最初の実行でだけ送り、片付けでの中断は次のタスクまで遅らせて、
    // 直後の再実行 (= 擬似的な付け外し) なら取り消す。本当に閉じたときだけ中断する。
    // (中断しても、サーバーの処理は止まらない。読むのをやめるだけ。)
    useEffect(() => {
        if (pendingAbortRef.current != null) {
            clearTimeout(pendingAbortRef.current);
            pendingAbortRef.current = null;
        }
        if (!requestSentRef.current) {
            requestSentRef.current = true;
            const ac = new AbortController();
            abortRef.current = ac;
            void runStream(ac);
        }
        return () => {
            pendingAbortRef.current = setTimeout(() => {
                pendingAbortRef.current = null;
                abortRef.current?.abort();
            }, 0);
        };
        // 1 つの小窓 = 1 つの操作。props は開いている間は変わらない (親が一度だけ
        // 作って渡す) ので、送り直しの契機にしない。
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    async function runStream(ac: AbortController) {
        const { path, body } = buildRequest(operation, addonId, deleteData, answers);
        let res: Response;
        try {
            res = await apiFetch(path, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'text/event-stream',
                },
                body: JSON.stringify(body),
                signal: ac.signal,
            });
        } catch (e) {
            if (ac.signal.aborted) return;
            const msg = e instanceof Error ? e.message : String(e);
            setStreamError(msg);
            return;
        }

        if (!res.ok) {
            const text = await res.text();
            setStreamError(`HTTP ${res.status}: ${text}`);
            return;
        }
        if (!res.body) {
            setStreamError(uiText("components.AddonInstallProgressDialog.text004"));
            return;
        }

        // ここから先、サーバーの処理は始まっている。読み取りがどう終わっても、
        // finished を受け取っていなければ「切断」として状態の問い合わせへ移る。
        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        streamingRef.current = true;
        lastDataAtRef.current = Date.now();
        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                lastDataAtRef.current = Date.now();
                buffer += decoder.decode(value, { stream: true });

                // SSE は "data: <json>\n\n" の繰り返し (": keepalive" のコメント行は読み飛ばす)
                while (true) {
                    const sep = buffer.indexOf('\n\n');
                    if (sep < 0) break;
                    const block = buffer.slice(0, sep);
                    buffer = buffer.slice(sep + 2);

                    for (const line of block.split('\n')) {
                        if (!line.startsWith('data: ')) continue;
                        const payload = line.slice(6);
                        try {
                            const ev = parseUIEvent(payload);
                            handleEvent(ev);
                        } catch {
                            // malformed SSE — skip
                        }
                    }
                }
            }
        } catch {
            // 小窓を閉じたことによる中断なら何もしない。無通信で打ち切った・
            // 接続が落ちたなら、下の立て直しへ
            if (ac.signal.aborted && !stalledRef.current) return;
        } finally {
            streamingRef.current = false;
        }
        if (!finishedRef.current) setConnectionLost(true);
    }

    function handleEvent(ev: {
        phase: string;
        message?: string;
        step_index?: number;
        step_total?: number;
        ok?: boolean;
        error?: string;
        restart_required?: boolean;
        manifest?: { name: string; version: string; setup_version: number };
        operation_id?: string;
    }) {
        if (ev.phase === 'started' && ev.operation_id) {
            operationIdRef.current = ev.operation_id;
        }
        if (ev.step_index != null && ev.step_total != null) {
            setCurrentStep({ index: ev.step_index, total: ev.step_total });
        }
        if (ev.phase === 'finished') {
            finishedRef.current = true;
            setFinished({
                ok: ev.ok ?? false,
                error: ev.error,
                restart_required: ev.restart_required,
                manifest: ev.manifest,
            });
            return;
        }
        const entry: LogEntry = {
            id: nextLogIdRef.current++,
            phase: ev.phase,
            message: ev.message ?? '',
            step_index: ev.step_index,
            step_total: ev.step_total,
        };
        setLogs((prev) => [...prev, entry]);
    }

    // 無通信の見張り: keepalive すら届かなくなったら、読み取りを打ち切る
    useEffect(() => {
        const timer = setInterval(() => {
            if (!streamingRef.current || stalledRef.current) return;
            if (Date.now() - lastDataAtRef.current > STALL_TIMEOUT_MS) {
                stalledRef.current = true;
                abortRef.current?.abort();
            }
        }, STALL_CHECK_INTERVAL_MS);
        return () => clearInterval(timer);
    }, []);

    // 切断後: 処理がまだ走っているかを問い合わせ続け、終わったら結果を出す
    useEffect(() => {
        if (!connectionLost || finished) return;
        let cancelled = false;
        let timer: ReturnType<typeof setTimeout> | null = null;
        const tick = async () => {
            try {
                const res = await apiFetch(`/api/addon-catalog/operations/${encodeURIComponent(addonId)}`);
                if (res.ok) {
                    const st: OperationStatus = await res.json();
                    if (cancelled) return;
                    const ours = operationIdRef.current;
                    // started を受け取れていなければ id で照合できないので、走っている
                    // 操作をこの小窓の操作とみなす
                    const stillRunning = st.running && (ours == null || st.operation_id === ours);
                    if (!stillRunning) {
                        const last = st.last_result;
                        finishedRef.current = true;
                        if (last && ours != null && last.operation_id === ours) {
                            setFinished({
                                ok: last.ok ?? false,
                                error: last.error,
                                restart_required: last.restart_required,
                                manifest: last.manifest,
                            });
                        } else {
                            // バックエンドの再起動などで結果の記録が無い。成否は言わない
                            setFinished({ ok: false, unknown: true });
                        }
                        return;
                    }
                }
            } catch {
                // バックエンドに届かない間も、問い合わせは続ける
            }
            if (!cancelled) timer = setTimeout(tick, STATUS_POLL_INTERVAL_MS);
        };
        void tick();
        return () => {
            cancelled = true;
            if (timer != null) clearTimeout(timer);
        };
    }, [connectionLost, finished, addonId]);

    // ログ自動スクロール
    useEffect(() => {
        const el = logContainerRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [logs.length]);

    const inProgress = finished == null && streamError == null;

    // 実行中はオーバーレイクリックで閉じさせない (誤って閉じないように)。
    // 閉じるのは × かフッターのボタンで — 閉じても処理はサーバーで続く。
    const handleOverlayClose = inProgress ? () => { /* noop while running */ } : onClose;

    return (
        <ModalOverlay onClose={handleOverlayClose}>
            <div className={styles.dialog} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <div>
                        {operation === 'options' ? (
                            <h3 data-i18n="components.AddonInstallProgressDialog.text015">{uiText("components.AddonInstallProgressDialog.text015", { p1: displayName })}</h3>
                        ) : (
                            <h3 data-i18n="components.AddonInstallProgressDialog.text005 components.AddonInstallProgressDialog.text006">{displayName}{uiText("components.AddonInstallProgressDialog.text005")}{opLabel(operation)}{uiText("components.AddonInstallProgressDialog.text006")}</h3>
                        )}
                        <div className={styles.subId}>{addonId}</div>
                    </div>
                    <button data-i18n="components.AddonInstallProgressDialog.text013"
                        type="button"
                        className={styles.closeBtn}
                        onClick={onClose}
                        aria-label={uiText("components.AddonInstallProgressDialog.text013")}
                        title={uiText("components.AddonInstallProgressDialog.text013")}
                    >
                        <X size={18} />
                    </button>
                </div>

                {currentStep && inProgress && (
                    <div className={styles.progressBar}>
                        <div
                            className={styles.progressFill}
                            style={{ width: `${(currentStep.index / currentStep.total) * 100}%` }}
                        />
                        <div data-i18n="components.AddonInstallProgressDialog.text007" className={styles.progressText}>{uiText("components.AddonInstallProgressDialog.text007")}{currentStep.index} / {currentStep.total}
                        </div>
                    </div>
                )}
                {inProgress && !currentStep && (
                    <div className={styles.indeterminateBar}>
                        <div className={styles.indeterminateFill} />
                    </div>
                )}

                <div ref={logContainerRef} className={styles.logArea}>
                    {logs.map((entry) => (
                        <div key={entry.id} className={styles.logLine}>
                            <span className={`${styles.logPhase} ${styles[`phase_${entry.phase}`] ?? ''}`}>
                                {entry.phase}
                            </span>
                            {entry.step_index != null && entry.step_total != null && (
                                <span className={styles.logStepIndex}>
                                    ({entry.step_index}/{entry.step_total})
                                </span>
                            )}
                            <span className={styles.logMessage}>{entry.message}</span>
                        </div>
                    ))}
                </div>

                {connectionLost && inProgress && (
                    <div className={styles.lostNote}>
                        <Info size={16} />
                        <span data-i18n="components.AddonInstallProgressDialog.text016">{uiText("components.AddonInstallProgressDialog.text016")}</span>
                    </div>
                )}

                {finished && finished.unknown && (
                    <div className={`${styles.resultBox} ${styles.resultUnknown}`}>
                        <Info size={18} />
                        <div className={styles.resultText}>
                            <strong data-i18n="components.AddonInstallProgressDialog.text017">{uiText("components.AddonInstallProgressDialog.text017")}</strong>
                            <span data-i18n="components.AddonInstallProgressDialog.text018" className={styles.resultDetail}>{uiText("components.AddonInstallProgressDialog.text018")}</span>
                        </div>
                    </div>
                )}

                {finished && !finished.unknown && (
                    <div className={`${styles.resultBox} ${finished.ok ? styles.resultOk : styles.resultErr}`}>
                        {finished.ok ? (
                            <>
                                <CheckCircle2 size={18} />
                                <div className={styles.resultText}>
                                    <strong data-i18n="components.AddonInstallProgressDialog.text008">{opLabel(operation)}{uiText("components.AddonInstallProgressDialog.text008")}</strong>
                                    {finished.manifest && (
                                        <span className={styles.resultDetail}>
                                            v{finished.manifest.version}
                                        </span>
                                    )}
                                </div>
                            </>
                        ) : (
                            <>
                                <AlertCircle size={18} />
                                <div className={styles.resultText}>
                                    <strong data-i18n="components.AddonInstallProgressDialog.text009">{opLabel(operation)}{uiText("components.AddonInstallProgressDialog.text009")}</strong>
                                    {finished.error && (
                                        <span className={styles.resultDetail}>{finished.error}</span>
                                    )}
                                </div>
                            </>
                        )}
                    </div>
                )}

                {finished?.ok && finished.restart_required && (
                    <div className={styles.restartNote}>
                        <AlertTriangle size={16} />
                        <span data-i18n="components.AddonInstallProgressDialog.text010">{uiText("components.AddonInstallProgressDialog.text010")}</span>
                    </div>
                )}

                {streamError && (
                    <div className={`${styles.resultBox} ${styles.resultErr}`}>
                        <AlertCircle size={18} />
                        <div className={styles.resultText}>
                            <strong data-i18n="components.AddonInstallProgressDialog.text011">{uiText("components.AddonInstallProgressDialog.text011")}</strong>
                            <span className={styles.resultDetail}>{streamError}</span>
                        </div>
                    </div>
                )}

                <div className={styles.footer}>
                    {inProgress ? (
                        <>
                            <div data-i18n="components.AddonInstallProgressDialog.text012" className={styles.runningHint}>{uiText("components.AddonInstallProgressDialog.text012")}</div>
                            <button data-i18n="components.AddonInstallProgressDialog.text019" className={styles.btnSecondary} onClick={onClose}>{uiText("components.AddonInstallProgressDialog.text019")}</button>
                        </>
                    ) : (
                        <button data-i18n="components.AddonInstallProgressDialog.text013" className={styles.btnPrimary} onClick={onClose}>{uiText("components.AddonInstallProgressDialog.text013")}</button>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
