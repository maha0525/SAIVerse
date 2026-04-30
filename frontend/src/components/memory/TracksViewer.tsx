'use client';
import React, { useState, useEffect, useCallback } from 'react';
import { Loader2, GitBranch, ChevronDown, ChevronUp, RefreshCw, PauseCircle } from 'lucide-react';
import slStyles from './TracksViewer.module.css';

interface TrackItem {
    track_id: string;
    persona_id: string;
    title: string | null;
    track_type: string;
    is_persistent: boolean;
    output_target: string;
    status: string;
    is_forgotten: boolean;
    intent: string | null;
    track_metadata: Record<string, unknown> | null;
    pause_summary: string | null;
    pause_summary_updated_at: number | null;
    last_active_at: number | null;
    waiting_for: string | null;
    waiting_timeout_at: number | null;
    created_at: number | null;
    completed_at: number | null;
    aborted_at: number | null;
}

interface TracksStatusCount {
    status: string;
    count: number;
}

interface TracksResponse {
    items: TrackItem[];
    total: number;
    status_counts: TracksStatusCount[];
}

interface Props {
    personaId: string;
}

const STATUS_FILTER_OPTIONS = [
    { value: '', label: '全状態' },
    { value: 'running', label: 'running' },
    { value: 'alert', label: 'alert' },
    { value: 'pending', label: 'pending' },
    { value: 'waiting', label: 'waiting' },
    { value: 'unstarted', label: 'unstarted' },
    { value: 'completed', label: 'completed' },
    { value: 'aborted', label: 'aborted' },
];

function formatTimestamp(epoch: number | null | undefined): string {
    if (epoch === null || epoch === undefined) return '—';
    try {
        return new Date(epoch * 1000).toLocaleString('ja-JP', { hour12: false });
    } catch {
        return String(epoch);
    }
}

function shortId(id: string): string {
    return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

export default function TracksViewer({ personaId }: Props) {
    const [data, setData] = useState<TracksResponse | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [statusFilter, setStatusFilter] = useState<string>('');
    const [includeForgotten, setIncludeForgotten] = useState<boolean>(false);
    const [expanded, setExpanded] = useState<Set<string>>(new Set());

    const load = useCallback(async () => {
        setIsLoading(true);
        setError(null);
        try {
            const params = new URLSearchParams();
            if (statusFilter) params.append('status', statusFilter);
            if (includeForgotten) params.append('include_forgotten', 'true');
            const res = await fetch(
                `/api/people/${personaId}/tracks?${params.toString()}`
            );
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            setData((await res.json()) as TracksResponse);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setIsLoading(false);
        }
    }, [personaId, statusFilter, includeForgotten]);

    useEffect(() => { load(); }, [load]);

    const toggleExpand = (id: string) => {
        setExpanded(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    return (
        <div className={slStyles.rootContainer}>
            {/* Filter bar */}
            <div className={slStyles.filterBar}>
                <div className={slStyles.filterGroup}>
                    <label className={slStyles.filterLabel}>Status:</label>
                    {STATUS_FILTER_OPTIONS.map(opt => (
                        <button
                            key={opt.value}
                            className={`${slStyles.chip} ${statusFilter === opt.value ? slStyles.chipActive : ''}`}
                            onClick={() => setStatusFilter(opt.value)}
                        >
                            {opt.label}
                        </button>
                    ))}
                </div>
                <div className={slStyles.filterGroup}>
                    <label className={slStyles.toggleLabel}>
                        <input
                            type="checkbox"
                            checked={includeForgotten}
                            onChange={e => setIncludeForgotten(e.target.checked)}
                        />
                        forgotten を含める
                    </label>
                    <button
                        className={slStyles.refreshBtn}
                        onClick={load}
                        disabled={isLoading}
                        title="再読み込み"
                    >
                        {isLoading ? <Loader2 size={14} className={slStyles.spin} /> : <RefreshCw size={14} />}
                    </button>
                </div>
            </div>

            {/* Status counts summary */}
            <div className={slStyles.summary}>
                <div className={slStyles.summaryHeader}>
                    <GitBranch size={16} />
                    <span>Track ステータス内訳</span>
                </div>
                <div className={slStyles.summaryRow}>
                    {data?.status_counts.map(s => (
                        <div
                            key={s.status}
                            className={`${slStyles.statusPill} ${slStyles[`status_${s.status}`] || ''}`}
                            onClick={() => setStatusFilter(s.status === statusFilter ? '' : s.status)}
                        >
                            <span className={slStyles.statusPillLabel}>{s.status}</span>
                            <span className={slStyles.statusPillCount}>{s.count}</span>
                        </div>
                    ))}
                </div>
            </div>

            {/* Items list */}
            <div className={slStyles.itemsContainer}>
                {error && <div className={slStyles.errorBox}>Error: {error}</div>}
                {!error && data && (
                    <>
                        <div className={slStyles.itemsHeader}>
                            <span>表示中: {data.total} 件</span>
                            {statusFilter && (
                                <span className={slStyles.filterHint}>
                                    (フィルタ: status={statusFilter})
                                </span>
                            )}
                        </div>
                        {data.items.length === 0 && (
                            <div className={slStyles.emptyBox}>
                                該当 Track なし。
                            </div>
                        )}
                        <div className={slStyles.itemsList}>
                            {data.items.map(t => (
                                <TrackCard
                                    key={t.track_id}
                                    personaId={personaId}
                                    track={t}
                                    expanded={expanded.has(t.track_id)}
                                    onToggle={() => toggleExpand(t.track_id)}
                                    onChanged={load}
                                />
                            ))}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

interface TrackCardProps {
    personaId: string;
    track: TrackItem;
    expanded: boolean;
    onToggle: () => void;
    onChanged: () => void;
}

function TrackCard({ personaId, track, expanded, onToggle, onChanged }: TrackCardProps) {
    const meta = track.track_metadata || {};
    const entryLineRole = (meta.entry_line_role as string | undefined) || null;
    const [actionLoading, setActionLoading] = useState(false);
    const [actionError, setActionError] = useState<string | null>(null);

    const canPause = track.status === 'running' || track.status === 'alert';

    const pauseTrack = async () => {
        setActionLoading(true);
        setActionError(null);
        try {
            const res = await fetch(
                `/api/people/${personaId}/tracks/${track.track_id}/pause`,
                { method: 'POST' },
            );
            if (!res.ok) {
                const detail = await res.json().catch(() => null);
                throw new Error(
                    (detail && typeof detail.detail === 'string')
                        ? detail.detail
                        : `HTTP ${res.status}`,
                );
            }
            onChanged();
        } catch (e) {
            setActionError(e instanceof Error ? e.message : String(e));
        } finally {
            setActionLoading(false);
        }
    };

    return (
        <div className={slStyles.trackCard}>
            <div className={slStyles.trackHeader} onClick={onToggle}>
                <span className={`${slStyles.statusBadge} ${slStyles[`status_${track.status}`] || ''}`}>
                    {track.status}
                </span>
                <span className={slStyles.typeBadge}>{track.track_type}</span>
                {track.is_persistent && (
                    <span className={slStyles.persistentBadge}>persistent</span>
                )}
                {track.is_forgotten && (
                    <span className={slStyles.forgottenBadge}>forgotten</span>
                )}
                {entryLineRole && (
                    <span className={`${slStyles.entryLineBadge} ${slStyles[`line_${entryLineRole}`] || ''}`}>
                        {entryLineRole}
                    </span>
                )}
                <span className={slStyles.trackTitle}>
                    {track.title || '(無題)'}
                </span>
                <span className={slStyles.trackId}>{shortId(track.track_id)}</span>
                <span className={slStyles.trackTime}>
                    {track.last_active_at ? formatTimestamp(track.last_active_at) : '—'}
                </span>
                <span className={slStyles.expandIcon}>
                    {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
            </div>

            {expanded && (
                <div className={slStyles.trackDetails}>
                    <DetailRow label="track_id" value={track.track_id} />
                    <DetailRow label="output_target" value={track.output_target} />
                    {track.intent && <DetailRow label="intent" value={track.intent} pre />}
                    {track.pause_summary && (
                        <DetailRow
                            label="pause_summary"
                            value={`${track.pause_summary} (updated: ${formatTimestamp(track.pause_summary_updated_at)})`}
                            pre
                        />
                    )}
                    {track.waiting_for && (
                        <DetailRow
                            label="waiting_for"
                            value={`${track.waiting_for}${track.waiting_timeout_at ? ` (timeout: ${formatTimestamp(track.waiting_timeout_at)})` : ''}`}
                        />
                    )}
                    {track.created_at && (
                        <DetailRow label="created_at" value={formatTimestamp(track.created_at)} />
                    )}
                    {track.completed_at && (
                        <DetailRow label="completed_at" value={formatTimestamp(track.completed_at)} />
                    )}
                    {track.aborted_at && (
                        <DetailRow label="aborted_at" value={formatTimestamp(track.aborted_at)} />
                    )}
                    {track.track_metadata && Object.keys(track.track_metadata).length > 0 && (
                        <DetailRow
                            label="track_metadata"
                            value={JSON.stringify(track.track_metadata, null, 2)}
                            pre
                        />
                    )}
                    <div className={slStyles.actionsRow}>
                        <span className={slStyles.actionsLabel}>actions:</span>
                        <button
                            className={slStyles.actionBtn}
                            onClick={pauseTrack}
                            disabled={!canPause || actionLoading}
                            title={
                                canPause
                                    ? 'この Track を pending に戻す (running/alert → pending)'
                                    : `pause は running/alert からのみ可能 (現在: ${track.status})`
                            }
                        >
                            {actionLoading ? (
                                <Loader2 size={12} className={slStyles.spin} />
                            ) : (
                                <PauseCircle size={12} />
                            )}
                            <span>pending に戻す</span>
                        </button>
                        {actionError && (
                            <span className={slStyles.actionError}>
                                {actionError}
                            </span>
                        )}
                    </div>
                </div>
            )}
        </div>
    );
}

function DetailRow({ label, value, pre }: { label: string; value: string; pre?: boolean }) {
    return (
        <div className={slStyles.detailRow}>
            <span className={slStyles.detailLabel}>{label}:</span>
            {pre ? (
                <pre className={slStyles.detailValuePre}>{value}</pre>
            ) : (
                <span className={slStyles.detailValue}>{value}</span>
            )}
        </div>
    );
}
