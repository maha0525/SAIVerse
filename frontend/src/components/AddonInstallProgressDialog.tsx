'use client';

import React, { useEffect, useRef, useState } from 'react';
import { CheckCircle2, AlertCircle, AlertTriangle, X } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './AddonInstallProgressDialog.module.css';

export type CatalogOperation = 'install' | 'update' | 'uninstall';

interface Props {
    addonId: string;
    displayName: string;
    operation: CatalogOperation;
    deleteData?: boolean;
    onClose: () => void;
}

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
}

function opLabel(op: CatalogOperation): string {
    switch (op) {
        case 'install': return '導入';
        case 'update': return '更新';
        case 'uninstall': return '削除';
    }
}

export default function AddonInstallProgressDialog({
    addonId,
    displayName,
    operation,
    deleteData,
    onClose,
}: Props) {
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [finished, setFinished] = useState<FinishedState | null>(null);
    const [streamError, setStreamError] = useState<string | null>(null);
    const [currentStep, setCurrentStep] = useState<{ index: number; total: number } | null>(null);
    const logContainerRef = useRef<HTMLDivElement | null>(null);
    const nextLogIdRef = useRef(1);
    const abortRef = useRef<AbortController | null>(null);

    // SSE 接続を開始
    useEffect(() => {
        const ac = new AbortController();
        abortRef.current = ac;

        const path = `/api/addon-catalog/${operation}`;
        const body: Record<string, unknown> = { addon_id: addonId };
        if (operation === 'uninstall' && deleteData != null) {
            body.delete_data = deleteData;
        }

        (async () => {
            try {
                const res = await fetch(path, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                    },
                    body: JSON.stringify(body),
                    signal: ac.signal,
                });

                if (!res.ok) {
                    const text = await res.text();
                    setStreamError(`HTTP ${res.status}: ${text}`);
                    return;
                }
                if (!res.body) {
                    setStreamError('レスポンスボディがありません');
                    return;
                }

                const reader = res.body.getReader();
                const decoder = new TextDecoder('utf-8');
                let buffer = '';

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });

                    // SSE は "data: <json>\n\n" の繰り返し
                    while (true) {
                        const sep = buffer.indexOf('\n\n');
                        if (sep < 0) break;
                        const block = buffer.slice(0, sep);
                        buffer = buffer.slice(sep + 2);

                        for (const line of block.split('\n')) {
                            if (!line.startsWith('data: ')) continue;
                            const payload = line.slice(6);
                            try {
                                const ev = JSON.parse(payload);
                                handleEvent(ev);
                            } catch {
                                // malformed SSE — skip
                            }
                        }
                    }
                }
            } catch (e) {
                if (ac.signal.aborted) return;
                const msg = e instanceof Error ? e.message : String(e);
                setStreamError(msg);
            }
        })();

        return () => {
            ac.abort();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [addonId, operation, deleteData]);

    function handleEvent(ev: {
        phase: string;
        message?: string;
        step_index?: number;
        step_total?: number;
        ok?: boolean;
        error?: string;
        restart_required?: boolean;
        manifest?: { name: string; version: string; setup_version: number };
    }) {
        if (ev.step_index != null && ev.step_total != null) {
            setCurrentStep({ index: ev.step_index, total: ev.step_total });
        }
        if (ev.phase === 'finished') {
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

    // ログ自動スクロール
    useEffect(() => {
        const el = logContainerRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [logs.length]);

    const inProgress = finished == null && streamError == null;

    // 実行中はオーバーレイクリックで閉じさせない (ユーザーが誤って中断しないように)
    const handleOverlayClose = inProgress ? () => { /* noop while running */ } : onClose;

    return (
        <ModalOverlay onClose={handleOverlayClose}>
            <div className={styles.dialog} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <div>
                        <h3>{displayName} を{opLabel(operation)}中</h3>
                        <div className={styles.subId}>{addonId}</div>
                    </div>
                    {!inProgress && (
                        <button className={styles.closeBtn} onClick={onClose}>
                            <X size={18} />
                        </button>
                    )}
                </div>

                {currentStep && inProgress && (
                    <div className={styles.progressBar}>
                        <div
                            className={styles.progressFill}
                            style={{ width: `${(currentStep.index / currentStep.total) * 100}%` }}
                        />
                        <div className={styles.progressText}>
                            ステップ {currentStep.index} / {currentStep.total}
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

                {finished && (
                    <div className={`${styles.resultBox} ${finished.ok ? styles.resultOk : styles.resultErr}`}>
                        {finished.ok ? (
                            <>
                                <CheckCircle2 size={18} />
                                <div className={styles.resultText}>
                                    <strong>{opLabel(operation)}完了</strong>
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
                                    <strong>{opLabel(operation)}失敗</strong>
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
                        <span>
                            このアドオンは API ルートを持つため、完全に反映するには SAIVerse の再起動が必要です。
                        </span>
                    </div>
                )}

                {streamError && (
                    <div className={`${styles.resultBox} ${styles.resultErr}`}>
                        <AlertCircle size={18} />
                        <div className={styles.resultText}>
                            <strong>通信エラー</strong>
                            <span className={styles.resultDetail}>{streamError}</span>
                        </div>
                    </div>
                )}

                <div className={styles.footer}>
                    {inProgress ? (
                        <div className={styles.runningHint}>実行中... 完了までお待ちください</div>
                    ) : (
                        <button className={styles.btnPrimary} onClick={onClose}>
                            閉じる
                        </button>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
