
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useState, useEffect, useCallback } from 'react';
import styles from './PulseTimelineViewer.module.css';

// Pulse タイムライン: SAIMemory messages を pulse_id でグルーピングして
// Pulse ごとの動作を可視化する。設計: docs/intent/pulse_timeline_display.md
// ⚠ Track の題・種別・連番の表示欄は束 6c (2026-08-22) で撤去した — 書き手が
// 退役して常に空だったため (docs/intent/track_retirement.md §8.6)。

interface Props {
    personaId: string;
}

interface PulseItem {
    pulse_id: string;
    line_roles: string[];
    message_count: number;
    first_created_at: number | null;
    last_created_at: number | null;
}

interface PulseMessage {
    entry_id: string;
    role: string;
    content: string;
    created_at: number | null;
    line_role: string | null;
    scope: string | null;
    origin_track_id: string | null;
    spell_origin_id: string | null;
    spell_seq: number | null;
}

interface PulsePrompt {
    node_id: string | null;
    content: string;
    created_at: number | null;
}

interface GapMessage {
    entry_id: string;
    role: string;
    content: string;
    created_at: number | null;
}

interface PulseDetail {
    messages: PulseMessage[];
    prompts: PulsePrompt[];
    gap_messages: GapMessage[];
}

const btnStyle: React.CSSProperties = {
    padding: '3px 10px',
    borderRadius: '4px',
    border: '1px solid var(--border-color)',
    background: 'var(--bg-tertiary)',
    color: 'inherit',
    cursor: 'pointer',
    fontSize: '0.8rem',
};

const cardStyle: React.CSSProperties = {
    border: '1px solid var(--border-color)',
    borderRadius: '6px',
    marginBottom: '0.5rem',
    background: 'var(--bg-secondary)',
};

const headerStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
    flexWrap: 'wrap',
    padding: '0.45rem 0.6rem',
    cursor: 'pointer',
};

function fmtTime(epoch: number | null): string {
    if (!epoch) return '—';
    return new Date(epoch * 1000).toLocaleString(getFormatLocale());
}

function roleColor(lr: string | null): string {
    if (lr === 'main_line') return 'var(--timeline-main)';
    if (lr === 'sub_line') return 'var(--timeline-sub)';
    if (lr === 'meta_judgment') return 'var(--timeline-warning)';
    return 'var(--text-secondary)';
}

const LINE_ROLES = ['main_line', 'sub_line', 'meta_judgment'] as const;
const SCOPES = ['committed', 'volatile', 'discardable'] as const;

const selectStyle: React.CSSProperties = {
    fontSize: '0.68rem',
    background: 'var(--bg-tertiary)',
    color: 'inherit',
    border: '1px solid var(--border-color)',
    borderRadius: '3px',
    padding: '0 2px',
    cursor: 'pointer',
};

export default function PulseTimelineViewer({ personaId }: Props) {
    useLocale();
    const [items, setItems] = useState<PulseItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [expanded, setExpanded] = useState<string | null>(null);
    const [detail, setDetail] = useState<Record<string, PulseDetail>>({});
    const [edits, setEdits] = useState<Record<string, { line_role?: string; scope?: string }>>({});
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/pulse-timeline?limit=200`);
            if (res.ok) {
                const data = await res.json();
                setItems(data.items || []);
            }
        } catch (e) {
            console.error('[PulseTimeline] load failed', e);
        } finally {
            setLoading(false);
        }
    }, [personaId]);

    useEffect(() => {
        load();
    }, [load]);

    const recordEdit = (entryId: string, field: 'line_role' | 'scope', value: string) => {
        setEdits((prev) => ({
            ...prev,
            [entryId]: { ...prev[entryId], [field]: value },
        }));
        setDetail((prev) => {
            const updated = { ...prev };
            Object.keys(updated).forEach((pulseId) => {
                updated[pulseId] = {
                    ...updated[pulseId],
                    messages: updated[pulseId].messages.map((m) =>
                        m.entry_id === entryId ? { ...m, [field]: value } : m
                    ),
                };
            });
            return updated;
        });
    };

    const saveEdits = async () => {
        const entries = Object.entries(edits);
        if (entries.length === 0) return;
        setSaving(true);
        try {
            const updates = entries.map(([entry_id, changes]) => ({ entry_id, ...changes }));
            const res = await apiFetch(`/api/people/${personaId}/messages`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates }),
            });
            if (res.ok) {
                const data = await res.json();
                console.log(`[PulseTimeline] saved ${data.updated} edits`);
                setEdits({});
            }
        } catch (e) {
            console.error('[PulseTimeline] save failed', e);
        } finally {
            setSaving(false);
        }
    };

    const toggle = async (pulseId: string) => {
        if (expanded === pulseId) {
            setExpanded(null);
            return;
        }
        setExpanded(pulseId);
        if (!detail[pulseId]) {
            try {
                const res = await apiFetch(`/api/people/${personaId}/pulse-timeline/${pulseId}`);
                if (res.ok) {
                    const data = await res.json();
                    setDetail((prev) => ({
                        ...prev,
                        [pulseId]: {
                            messages: data.messages || [],
                            prompts: data.prompts || [],
                            gap_messages: data.gap_messages || [],
                        },
                    }));
                }
            } catch (e) {
                console.error('[PulseTimeline] detail failed', e);
            }
        }
    };

    return (
        // .content が overflow:hidden なので、スクロールはこの Viewer 側が担う
        <div className={styles.viewer}>
            <div style={{
                display: 'flex',
                justifyContent: 'space-between',
                flexWrap: 'wrap',
                gap: '0.5rem',
                alignItems: 'center',
                position: 'sticky',
                top: 0,
                zIndex: 2,
                background: 'var(--bg-secondary)',
                padding: '0.5rem',
                margin: '-0.5rem -0.5rem 0.5rem -0.5rem',
            }}>
                <span data-i18n="components.memory.PulseTimelineViewer.text001" style={{ fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    {items.length}{uiText("components.memory.PulseTimelineViewer.text001")}</span>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.3rem' }}>
                    {Object.keys(edits).length > 0 && (
                        <button data-i18n="components.memory.PulseTimelineViewer.text002 components.memory.PulseTimelineViewer.text003" onClick={saveEdits} disabled={saving} style={{ ...btnStyle, background: 'var(--bg-tertiary)', borderColor: 'var(--timeline-main)' }}>
                            {saving ? uiText("components.memory.PulseTimelineViewer.text002") : uiText("components.memory.PulseTimelineViewer.text003", { p1: Object.keys(edits).length })}
                        </button>
                    )}
                    <button data-i18n="components.memory.PulseTimelineViewer.text004 components.memory.PulseTimelineViewer.text005" onClick={load} disabled={loading} style={btnStyle}>
                        {loading ? uiText("components.memory.PulseTimelineViewer.text004") : uiText("components.memory.PulseTimelineViewer.text005")}
                    </button>
                </div>
            </div>

            {items.map((p) => (
                <div key={p.pulse_id} style={cardStyle}>
                    <div style={headerStyle} onClick={() => toggle(p.pulse_id)}>
                        <span style={{ color: 'var(--text-secondary)', fontSize: '0.75rem' }}>{fmtTime(p.last_created_at)}</span>
                        {p.line_roles.map((lr) => (
                            <span
                                key={lr}
                                style={{ color: roleColor(lr), fontSize: '0.72rem', border: `1px solid ${roleColor(lr)}`, borderRadius: '8px', padding: '0 5px' }}
                            >
                                {lr}
                            </span>
                        ))}
                        <span style={{ marginLeft: 'auto', fontSize: '0.72rem', color: 'var(--text-secondary)' }}>
                            {p.message_count} {uiText("components.memory.PulseTimelineViewer.label001")}{p.pulse_id.slice(0, 8)} · {expanded === p.pulse_id ? '▲' : '▼'}
                        </span>
                    </div>

                    {expanded === p.pulse_id && (
                        <div style={{ padding: '0.4rem 0.6rem', borderTop: '1px solid var(--border-color)' }}>
                            {(() => {
                                const msgs = detail[p.pulse_id]?.messages || [];
                                const prompts = detail[p.pulse_id]?.prompts || [];
                                const gapMsgs = detail[p.pulse_id]?.gap_messages || [];
                                const promptByNodeId = new Map<string, PulsePrompt>();
                                prompts.forEach((pr) => {
                                    if (pr.node_id) promptByNodeId.set(pr.node_id, pr);
                                });

                                type TimelineEntry =
                                    | { kind: 'prompt'; prompt: PulsePrompt }
                                    | { kind: 'message'; message: PulseMessage };
                                const timeline: TimelineEntry[] = [];
                                for (const m of msgs) {
                                    const pr = promptByNodeId.get(m.entry_id);
                                    if (pr) timeline.push({ kind: 'prompt', prompt: pr });
                                    timeline.push({ kind: 'message', message: m });
                                }

                                if (timeline.length === 0) {
                                    return <span style={{ fontSize: '0.75rem', color: 'var(--text-secondary)' }}>{uiText("components.memory.PulseTimelineViewer.label002")}</span>;
                                }

                                const renderPrompt = (pr: PulsePrompt, key: string) => (
                                    <details key={key} style={{ marginBottom: '0.3rem' }}>
                                        <summary data-i18n="components.memory.PulseTimelineViewer.text006" style={{ fontSize: '0.72rem', color: 'var(--timeline-sub)', cursor: 'pointer' }}>{uiText("components.memory.PulseTimelineViewer.text006")}{fmtTime(pr.created_at)}
                                        </summary>
                                        <pre className={styles.content} style={{
                                            fontSize: '0.7rem',
                                            whiteSpace: 'pre-wrap',
                                            wordBreak: 'break-word',
                                            background: 'var(--bg-tertiary)',
                                            padding: '0.3rem 0.5rem',
                                            borderRadius: '4px',
                                            maxHeight: '320px',
                                            overflow: 'auto',
                                            margin: '0.2rem 0',
                                        }}>
                                            {pr.content}
                                        </pre>
                                    </details>
                                );

                                const renderMessage = (m: PulseMessage) => (
                                    <div key={m.entry_id} style={{ marginBottom: '0.4rem', opacity: m.scope === 'discardable' ? 0.5 : 1 }}>
                                        <div style={{ fontSize: '0.7rem', display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginBottom: '2px', alignItems: 'center' }}>
                                            <span style={{ color: 'var(--text-secondary)' }}>{m.role}</span>
                                            <select
                                                value={m.line_role || ''}
                                                onChange={(e) => recordEdit(m.entry_id, 'line_role', e.target.value)}
                                                style={{ ...selectStyle, color: roleColor(m.line_role) }}
                                            >
                                                {LINE_ROLES.map((lr) => <option key={lr} value={lr}>{lr}</option>)}
                                            </select>
                                            <select
                                                value={m.scope || ''}
                                                onChange={(e) => recordEdit(m.entry_id, 'scope', e.target.value)}
                                                style={{ ...selectStyle, color: m.scope === 'discardable' ? 'var(--timeline-discardable)' : 'var(--text-secondary)' }}
                                            >
                                                {SCOPES.map((s) => <option key={s} value={s}>{s}</option>)}
                                            </select>
                                            {m.spell_seq != null && (
                                                <span style={{ color: 'var(--timeline-spell)', fontSize: '0.68rem' }}>
                                                    {uiText("components.memory.PulseTimelineViewer.label003")}{m.spell_seq}
                                                </span>
                                            )}
                                            {edits[m.entry_id] && (
                                                <span style={{ color: 'var(--timeline-main)', fontSize: '0.65rem' }}>*</span>
                                            )}
                                        </div>
                                        <div
                                            className={styles.content}
                                            style={{
                                                fontSize: '0.8rem',
                                                whiteSpace: 'pre-wrap',
                                                wordBreak: 'break-word',
                                                background: 'var(--bg-tertiary)',
                                                padding: '0.3rem 0.5rem',
                                                borderRadius: '4px',
                                                fontFamily: 'monospace',
                                            }}
                                        >
                                            {m.content}
                                        </div>
                                    </div>
                                );

                                const elements: React.ReactNode[] = [];

                                if (gapMsgs.length > 0) {
                                    elements.push(
                                        <div data-i18n="components.memory.PulseTimelineViewer.text007 components.memory.PulseTimelineViewer.text008" key="gap-header" style={{
                                            fontSize: '0.68rem', color: 'var(--timeline-gap)',
                                            borderBottom: '1px dashed var(--timeline-gap)',
                                            paddingBottom: '0.2rem', marginBottom: '0.3rem',
                                        }}>{uiText("components.memory.PulseTimelineViewer.text007")}{gapMsgs.length}{uiText("components.memory.PulseTimelineViewer.text008")}</div>
                                    );
                                    gapMsgs.forEach((g, gi) => {
                                        elements.push(
                                            <div key={`gap-${gi}`} style={{ marginBottom: '0.3rem', opacity: 0.75 }}>
                                                <div style={{ fontSize: '0.7rem', display: 'flex', flexWrap: 'wrap', gap: '0.4rem', marginBottom: '2px' }}>
                                                    <span style={{ color: 'var(--timeline-gap)' }}>{g.role}</span>
                                                    <span style={{ color: 'var(--text-secondary)' }}>{fmtTime(g.created_at)}</span>
                                                </div>
                                                <div className={styles.content} style={{
                                                    fontSize: '0.8rem', whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                                                    background: 'var(--bg-tertiary)', padding: '0.3rem 0.5rem',
                                                    borderRadius: '4px', fontFamily: 'monospace',
                                                    borderLeft: '2px solid var(--timeline-gap)',
                                                }}>
                                                    {g.content}
                                                </div>
                                            </div>
                                        );
                                    });
                                    elements.push(
                                        <div key="gap-separator" style={{
                                            borderBottom: '1px dashed var(--timeline-gap)',
                                            marginBottom: '0.3rem',
                                        }} />
                                    );
                                }

                                let inSpellGroup = false;
                                let currentSpellOrigin: string | null = null;

                                timeline.forEach((entry, i) => {
                                    const spellId = entry.kind === 'message' ? entry.message.spell_origin_id : null;

                                    if (spellId && spellId !== currentSpellOrigin) {
                                        if (inSpellGroup) elements.push(<div key={`spell-end-${i}`} style={{ height: '2px' }} />);
                                        currentSpellOrigin = spellId;
                                        inSpellGroup = true;
                                        elements.push(
                                            <div key={`spell-start-${i}`} style={{
                                                fontSize: '0.68rem', color: 'var(--timeline-spell)',
                                                borderTop: '1px dashed var(--timeline-spell)',
                                                paddingTop: '0.3rem', marginTop: '0.2rem', marginBottom: '0.15rem',
                                            }}>
                                                {uiText("components.memory.PulseTimelineViewer.label004")}{spellId.slice(0, 8)}
                                            </div>
                                        );
                                    } else if (!spellId && inSpellGroup) {
                                        inSpellGroup = false;
                                        currentSpellOrigin = null;
                                        elements.push(
                                            <div key={`spell-end-${i}`} style={{
                                                borderTop: '1px dashed var(--timeline-spell)',
                                                marginTop: '0.15rem', marginBottom: '0.3rem',
                                            }} />
                                        );
                                    }

                                    if (entry.kind === 'prompt') {
                                        elements.push(renderPrompt(entry.prompt, `prompt-${i}`));
                                    } else {
                                        elements.push(renderMessage(entry.message));
                                    }
                                });

                                return elements;
                            })()}
                        </div>
                    )}
                </div>
            ))}

            {items.length === 0 && !loading && (
                <div data-i18n="components.memory.PulseTimelineViewer.text009" style={{ color: 'var(--text-secondary)', fontSize: '0.85rem', padding: '1rem' }}>{uiText("components.memory.PulseTimelineViewer.text009")}</div>
            )}
        </div>
    );
}
