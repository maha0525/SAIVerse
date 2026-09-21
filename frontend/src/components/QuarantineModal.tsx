"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    useLocale();
    const [entries, setEntries] = useState<QuarantineEntry[]>([]);
    const [loading, setLoading] = useState(false);
    const [selectedBackups, setSelectedBackups] = useState<Record<string, string>>({});
    const [actionInProgress, setActionInProgress] = useState<string | null>(null);
    const [actionResult, setActionResult] = useState<{ buildingId: string; result: ActionResponse } | null>(null);

    const fetchEntries = async () => {
        setLoading(true);
        try {
            const res = await apiFetch("/api/system/quarantine");
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
            alert(uiText("components.QuarantineModal.text001"));
            return;
        }
        if (!confirm(uiText("components.QuarantineModal.text002", { p1: buildingId, p2: getBaseName(backupFilename) }))) {
            return;
        }
        setActionInProgress(buildingId);
        try {
            const res = await apiFetch(`/api/system/quarantine/${encodeURIComponent(buildingId)}/restore`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ backup_filename: backupFilename }),
            });
            const data = await res.json();
            if (!res.ok) {
                setActionResult({ buildingId, result: { success: false, message: data.detail || uiText("components.QuarantineModal.text003") } });
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
            uiText("components.QuarantineModal.text004", { p1: buildingId }) +
            uiText("components.QuarantineModal.text005")
        )) {
            return;
        }
        setActionInProgress(buildingId);
        try {
            const res = await apiFetch(`/api/system/quarantine/${encodeURIComponent(buildingId)}/reset`, {
                method: "POST",
            });
            const data = await res.json();
            if (!res.ok) {
                setActionResult({ buildingId, result: { success: false, message: data.detail || uiText("components.QuarantineModal.text006") } });
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
                        <span data-i18n="components.QuarantineModal.text007">{uiText("components.QuarantineModal.text007")}</span>
                    </div>
                    <button className={styles.closeButton} onClick={onClose} aria-label={uiText("components.QuarantineModal.label001")}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.modalBody}>
                    {loading && <p data-i18n="components.QuarantineModal.text008" className={styles.message}>{uiText("components.QuarantineModal.text008")}</p>}
                    {!loading && entries.length === 0 && (
                        <p data-i18n="components.QuarantineModal.text009" className={styles.successMessage}>{uiText("components.QuarantineModal.text009")}</p>
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
                                    <dt data-i18n="components.QuarantineModal.text010">{uiText("components.QuarantineModal.text010")}</dt>
                                    <dd>{translateReason(entry.reason)}</dd>
                                </div>
                                <div className={styles.metaRow}>
                                    <dt data-i18n="components.QuarantineModal.text011">{uiText("components.QuarantineModal.text011")}</dt>
                                    <dd data-i18n="components.QuarantineModal.text012" className={styles.path}>
                                        {entry.corrupted_path || uiText("components.QuarantineModal.text012")}
                                    </dd>
                                </div>
                                {entry.rescue_error && (
                                    <div className={styles.metaRow}>
                                        <dt data-i18n="components.QuarantineModal.text013">{uiText("components.QuarantineModal.text013")}</dt>
                                        <dd className={styles.errorText}>{entry.rescue_error}</dd>
                                    </div>
                                )}
                            </dl>

                            <div className={styles.actionsSection}>
                                <h4 data-i18n="components.QuarantineModal.text014" className={styles.actionsTitle}>
                                    <RotateCcw size={16} />{uiText("components.QuarantineModal.text014")}</h4>
                                {entry.available_backups.length === 0 ? (
                                    <p data-i18n="components.QuarantineModal.text015" className={styles.subtleMessage}>{uiText("components.QuarantineModal.text015")}</p>
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
                                        <button data-i18n="components.QuarantineModal.text016"
                                            className={styles.primaryButton}
                                            onClick={() => handleRestore(entry.building_id)}
                                            disabled={actionInProgress === entry.building_id || !selectedBackups[entry.building_id]}
                                        >{uiText("components.QuarantineModal.text016")}</button>
                                    </>
                                )}
                            </div>

                            <div data-i18n="components.QuarantineModal.text017" className={styles.divider}>{uiText("components.QuarantineModal.text017")}</div>

                            <div className={styles.actionsSection}>
                                <h4 data-i18n="components.QuarantineModal.text018" className={styles.actionsTitle}>
                                    <Trash2 size={16} />{uiText("components.QuarantineModal.text018")}</h4>
                                <p data-i18n="components.QuarantineModal.text019" className={styles.subtleMessage}>{uiText("components.QuarantineModal.text019")}</p>
                                <button data-i18n="components.QuarantineModal.text020"
                                    className={styles.dangerButton}
                                    onClick={() => handleReset(entry.building_id)}
                                    disabled={actionInProgress === entry.building_id}
                                >{uiText("components.QuarantineModal.text020")}</button>
                            </div>

                            {actionResult?.buildingId === entry.building_id && (
                                <div className={`${styles.resultBox} ${actionResult.result.success ? styles.resultSuccess : styles.resultError}`}>
                                    {actionResult.result.message}
                                </div>
                            )}
                        </div>
                    ))}

                    <div className={styles.helperBox}>
                        <h4 data-i18n="components.QuarantineModal.text021">{uiText("components.QuarantineModal.text021")}</h4>
                        <p data-i18n="components.QuarantineModal.text022 components.QuarantineModal.text023 components.QuarantineModal.text024">{uiText("components.QuarantineModal.text022")}<code> {uiText("components.QuarantineModal.label002")}</code>{uiText("components.QuarantineModal.text023")}<code> {uiText("components.QuarantineModal.label003")}</code>{uiText("components.QuarantineModal.text024")}</p>
                    </div>
                </div>
            </div>
        </div>
    );
}

function translateReason(reason: string): string {
    const map: Record<string, string> = {
        corrupted: uiText("components.QuarantineModal.text025"),
        zero_byte: uiText("components.QuarantineModal.text026"),
        invalid_structure: uiText("components.QuarantineModal.text027"),
    };
    return map[reason] || reason;
}

function getBaseName(path: string): string {
    return path.split(/[/\\]/).pop() || path;
}
