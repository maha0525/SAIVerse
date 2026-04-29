'use client';
import React, { useState, useEffect, useCallback } from 'react';
import { Loader2, Layers, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react';
import slStyles from './StorageLayersViewer.module.css';

interface StorageLayerStat {
    layer: string;
    layer_index: number;
    label: string;
    count: number;
    latest_at: number | null;
    note: string | null;
}

interface StorageLayerEntry {
    layer: string;
    entry_id: string;
    created_at: number | null;

    // SAIMemory message fields
    role?: string | null;
    content?: string | null;
    line_role?: string | null;
    line_id?: string | null;
    origin_track_id?: string | null;
    scope?: string | null;
    paired_action_text?: string | null;

    // meta_judgment fields
    judgment_action?: string | null;
    judgment_thought?: string | null;
    switch_to_track_id?: string | null;
    trigger_type?: string | null;
    trigger_context?: string | null;
    notify_to_track?: string | null;
    committed_to_main_cache?: boolean | null;
    track_at_judgment_id?: string | null;

    // track_local fields
    log_kind?: string | null;
    payload?: string | null;
    source_line_id?: string | null;
    track_id?: string | null;
}

interface StorageLayersResponse {
    summary: StorageLayerStat[];
    items: StorageLayerEntry[];
    total_returned: number;
    truncated: boolean;
}

interface Props {
    personaId: string;
}

const LAYER_FILTER_OPTIONS = [
    { value: '', label: '全層' },
    { value: 'meta_judgment', label: '[1] メタ判断' },
    { value: 'main_cache', label: '[2] メイン' },
    { value: 'sub_cache', label: '[3] サブ' },
    { value: 'track_local', label: '[5] Track ログ' },
];

const SCOPE_FILTER_OPTIONS = [
    { value: '', label: '全 scope' },
    { value: 'committed', label: 'committed' },
    { value: 'discardable', label: 'discardable' },
    { value: 'volatile', label: 'volatile' },
];

const LIMIT_OPTIONS = [50, 100, 200, 500];

function formatTimestamp(epoch: number | null | undefined): string {
    if (epoch === null || epoch === undefined) return '—';
    try {
        const d = new Date(epoch * 1000);
        return d.toLocaleString('ja-JP', { hour12: false });
    } catch {
        return String(epoch);
    }
}

function shortId(id: string | null | undefined): string {
    if (!id) return '—';
    return id.length > 8 ? `${id.slice(0, 8)}…` : id;
}

export default function StorageLayersViewer({ personaId }: Props) {
    const [data, setData] = useState<StorageLayersResponse | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [layerFilter, setLayerFilter] = useState<string>('');
    const [scopeFilter, setScopeFilter] = useState<string>('');
    const [trackFilter, setTrackFilter] = useState<string>('');
    const [limit, setLimit] = useState<number>(50);

    const [expanded, setExpanded] = useState<Set<string>>(new Set());

    const load = useCallback(async () => {
        setIsLoading(true);
        setError(null);
        try {
            const params = new URLSearchParams();
            if (layerFilter) params.append('layer', layerFilter);
            if (scopeFilter) params.append('scope', scopeFilter);
            if (trackFilter) params.append('track_id', trackFilter);
            params.append('limit', String(limit));
            const res = await fetch(
                `/api/people/${personaId}/storage-layers?${params.toString()}`
            );
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const json = (await res.json()) as StorageLayersResponse;
            setData(json);
        } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        } finally {
            setIsLoading(false);
        }
    }, [personaId, layerFilter, scopeFilter, trackFilter, limit]);

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
                    <label className={slStyles.filterLabel}>Layer:</label>
                    {LAYER_FILTER_OPTIONS.map(opt => (
                        <button
                            key={opt.value}
                            className={`${slStyles.chip} ${layerFilter === opt.value ? slStyles.chipActive : ''}`}
                            onClick={() => setLayerFilter(opt.value)}
                        >
                            {opt.label}
                        </button>
                    ))}
                </div>
                <div className={slStyles.filterGroup}>
                    <label className={slStyles.filterLabel}>Scope:</label>
                    {SCOPE_FILTER_OPTIONS.map(opt => (
                        <button
                            key={opt.value}
                            className={`${slStyles.chip} ${scopeFilter === opt.value ? slStyles.chipActive : ''}`}
                            onClick={() => setScopeFilter(opt.value)}
                        >
                            {opt.label}
                        </button>
                    ))}
                </div>
                <div className={slStyles.filterGroup}>
                    <label className={slStyles.filterLabel}>Track ID:</label>
                    <input
                        type="text"
                        value={trackFilter}
                        onChange={e => setTrackFilter(e.target.value)}
                        placeholder="(空 = 全 Track)"
                        className={slStyles.trackInput}
                    />
                </div>
                <div className={slStyles.filterGroup}>
                    <label className={slStyles.filterLabel}>Limit:</label>
                    {LIMIT_OPTIONS.map(n => (
                        <button
                            key={n}
                            className={`${slStyles.chip} ${limit === n ? slStyles.chipActive : ''}`}
                            onClick={() => setLimit(n)}
                        >
                            {n}
                        </button>
                    ))}
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

            {/* Summary */}
            <div className={slStyles.summary}>
                <div className={slStyles.summaryHeader}>
                    <Layers size={16} />
                    <span>7 層ストレージ サマリ</span>
                </div>
                <div className={slStyles.summaryGrid}>
                    {data?.summary.map(s => (
                        <div key={s.layer} className={slStyles.summaryCell}>
                            <div className={slStyles.summaryLabel}>{s.label}</div>
                            <div className={slStyles.summaryCount}>
                                {s.count.toLocaleString()} <span className={slStyles.summaryUnit}>件</span>
                            </div>
                            <div className={slStyles.summaryMeta}>
                                {s.latest_at ? `latest: ${formatTimestamp(s.latest_at)}` : '—'}
                            </div>
                            {s.note && <div className={slStyles.summaryNote}>{s.note}</div>}
                        </div>
                    ))}
                </div>
            </div>

            {/* Items list */}
            <div className={slStyles.itemsContainer}>
                {error && (
                    <div className={slStyles.errorBox}>
                        Error: {error}
                    </div>
                )}
                {!error && data && (
                    <>
                        <div className={slStyles.itemsHeader}>
                            <span>表示中: {data.total_returned} 件</span>
                            {data.truncated && (
                                <span className={slStyles.truncatedBadge}>
                                    一部の層で limit 超過、絞り込みを推奨
                                </span>
                            )}
                        </div>
                        {data.items.length === 0 && (
                            <div className={slStyles.emptyBox}>
                                該当エントリなし。フィルタを変更するか、ペルソナで Pulse を回してください。
                            </div>
                        )}
                        <div className={slStyles.itemsList}>
                            {data.items.map(item => (
                                <EntryCard
                                    key={`${item.layer}:${item.entry_id}`}
                                    entry={item}
                                    expanded={expanded.has(`${item.layer}:${item.entry_id}`)}
                                    onToggle={() => toggleExpand(`${item.layer}:${item.entry_id}`)}
                                />
                            ))}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

interface EntryCardProps {
    entry: StorageLayerEntry;
    expanded: boolean;
    onToggle: () => void;
}

function EntryCard({ entry, expanded, onToggle }: EntryCardProps) {
    const layerBadgeClass = `${slStyles.layerBadge} ${slStyles[`layer_${entry.layer}`] || ''}`;
    const time = formatTimestamp(entry.created_at);

    return (
        <div className={slStyles.entryCard}>
            <div className={slStyles.entryHeader} onClick={onToggle}>
                <span className={layerBadgeClass}>{entry.layer}</span>
                {entry.role && <span className={slStyles.roleBadge}>{entry.role}</span>}
                {entry.scope && entry.scope !== 'committed' && (
                    <span className={`${slStyles.scopeBadge} ${slStyles[`scope_${entry.scope}`] || ''}`}>
                        {entry.scope}
                    </span>
                )}
                {entry.judgment_action && (
                    <span className={slStyles.actionBadge}>{entry.judgment_action}</span>
                )}
                {entry.log_kind && (
                    <span className={slStyles.logKindBadge}>{entry.log_kind}</span>
                )}
                <span className={slStyles.entryTime}>{time}</span>
                <span className={slStyles.expandIcon}>
                    {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                </span>
            </div>

            <div className={slStyles.entryPreview}>
                {/* Layer-specific summary line */}
                {entry.layer === 'meta_judgment' && (
                    <span>
                        {entry.judgment_thought
                            ? entry.judgment_thought.slice(0, 200)
                            : '(no thought)'}
                        {entry.switch_to_track_id && (
                            <span className={slStyles.inlineMeta}>
                                {' → switch to '}{shortId(entry.switch_to_track_id)}
                            </span>
                        )}
                    </span>
                )}
                {(entry.layer === 'main_cache' || entry.layer === 'sub_cache') && (
                    <span>{(entry.content || '').slice(0, 200)}</span>
                )}
                {entry.layer === 'track_local' && (
                    <span>
                        track={shortId(entry.track_id)}{' '}
                        {entry.payload ? entry.payload.slice(0, 200) : '(no payload)'}
                    </span>
                )}
            </div>

            {expanded && (
                <div className={slStyles.entryDetails}>
                    {/* Common */}
                    <DetailRow label="entry_id" value={entry.entry_id} />
                    {entry.line_role && <DetailRow label="line_role" value={entry.line_role} />}
                    {entry.line_id && <DetailRow label="line_id" value={entry.line_id} />}
                    {entry.origin_track_id && (
                        <DetailRow label="origin_track_id" value={entry.origin_track_id} />
                    )}
                    {entry.scope && <DetailRow label="scope" value={entry.scope} />}

                    {/* messages */}
                    {entry.content && (
                        <DetailRow label="content" value={entry.content} pre />
                    )}
                    {entry.paired_action_text && (
                        <DetailRow
                            label="paired_action_text"
                            value={entry.paired_action_text}
                            pre
                        />
                    )}

                    {/* meta_judgment */}
                    {entry.judgment_action && (
                        <DetailRow label="judgment_action" value={entry.judgment_action} />
                    )}
                    {entry.judgment_thought && (
                        <DetailRow label="judgment_thought" value={entry.judgment_thought} pre />
                    )}
                    {entry.switch_to_track_id && (
                        <DetailRow label="switch_to_track_id" value={entry.switch_to_track_id} />
                    )}
                    {entry.trigger_type && (
                        <DetailRow label="trigger_type" value={entry.trigger_type} />
                    )}
                    {entry.trigger_context && (
                        <DetailRow label="trigger_context" value={entry.trigger_context} pre />
                    )}
                    {entry.notify_to_track && (
                        <DetailRow label="notify_to_track" value={entry.notify_to_track} pre />
                    )}
                    {entry.committed_to_main_cache !== null && entry.committed_to_main_cache !== undefined && (
                        <DetailRow
                            label="committed_to_main_cache"
                            value={String(entry.committed_to_main_cache)}
                        />
                    )}
                    {entry.track_at_judgment_id && (
                        <DetailRow label="track_at_judgment_id" value={entry.track_at_judgment_id} />
                    )}

                    {/* track_local */}
                    {entry.log_kind && <DetailRow label="log_kind" value={entry.log_kind} />}
                    {entry.payload && <DetailRow label="payload" value={entry.payload} pre />}
                    {entry.source_line_id && (
                        <DetailRow label="source_line_id" value={entry.source_line_id} />
                    )}
                    {entry.track_id && <DetailRow label="track_id" value={entry.track_id} />}
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
