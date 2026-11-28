'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    AlertTriangle,
    Database,
    HardDrive,
    Plus,
    RefreshCw,
    RotateCcw,
    Sparkles,
    Trash2,
} from 'lucide-react';
import { apiFetch } from '@/i18n/api';
import { getFormatLocale, t as uiText } from '@/i18n/core';
import { announceUpdateStarted } from '@/lib/releaseChannel';
import ModalOverlay from '../common/ModalOverlay';
import styles from './BackupManagementPanel.module.css';

interface SnapshotItem {
    name: string;
    format: 'cas' | 'zip';
    created_at: string | null;
    saiverse_version: string | null;
    note: string;
    file_count: number | null;
    total_bytes_uncompressed: number | null;
    archive_size_bytes: number | null;
    unique_bytes: number | null;
    readable: boolean;
}

interface SnapshotStats {
    cas_store_bytes: number;
    cas_object_count: number;
    cas_snapshot_count: number;
    legacy_zip_bytes: number;
    legacy_zip_count: number;
    total_disk_bytes: number;
    orphan_object_count: number;
    orphan_bytes: number;
    temp_file_count: number;
    temp_bytes: number;
    corrupted_manifests: string[];
}

interface PersonaBackupSummary {
    persona_id: string;
    display_name: string;
    registered: boolean;
    has_memory_db: boolean;
    memory_db_size_bytes: number;
    backup_count: number;
    latest_backup_at: string | null;
    has_rdiff_backup: boolean;
}

interface PersonaBackupEntry {
    filename: string;
    kind: 'backup' | 'pre_restore';
    size_bytes: number;
    created_at: string;
}

interface PersonaRestoreTarget {
    personaId: string;
    displayName: string;
    entry: PersonaBackupEntry;
}

interface BackupManagementPanelProps {
    onOperationStarted?: () => void;
}

type SubTab = 'snapshots' | 'personas';

function formatBytes(bytes: number | null | undefined): string {
    if (bytes == null || Number.isNaN(bytes)) return '-';
    if (bytes < 1024) return `${bytes} B`;
    const kb = bytes / 1024;
    if (kb < 1024) return `${kb.toFixed(1)} KB`;
    const mb = kb / 1024;
    if (mb < 1024) return `${mb.toFixed(1)} MB`;
    return `${(mb / 1024).toFixed(2)} GB`;
}

function formatTimestamp(iso: string | null | undefined): string {
    if (!iso) return '-';
    try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        return d.toLocaleString(getFormatLocale(), {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        });
    } catch {
        return iso;
    }
}

async function readErrorDetail(res: Response, fallback: string): Promise<string> {
    try {
        const body = await res.json();
        if (body && typeof body.detail === 'string' && body.detail.trim()) {
            return body.detail.trim();
        }
    } catch {
        // ignore non-JSON body
    }
    return fallback;
}

export default function BackupManagementPanel({ onOperationStarted }: BackupManagementPanelProps) {
    const [subTab, setSubTab] = useState<SubTab>('snapshots');

    // World snapshots state
    const [snapshots, setSnapshots] = useState<SnapshotItem[]>([]);
    const [stats, setStats] = useState<SnapshotStats | null>(null);
    const [currentVersion, setCurrentVersion] = useState<string>('');
    const [snapLoading, setSnapLoading] = useState(false);
    const [snapBusy, setSnapBusy] = useState(false);
    const [snapNote, setSnapNote] = useState('');
    const [snapNotice, setSnapNotice] = useState<string | null>(null);
    const [snapError, setSnapError] = useState<string | null>(null);
    const [restoreTarget, setRestoreTarget] = useState<SnapshotItem | null>(null);
    const [restoreChecked, setRestoreChecked] = useState(false);

    // Persona memory.db backups state
    const [personas, setPersonas] = useState<PersonaBackupSummary[]>([]);
    const [selectedPersonaId, setSelectedPersonaId] = useState<string>('');
    const [loadedPersonaId, setLoadedPersonaId] = useState<string>('');
    const [personaBackups, setPersonaBackups] = useState<PersonaBackupEntry[]>([]);
    const [personaHasDb, setPersonaHasDb] = useState(false);
    const [personaDbSize, setPersonaDbSize] = useState(0);
    const [personaHasRdiff, setPersonaHasRdiff] = useState(false);
    const [personaLoading, setPersonaLoading] = useState(false);
    const [personaBusy, setPersonaBusy] = useState(false);
    const [personaNotice, setPersonaNotice] = useState<string | null>(null);
    const [personaError, setPersonaError] = useState<string | null>(null);
    const [personaRestoreTarget, setPersonaRestoreTarget] = useState<PersonaRestoreTarget | null>(null);
    const [personaRestoreChecked, setPersonaRestoreChecked] = useState(false);

    // Guards against out-of-order / stale async responses when switching personas
    const selectedPersonaIdRef = useRef<string>('');
    const snapReqGenRef = useRef(0);
    const personasListReqGenRef = useRef(0);
    const personaDetailReqGenRef = useRef(0);
    const personaDetailAbortRef = useRef<AbortController | null>(null);

    selectedPersonaIdRef.current = selectedPersonaId;

    useEffect(() => {
        return () => {
            personaDetailAbortRef.current?.abort();
        };
    }, []);

    const loadSnapshots = useCallback(async () => {
        const reqGen = ++snapReqGenRef.current;
        setSnapLoading(true);
        setSnapError(null);
        try {
            const res = await apiFetch('/api/backups/snapshots');
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.loadSnapshotsFailed')));
            }
            const data = await res.json();
            if (reqGen !== snapReqGenRef.current) return;
            setSnapshots(Array.isArray(data.snapshots) ? data.snapshots : []);
            setStats(data.stats || null);
            setCurrentVersion(typeof data.saiverse_version === 'string' ? data.saiverse_version : '');
        } catch (err) {
            if (reqGen !== snapReqGenRef.current) return;
            setSnapError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.loadSnapshotsFailed'));
        } finally {
            if (reqGen === snapReqGenRef.current) {
                setSnapLoading(false);
            }
        }
    }, []);

    const loadPersonaDetail = useCallback(async (personaId: string) => {
        personaDetailAbortRef.current?.abort();
        const reqGen = ++personaDetailReqGenRef.current;
        setLoadedPersonaId('');
        setPersonaBackups([]);
        setPersonaHasDb(false);
        setPersonaDbSize(0);
        setPersonaHasRdiff(false);
        if (!personaId) {
            setPersonaLoading(false);
            return;
        }
        const controller = new AbortController();
        personaDetailAbortRef.current = controller;
        setPersonaLoading(true);
        setPersonaError(null);
        try {
            const res = await apiFetch(
                `/api/backups/personas/${encodeURIComponent(personaId)}`,
                { signal: controller.signal }
            );
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.loadPersonaFailed')));
            }
            const data = await res.json();
            if (
                reqGen !== personaDetailReqGenRef.current
                || selectedPersonaIdRef.current !== personaId
            ) {
                return;
            }
            setLoadedPersonaId(personaId);
            setPersonaBackups(Array.isArray(data.backups) ? data.backups : []);
            setPersonaHasDb(Boolean(data.has_memory_db));
            setPersonaDbSize(typeof data.memory_db_size_bytes === 'number' ? data.memory_db_size_bytes : 0);
            setPersonaHasRdiff(Boolean(data.has_rdiff_backup));
        } catch (err) {
            if (
                controller.signal.aborted
                || reqGen !== personaDetailReqGenRef.current
                || selectedPersonaIdRef.current !== personaId
            ) {
                return;
            }
            setPersonaError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.loadPersonaFailed'));
        } finally {
            if (reqGen === personaDetailReqGenRef.current) {
                setPersonaLoading(false);
            }
        }
    }, []);

    const loadPersonas = useCallback(async () => {
        const reqGen = ++personasListReqGenRef.current;
        setPersonaLoading(true);
        setPersonaError(null);
        try {
            const res = await apiFetch('/api/backups/personas');
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.loadPersonaFailed')));
            }
            const data = await res.json();
            if (reqGen !== personasListReqGenRef.current) return;
            const list: PersonaBackupSummary[] = Array.isArray(data.personas) ? data.personas : [];
            setPersonas(list);
            setSelectedPersonaId(prev => {
                if (prev && list.some(p => p.persona_id === prev)) return prev;
                return list[0]?.persona_id || '';
            });
        } catch (err) {
            if (reqGen !== personasListReqGenRef.current) return;
            setPersonaError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.loadPersonaFailed'));
        } finally {
            if (reqGen === personasListReqGenRef.current) {
                setPersonaLoading(false);
            }
        }
    }, []);

    useEffect(() => {
        loadSnapshots();
    }, [loadSnapshots]);

    useEffect(() => {
        if (subTab === 'personas') {
            loadPersonas();
        }
    }, [subTab, loadPersonas]);

    useEffect(() => {
        if (subTab === 'personas' && selectedPersonaId) {
            loadPersonaDetail(selectedPersonaId);
        }
    }, [subTab, selectedPersonaId, loadPersonaDetail]);

    // ---- World snapshot actions ----

    const handleCreateSnapshot = async () => {
        if (snapBusy) return;
        if (!window.confirm(uiText('components.BackupManagementPanel.createSnapshotConfirm'))) {
            return;
        }
        setSnapBusy(true);
        setSnapError(null);
        setSnapNotice(null);
        try {
            const res = await apiFetch('/api/backups/snapshots', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ note: snapNote.trim() || undefined }),
            });
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.createSnapshotFailed')));
            }
            announceUpdateStarted(
                '',
                uiText('components.BackupManagementPanel.bannerCreatingSnapshot'),
                uiText('components.BackupManagementPanel.toastSnapshotCreated'),
            );
            onOperationStarted?.();
        } catch (err) {
            setSnapError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.createSnapshotFailed'));
            setSnapBusy(false);
        }
    };

    const handleDeleteSnapshot = async (item: SnapshotItem) => {
        if (snapBusy) return;
        if (!window.confirm(uiText('components.BackupManagementPanel.deleteSnapshotConfirm', { p1: item.name }))) {
            return;
        }
        setSnapBusy(true);
        setSnapError(null);
        setSnapNotice(null);
        try {
            const res = await apiFetch(`/api/backups/snapshots/${encodeURIComponent(item.name)}`, {
                method: 'DELETE',
            });
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.deleteSnapshotFailed')));
            }
            const data = await res.json();
            const reclaimed = data?.deleted?.reclaimed_bytes ?? 0;
            setSnapNotice(
                uiText('components.BackupManagementPanel.deleteSnapshotDone', {
                    p1: item.name,
                    p2: formatBytes(reclaimed),
                })
            );
            await loadSnapshots();
        } catch (err) {
            setSnapError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.deleteSnapshotFailed'));
        } finally {
            setSnapBusy(false);
        }
    };

    const handleRunGc = async () => {
        if (snapBusy) return;
        setSnapBusy(true);
        setSnapError(null);
        setSnapNotice(null);
        try {
            const res = await apiFetch('/api/backups/snapshots/gc', { method: 'POST' });
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.gcFailed')));
            }
            const data = await res.json();
            const reclaimed = data?.gc?.reclaimed_bytes ?? 0;
            const removedCount = (data?.gc?.removed_objects ?? 0) + (data?.gc?.removed_temp_files ?? 0);
            setSnapNotice(
                uiText('components.BackupManagementPanel.gcDone', {
                    p1: formatBytes(reclaimed),
                    p2: String(removedCount),
                })
            );
            await loadSnapshots();
        } catch (err) {
            setSnapError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.gcFailed'));
        } finally {
            setSnapBusy(false);
        }
    };

    const handleConfirmRestoreSnapshot = async () => {
        if (!restoreTarget || !restoreChecked || snapBusy) return;
        setSnapBusy(true);
        setSnapError(null);
        try {
            const res = await apiFetch(
                `/api/backups/snapshots/${encodeURIComponent(restoreTarget.name)}/restore`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm: true }),
                }
            );
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.restoreSnapshotFailed')));
            }
            setRestoreTarget(null);
            setRestoreChecked(false);
            announceUpdateStarted(
                '',
                uiText('components.BackupManagementPanel.bannerRestoringSnapshot'),
                uiText('components.BackupManagementPanel.toastSnapshotRestored'),
            );
            onOperationStarted?.();
        } catch (err) {
            setSnapError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.restoreSnapshotFailed'));
            setSnapBusy(false);
        }
    };

    // ---- Persona memory.db actions ----

    const selectedPersona = personas.find(p => p.persona_id === selectedPersonaId) || null;
    const detailMatchesSelection = Boolean(selectedPersonaId && loadedPersonaId === selectedPersonaId);

    const handleCreatePersonaBackup = async () => {
        const targetPersonaId = selectedPersonaId;
        const targetDisplayName = selectedPersona?.display_name || targetPersonaId;
        if (!targetPersonaId || !detailMatchesSelection || personaBusy) return;
        setPersonaBusy(true);
        setPersonaError(null);
        setPersonaNotice(null);
        try {
            const res = await apiFetch(`/api/backups/personas/${encodeURIComponent(targetPersonaId)}`, {
                method: 'POST',
            });
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.createPersonaBackupFailed')));
            }
            const data = await res.json();
            if (selectedPersonaIdRef.current === targetPersonaId && Array.isArray(data.backups)) {
                setPersonaBackups(data.backups);
            }
            setPersonaNotice(
                uiText('components.BackupManagementPanel.createPersonaBackupDone', {
                    p1: targetDisplayName,
                })
            );
            await loadPersonas();
        } catch (err) {
            setPersonaError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.createPersonaBackupFailed'));
        } finally {
            setPersonaBusy(false);
        }
    };

    const handleDeletePersonaBackup = async (entry: PersonaBackupEntry) => {
        const targetPersonaId = selectedPersonaId;
        if (!targetPersonaId || !detailMatchesSelection || personaBusy) return;
        if (
            !window.confirm(
                uiText('components.BackupManagementPanel.deletePersonaBackupConfirm', {
                    p1: entry.filename,
                })
            )
        ) {
            return;
        }
        setPersonaBusy(true);
        setPersonaError(null);
        setPersonaNotice(null);
        try {
            const res = await apiFetch(
                `/api/backups/personas/${encodeURIComponent(targetPersonaId)}/${encodeURIComponent(entry.filename)}`,
                { method: 'DELETE' }
            );
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.deletePersonaBackupFailed')));
            }
            const data = await res.json();
            if (selectedPersonaIdRef.current === targetPersonaId && Array.isArray(data.backups)) {
                setPersonaBackups(data.backups);
            }
            setPersonaNotice(
                uiText('components.BackupManagementPanel.deletePersonaBackupDone', {
                    p1: entry.filename,
                    p2: formatBytes(data.reclaimed_bytes ?? 0),
                })
            );
            await loadPersonas();
        } catch (err) {
            setPersonaError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.deletePersonaBackupFailed'));
        } finally {
            setPersonaBusy(false);
        }
    };

    const handleConfirmRestorePersona = async () => {
        if (!personaRestoreTarget || !personaRestoreChecked || personaBusy) return;
        const { personaId, displayName, entry } = personaRestoreTarget;
        setPersonaBusy(true);
        setPersonaError(null);
        try {
            const res = await apiFetch(
                `/api/backups/personas/${encodeURIComponent(personaId)}/restore`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        filename: entry.filename,
                        confirm: true,
                    }),
                }
            );
            if (!res.ok) {
                throw new Error(await readErrorDetail(res, uiText('components.BackupManagementPanel.restorePersonaFailed')));
            }
            setPersonaRestoreTarget(null);
            setPersonaRestoreChecked(false);
            announceUpdateStarted(
                '',
                uiText('components.BackupManagementPanel.bannerRestoringPersona', {
                    p1: displayName,
                }),
                uiText('components.BackupManagementPanel.toastPersonaRestored', {
                    p1: displayName,
                }),
            );
            onOperationStarted?.();
        } catch (err) {
            setPersonaError(err instanceof Error ? err.message : uiText('components.BackupManagementPanel.restorePersonaFailed'));
            setPersonaBusy(false);
        }
    };

    const reclaimableBytes = (stats?.orphan_bytes || 0) + (stats?.temp_bytes || 0);
    const reclaimableCount = (stats?.orphan_object_count || 0) + (stats?.temp_file_count || 0);
    const latestUncompressedBytes =
        snapshots.find(
            item => item.readable && typeof item.total_bytes_uncompressed === 'number'
        )?.total_bytes_uncompressed ?? null;
    const casSnapshotCount = stats?.cas_snapshot_count ?? 0;
    const gcPausedByCorruption = (stats?.corrupted_manifests.length ?? 0) > 0;

    return (
        <div className={styles.container}>
            <div className={styles.subTabBar}>
                <button
                    type="button"
                    className={`${styles.subTab} ${subTab === 'snapshots' ? styles.subTabActive : ''}`}
                    onClick={() => setSubTab('snapshots')}
                >
                    <HardDrive size={15} />
                    {uiText('components.BackupManagementPanel.subTabSnapshots')}
                </button>
                <button
                    type="button"
                    className={`${styles.subTab} ${subTab === 'personas' ? styles.subTabActive : ''}`}
                    onClick={() => setSubTab('personas')}
                >
                    <Database size={15} />
                    {uiText('components.BackupManagementPanel.subTabPersonas')}
                </button>
            </div>

            {subTab === 'snapshots' && (
                <>
                    <div className={styles.sectionHeader}>
                        <h3>
                            <HardDrive size={17} />
                            {uiText('components.BackupManagementPanel.snapshotsTitle')}
                        </h3>
                        <div className={styles.actions}>
                            <button
                                type="button"
                                className={styles.btnSecondary}
                                onClick={loadSnapshots}
                                disabled={snapLoading || snapBusy}
                            >
                                <RefreshCw size={14} />
                                {uiText('components.BackupManagementPanel.refresh')}
                            </button>
                        </div>
                    </div>

                    <p className={styles.intro}>
                        {uiText('components.BackupManagementPanel.snapshotsIntro')}
                    </p>

                    {stats && (
                        <div className={styles.statsCard}>
                            <div className={styles.statsGroup}>
                                <span className={styles.statItem}>
                                    <span className={styles.statLabel}>
                                        {uiText('components.BackupManagementPanel.statCount')}
                                    </span>
                                    <span className={styles.statValue}>{snapshots.length}</span>
                                </span>
                                <span className={styles.statItem}>
                                    <span className={styles.statLabel}>
                                        {uiText('components.BackupManagementPanel.statTotalDisk')}
                                    </span>
                                    <span className={styles.statValue}>
                                        {formatBytes(stats.total_disk_bytes)}
                                    </span>
                                </span>
                                {(casSnapshotCount > 0 || stats.cas_store_bytes > 0) && (
                                    <span className={styles.statItem}>
                                        <span className={styles.statLabel}>
                                            {uiText('components.BackupManagementPanel.statCasStore')}
                                        </span>
                                        <span className={styles.statValue}>
                                            {formatBytes(stats.cas_store_bytes)} ({casSnapshotCount})
                                        </span>
                                    </span>
                                )}
                                {stats.legacy_zip_count > 0 && (
                                    <span className={styles.statItem}>
                                        <span className={styles.statLabel}>
                                            {uiText('components.BackupManagementPanel.statLegacyZip')}
                                        </span>
                                        <span className={styles.statValue}>
                                            {formatBytes(stats.legacy_zip_bytes)} ({stats.legacy_zip_count})
                                        </span>
                                    </span>
                                )}
                                {latestUncompressedBytes != null && (
                                    <span className={styles.statItem}>
                                        <span className={styles.statLabel}>
                                            {uiText('components.BackupManagementPanel.statLatestUncompressed')}
                                        </span>
                                        <span className={styles.statValue}>
                                            {formatBytes(latestUncompressedBytes)}
                                        </span>
                                    </span>
                                )}
                            </div>
                            {reclaimableCount > 0 && (
                                <button
                                    type="button"
                                    className={styles.btnSecondary}
                                    onClick={handleRunGc}
                                    disabled={snapBusy}
                                >
                                    <Sparkles size={14} />
                                    {uiText('components.BackupManagementPanel.gcButton', {
                                        p1: formatBytes(reclaimableBytes),
                                    })}
                                </button>
                            )}
                        </div>
                    )}

                    {stats && stats.corrupted_manifests.length > 0 && (
                        <div className={styles.warningBox}>
                            {uiText('components.BackupManagementPanel.corruptedManifestsWarning', {
                                p1: stats.corrupted_manifests.join(', '),
                            })}
                        </div>
                    )}

                    <div className={styles.createCard}>
                        <input
                            type="text"
                            className={styles.noteInput}
                            value={snapNote}
                            onChange={e => setSnapNote(e.target.value)}
                            placeholder={uiText('components.BackupManagementPanel.notePlaceholder')}
                            disabled={snapBusy}
                        />
                        <button
                            type="button"
                            className={styles.btnPrimary}
                            onClick={handleCreateSnapshot}
                            disabled={snapBusy}
                        >
                            <Plus size={15} />
                            {uiText('components.BackupManagementPanel.createSnapshotButton')}
                        </button>
                    </div>

                    {snapNotice && <div className={styles.notice}>{snapNotice}</div>}
                    {snapError && <div className={styles.errorBox}>{snapError}</div>}

                    {snapLoading ? (
                        <div className={styles.emptyState}>
                            {uiText('components.BackupManagementPanel.loading')}
                        </div>
                    ) : snapshots.length === 0 ? (
                        <div className={styles.emptyState}>
                            {uiText('components.BackupManagementPanel.noSnapshots')}
                        </div>
                    ) : (
                        <div className={styles.list}>
                            {snapshots.map(item => (
                                <div key={`${item.format}:${item.name}`} className={styles.itemCard}>
                                    <div className={styles.itemMain}>
                                        <div className={styles.itemTitleRow}>
                                            <span className={styles.itemName}>{item.name}</span>
                                            <span
                                                className={`${styles.badge} ${item.format === 'cas' ? styles.badgeCas : ''}`}
                                            >
                                                {item.format.toUpperCase()}
                                            </span>
                                            {item.saiverse_version && (
                                                <span className={styles.badge}>
                                                    v{item.saiverse_version}
                                                </span>
                                            )}
                                        </div>
                                        <div className={styles.itemMeta}>
                                            <span>{formatTimestamp(item.created_at)}</span>
                                            {item.file_count != null && (
                                                <span>
                                                    {uiText('components.BackupManagementPanel.fileCount', {
                                                        p1: String(item.file_count),
                                                    })}
                                                </span>
                                            )}
                                            <span>
                                                {item.unique_bytes != null
                                                    ? uiText('components.BackupManagementPanel.reclaimOnDelete', {
                                                          p1: formatBytes(item.unique_bytes),
                                                      })
                                                    : item.format === 'cas' && gcPausedByCorruption
                                                        ? uiText(
                                                              'components.BackupManagementPanel.reclaimUnavailableGcPaused'
                                                          )
                                                        : uiText('components.BackupManagementPanel.reclaimOnDelete', {
                                                              p1: '-',
                                                          })}
                                            </span>
                                        </div>
                                        {item.note && <div className={styles.itemNote}>{item.note}</div>}
                                    </div>
                                    <div className={styles.itemActions}>
                                        <button
                                            type="button"
                                            className={styles.btnSecondary}
                                            onClick={() => {
                                                setRestoreTarget(item);
                                                setRestoreChecked(false);
                                            }}
                                            disabled={snapBusy || !item.readable}
                                        >
                                            <RotateCcw size={14} />
                                            {uiText('components.BackupManagementPanel.restoreButton')}
                                        </button>
                                        <button
                                            type="button"
                                            className={styles.btnDanger}
                                            onClick={() => handleDeleteSnapshot(item)}
                                            disabled={snapBusy}
                                        >
                                            <Trash2 size={14} />
                                            {uiText('components.BackupManagementPanel.deleteButton')}
                                        </button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </>
            )}

            {subTab === 'personas' && (
                <>
                    <div className={styles.sectionHeader}>
                        <h3>
                            <Database size={17} />
                            {uiText('components.BackupManagementPanel.personasTitle')}
                        </h3>
                        <div className={styles.actions}>
                            <button
                                type="button"
                                className={styles.btnSecondary}
                                onClick={async () => {
                                    await loadPersonas();
                                    if (selectedPersonaId) {
                                        await loadPersonaDetail(selectedPersonaId);
                                    }
                                }}
                                disabled={personaLoading || personaBusy}
                            >
                                <RefreshCw size={14} />
                                {uiText('components.BackupManagementPanel.refresh')}
                            </button>
                        </div>
                    </div>

                    <p className={styles.intro}>
                        {uiText('components.BackupManagementPanel.personasIntro')}
                    </p>

                    <div className={styles.createCard}>
                        <select
                            className={styles.selectInput}
                            value={selectedPersonaId}
                            onChange={e => {
                                setSelectedPersonaId(e.target.value);
                                setPersonaRestoreTarget(null);
                                setPersonaRestoreChecked(false);
                                setPersonaNotice(null);
                                setPersonaError(null);
                            }}
                            disabled={personaBusy || personas.length === 0}
                        >
                            {personas.length === 0 ? (
                                <option value="">
                                    {uiText('components.BackupManagementPanel.noPersonas')}
                                </option>
                            ) : (
                                personas.map(p => (
                                    <option key={p.persona_id} value={p.persona_id}>
                                        {p.display_name} ({p.persona_id}) —{' '}
                                        {uiText('components.BackupManagementPanel.personaBackupCount', {
                                            p1: String(p.backup_count),
                                        })}
                                        {!p.registered
                                            ? ` [${uiText('components.BackupManagementPanel.unregisteredTag')}]`
                                            : ''}
                                    </option>
                                ))
                            )}
                        </select>
                        <button
                            type="button"
                            className={styles.btnPrimary}
                            onClick={handleCreatePersonaBackup}
                            disabled={personaBusy || personaLoading || !detailMatchesSelection || !personaHasDb}
                        >
                            <Plus size={15} />
                            {uiText('components.BackupManagementPanel.createPersonaBackupButton')}
                        </button>
                    </div>

                    {detailMatchesSelection && (
                        <div className={styles.statsCard}>
                            <div className={styles.statsGroup}>
                                <span className={styles.statItem}>
                                    <span className={styles.statLabel}>
                                        {uiText('components.BackupManagementPanel.currentMemoryDbSize')}
                                    </span>
                                    <span className={styles.statValue}>
                                        {personaHasDb
                                            ? formatBytes(personaDbSize)
                                            : uiText('components.BackupManagementPanel.noMemoryDb')}
                                    </span>
                                </span>
                                <span className={styles.statItem}>
                                    <span className={styles.statLabel}>
                                        {uiText('components.BackupManagementPanel.statCount')}
                                    </span>
                                    <span className={styles.statValue}>{personaBackups.length}</span>
                                </span>
                            </div>
                        </div>
                    )}

                    {detailMatchesSelection && personaHasRdiff && (
                        <div className={styles.notice}>
                            {uiText('components.BackupManagementPanel.rdiffNotice')}
                        </div>
                    )}

                    {personaNotice && <div className={styles.notice}>{personaNotice}</div>}
                    {personaError && <div className={styles.errorBox}>{personaError}</div>}

                    {personaLoading || (Boolean(selectedPersonaId) && !detailMatchesSelection) ? (
                        <div className={styles.emptyState}>
                            {uiText('components.BackupManagementPanel.loading')}
                        </div>
                    ) : personaBackups.length === 0 ? (
                        <div className={styles.emptyState}>
                            {uiText('components.BackupManagementPanel.noPersonaBackups')}
                        </div>
                    ) : (
                        <div className={styles.list}>
                            {personaBackups.map(entry => (
                                <div key={`${loadedPersonaId}:${entry.filename}`} className={styles.itemCard}>
                                    <div className={styles.itemMain}>
                                        <div className={styles.itemTitleRow}>
                                            <span className={styles.itemName}>
                                                {formatTimestamp(entry.created_at)}
                                            </span>
                                            {entry.kind === 'pre_restore' && (
                                                <span className={`${styles.badge} ${styles.badgePreRestore}`}>
                                                    {uiText('components.BackupManagementPanel.preRestoreBadge')}
                                                </span>
                                            )}
                                        </div>
                                        <div className={styles.itemMeta}>
                                            <span>{formatBytes(entry.size_bytes)}</span>
                                            <span>{entry.filename}</span>
                                        </div>
                                    </div>
                                    <div className={styles.itemActions}>
                                        <button
                                            type="button"
                                            className={styles.btnSecondary}
                                            onClick={() => {
                                                if (!detailMatchesSelection) return;
                                                setPersonaRestoreTarget({
                                                    personaId: loadedPersonaId,
                                                    displayName: selectedPersona?.display_name || loadedPersonaId,
                                                    entry,
                                                });
                                                setPersonaRestoreChecked(false);
                                            }}
                                            disabled={personaBusy || !detailMatchesSelection}
                                        >
                                            <RotateCcw size={14} />
                                            {uiText('components.BackupManagementPanel.restoreButton')}
                                        </button>
                                        <button
                                            type="button"
                                            className={styles.btnDanger}
                                            onClick={() => handleDeletePersonaBackup(entry)}
                                            disabled={personaBusy || !detailMatchesSelection}
                                        >
                                            <Trash2 size={14} />
                                            {uiText('components.BackupManagementPanel.deleteButton')}
                                        </button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </>
            )}

            {/* World snapshot restore confirmation modal */}
            {restoreTarget && (
                <ModalOverlay
                    onClose={() => {
                        if (!snapBusy) setRestoreTarget(null);
                    }}
                    className={styles.confirmOverlay}
                >
                    <div className={styles.confirmModal} onClick={e => e.stopPropagation()}>
                        <h4 className={styles.confirmTitle}>
                            <AlertTriangle size={18} />
                            {uiText('components.BackupManagementPanel.restoreSnapshotModalTitle')}
                        </h4>
                        <div className={styles.confirmDetails}>
                            <div>
                                <strong>{restoreTarget.name}</strong> ({restoreTarget.format.toUpperCase()})
                            </div>
                            <div>
                                {formatTimestamp(restoreTarget.created_at)}
                                {restoreTarget.saiverse_version
                                    ? ` / v${restoreTarget.saiverse_version}`
                                    : ''}
                                {currentVersion && restoreTarget.saiverse_version && restoreTarget.saiverse_version !== currentVersion
                                    ? ` → ${uiText('components.BackupManagementPanel.currentCodeVersion', { p1: currentVersion })}`
                                    : ''}
                            </div>
                            {restoreTarget.note && <div>{restoreTarget.note}</div>}
                        </div>
                        <ul className={styles.confirmBullets}>
                            <li>{uiText('components.BackupManagementPanel.restoreSnapshotNote1')}</li>
                            <li>{uiText('components.BackupManagementPanel.restoreSnapshotNote2')}</li>
                            <li>{uiText('components.BackupManagementPanel.restoreSnapshotNote3')}</li>
                        </ul>
                        <label className={styles.confirmCheckLabel}>
                            <input
                                type="checkbox"
                                checked={restoreChecked}
                                onChange={e => setRestoreChecked(e.target.checked)}
                                disabled={snapBusy}
                            />
                            <span>{uiText('components.BackupManagementPanel.restoreConfirmCheckbox')}</span>
                        </label>
                        <div className={styles.confirmFooter}>
                            <button
                                type="button"
                                className={styles.btnSecondary}
                                onClick={() => setRestoreTarget(null)}
                                disabled={snapBusy}
                            >
                                {uiText('components.BackupManagementPanel.cancel')}
                            </button>
                            <button
                                type="button"
                                className={styles.btnDangerSolid}
                                onClick={handleConfirmRestoreSnapshot}
                                disabled={!restoreChecked || snapBusy}
                            >
                                <RotateCcw size={14} />
                                {uiText('components.BackupManagementPanel.executeRestore')}
                            </button>
                        </div>
                    </div>
                </ModalOverlay>
            )}

            {/* Persona memory.db restore confirmation modal */}
            {personaRestoreTarget && (
                <ModalOverlay
                    onClose={() => {
                        if (!personaBusy) setPersonaRestoreTarget(null);
                    }}
                    className={styles.confirmOverlay}
                >
                    <div className={styles.confirmModal} onClick={e => e.stopPropagation()}>
                        <h4 className={styles.confirmTitle}>
                            <AlertTriangle size={18} />
                            {uiText('components.BackupManagementPanel.restorePersonaModalTitle')}
                        </h4>
                        <div className={styles.confirmDetails}>
                            <div>
                                <strong>{personaRestoreTarget.displayName}</strong> ({personaRestoreTarget.personaId})
                            </div>
                            <div>
                                {formatTimestamp(personaRestoreTarget.entry.created_at)} /{' '}
                                {formatBytes(personaRestoreTarget.entry.size_bytes)}
                            </div>
                            <div>{personaRestoreTarget.entry.filename}</div>
                        </div>
                        <ul className={styles.confirmBullets}>
                            <li>{uiText('components.BackupManagementPanel.restorePersonaNote1')}</li>
                            <li>{uiText('components.BackupManagementPanel.restorePersonaNote2')}</li>
                            <li>{uiText('components.BackupManagementPanel.restorePersonaNote3')}</li>
                        </ul>
                        <label className={styles.confirmCheckLabel}>
                            <input
                                type="checkbox"
                                checked={personaRestoreChecked}
                                onChange={e => setPersonaRestoreChecked(e.target.checked)}
                                disabled={personaBusy}
                            />
                            <span>{uiText('components.BackupManagementPanel.restoreConfirmCheckbox')}</span>
                        </label>
                        <div className={styles.confirmFooter}>
                            <button
                                type="button"
                                className={styles.btnSecondary}
                                onClick={() => setPersonaRestoreTarget(null)}
                                disabled={personaBusy}
                            >
                                {uiText('components.BackupManagementPanel.cancel')}
                            </button>
                            <button
                                type="button"
                                className={styles.btnDangerSolid}
                                onClick={handleConfirmRestorePersona}
                                disabled={!personaRestoreChecked || personaBusy}
                            >
                                <RotateCcw size={14} />
                                {uiText('components.BackupManagementPanel.executeRestore')}
                            </button>
                        </div>
                    </div>
                </ModalOverlay>
            )}
        </div>
    );
}
