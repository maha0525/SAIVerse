"use client";

import { useEffect, useState } from "react";
import { X, AlertTriangle, RotateCcw, Trash2, FileWarning } from "lucide-react";
import styles from "./QuarantineModal.module.css";

interface QuarantineEntry {
    building_id: string;
    building_name?: string;
    reason: string;
    original_path: string;
    corrupted_path: string | null;
    rescue_error: string | null;
    available_backups: string[];
    detected_at: string;
}

interface QuarantineListResponse {
    quarantined: QuarantineEntry[];
}

interface ActionResponse {
    success: boolean;
    message: string;
}

interface Props {
    isOpen: boolean;
    onClose: () => void;
    onResolved?: () => void;
}

export default function QuarantineModal({ isOpen, onClose, onResolved }: Props) {
    const [entries, setEntries] = useState<QuarantineEntry[]>([]);
    const [loading, setLoading] = useState(false);
    const [selectedBackups, setSelectedBackups] = useState<Record<string, string>>({});
    const [actionInProgress, setActionInProgress] = useState<string | null>(null);
    const [actionResult, setActionResult] = useState<{ buildingId: string; result: ActionResponse } | null>(null);

    const fetchEntries = async () => {
        setLoading(true);
        try {
            const res = await fetch("/api/system/quarantine");
            if (!res.ok) return;
            const data: QuarantineListResponse = await res.json();
            setEntries(data.quarantined || []);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        if (isOpen) {
            fetchEntries();
            setActionResult(null);
        }
    }, [isOpen]);

    const handleRestore = async (buildingId: string) => {
        const backupFilename = selectedBackups[buildingId];
        if (!backupFilename) {
            alert("バックアップを選択してください。");
            return;
        }
        if (!confirm(`「${buildingId}」を以下のバックアップから復元します。よろしいですか？\n\n${getBaseName(backupFilename)}`)) {
            return;
        }
        setActionInProgress(buildingId);
        try {
            const res = await fetch(`/api/system/quarantine/${encodeURIComponent(buildingId)}/restore`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ backup_filename: backupFilename }),
            });
            const data = await res.json();
            if (!res.ok) {
                setActionResult({ buildingId, result: { success: false, message: data.detail || "復元失敗" } });
                return;
            }
            setActionResult({ buildingId, result: data });
            await fetchEntries();
            onResolved?.();
            window.dispatchEvent(
                new CustomEvent("quarantine-resolved", { detail: { buildingId } })
            );
        } finally {
            setActionInProgress(null);
        }
    };

    const handleReset = async (buildingId: string) => {
        if (!confirm(
            `「${buildingId}」を空履歴で再開します。\n\n破損ファイルは退避済みなので無事ですが、` +
            `この操作後は新規会話のみ表示されます。本当に進めますか？`
        )) {
            return;
        }
        setActionInProgress(buildingId);
        try {
            const res = await fetch(`/api/system/quarantine/${encodeURIComponent(buildingId)}/reset`, {
                method: "POST",
            });
            const data = await res.json();
            if (!res.ok) {
                setActionResult({ buildingId, result: { success: false, message: data.detail || "リセット失敗" } });
                return;
            }
            setActionResult({ buildingId, result: data });
            await fetchEntries();
            onResolved?.();
            window.dispatchEvent(
                new CustomEvent("quarantine-resolved", { detail: { buildingId } })
            );
        } finally {
            setActionInProgress(null);
        }
    };

    if (!isOpen) return null;

    return (
        <div className={styles.overlay} onClick={onClose}>
            <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                <div className={styles.modalHeader}>
                    <div className={styles.modalTitle}>
                        <AlertTriangle size={22} className={styles.warningIcon} />
                        <span>破損ビルディングの管理</span>
                    </div>
                    <button className={styles.closeButton} onClick={onClose} aria-label="Close">
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.modalBody}>
                    {loading && <p className={styles.message}>読み込み中...</p>}
                    {!loading && entries.length === 0 && (
                        <p className={styles.successMessage}>
                            ✓ 隔離中のビルディングはありません。すべて正常です。
                        </p>
                    )}
                    {entries.map((entry) => (
                        <div key={entry.building_id} className={styles.entry}>
                            <h3 className={styles.entryTitle}>
                                <FileWarning size={18} />
                                {entry.building_name || entry.building_id}
                                <span className={styles.entryId}>({entry.building_id})</span>
                            </h3>

                            <dl className={styles.metaList}>
                                <div className={styles.metaRow}>
                                    <dt>異常理由:</dt>
                                    <dd>{translateReason(entry.reason)}</dd>
                                </div>
                                <div className={styles.metaRow}>
                                    <dt>退避先:</dt>
                                    <dd className={styles.path}>
                                        {entry.corrupted_path || "（退避失敗）"}
                                    </dd>
                                </div>
                                {entry.rescue_error && (
                                    <div className={styles.metaRow}>
                                        <dt>退避エラー:</dt>
                                        <dd className={styles.errorText}>{entry.rescue_error}</dd>
                                    </div>
                                )}
                            </dl>

                            <div className={styles.actionsSection}>
                                <h4 className={styles.actionsTitle}>
                                    <RotateCcw size={16} /> バックアップから復元
                                </h4>
                                {entry.available_backups.length === 0 ? (
                                    <p className={styles.subtleMessage}>利用可能なバックアップがありません。</p>
                                ) : (
                                    <>
                                        <ul className={styles.backupList}>
                                            {entry.available_backups.map((backup) => (
                                                <li key={backup} className={styles.backupItem}>
                                                    <label>
                                                        <input
                                                            type="radio"
                                                            name={`backup-${entry.building_id}`}
                                                            value={backup}
                                                            checked={selectedBackups[entry.building_id] === backup}
                                                            onChange={() =>
                                                                setSelectedBackups({
                                                                    ...selectedBackups,
                                                                    [entry.building_id]: backup,
                                                                })
                                                            }
                                                        />
                                                        <span className={styles.backupName}>{getBaseName(backup)}</span>
                                                    </label>
                                                </li>
                                            ))}
                                        </ul>
                                        <button
                                            className={styles.primaryButton}
                                            onClick={() => handleRestore(entry.building_id)}
                                            disabled={actionInProgress === entry.building_id || !selectedBackups[entry.building_id]}
                                        >
                                            選択したバックアップで復元
                                        </button>
                                    </>
                                )}
                            </div>

                            <div className={styles.divider}>または</div>

                            <div className={styles.actionsSection}>
                                <h4 className={styles.actionsTitle}>
                                    <Trash2 size={16} /> リセット（空履歴で再開）
                                </h4>
                                <p className={styles.subtleMessage}>
                                    破損ファイルは退避先に保持されたまま、新規空履歴で再開します。
                                    後から手動で復旧することも可能です。
                                </p>
                                <button
                                    className={styles.dangerButton}
                                    onClick={() => handleReset(entry.building_id)}
                                    disabled={actionInProgress === entry.building_id}
                                >
                                    リセット
                                </button>
                            </div>

                            {actionResult?.buildingId === entry.building_id && (
                                <div className={`${styles.resultBox} ${actionResult.result.success ? styles.resultSuccess : styles.resultError}`}>
                                    {actionResult.result.message}
                                </div>
                            )}
                        </div>
                    ))}

                    <div className={styles.helperBox}>
                        <h4>手動対応する場合</h4>
                        <p>
                            上記の操作を使わず手動で対応したい場合は、SAIVerseを停止してから
                            <code> .corrupted_*</code> ファイルを修復し、元の<code> log.json </code>
                            にリネームしてSAIVerseを再起動してください。
                        </p>
                    </div>
                </div>
            </div>
        </div>
    );
}

function translateReason(reason: string): string {
    const map: Record<string, string> = {
        corrupted: "JSONパース失敗（破損）",
        zero_byte: "0バイトファイル（書き込み中断の痕跡）",
        invalid_structure: "構造異常（配列ではない）",
    };
    return map[reason] || reason;
}

function getBaseName(path: string): string {
    return path.split(/[/\\]/).pop() || path;
}
