import { useState, useEffect, useCallback } from 'react';

// Pulse タイムライン: SAIMemory messages を pulse_id でグルーピングして
// 自律 Track の動作を可視化する。設計: docs/intent/persona_cognition/debug_controller.md

interface Props {
    personaId: string;
}

interface PulseItem {
    pulse_id: string;
    track_id: string | null;
    track_title: string | null;
    track_type: string | null;
    track_seq: number | null;
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

interface PulseDetail {
    messages: PulseMessage[];
    prompts: PulsePrompt[];
}

const btnStyle: React.CSSProperties = {
    padding: '3px 10px',
    borderRadius: '4px',
    border: '1px solid #555',
    background: 'rgba(120,120,120,0.12)',
    color: 'inherit',
    cursor: 'pointer',
    fontSize: '0.8rem',
};

const cardStyle: React.CSSProperties = {
    border: '1px solid rgba(255,255,255,0.1)',
    borderRadius: '6px',
    marginBottom: '0.5rem',
    background: 'rgba(100,100,100,0.06)',
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
    return new Date(epoch * 1000).toLocaleString();
}

function roleColor(lr: string | null): string {
    if (lr === 'main_line') return '#69db7c';
    if (lr === 'sub_line') return '#74c0fc';
    if (lr === 'meta_judgment') return '#ffd43b';
    return '#888';
}

const LINE_ROLES = ['main_line', 'sub_line', 'meta_judgment'] as const;
const SCOPES = ['committed', 'volatile', 'discardable'] as const;

const selectStyle: React.CSSProperties = {
    fontSize: '0.68rem',
    background: 'rgba(0,0,0,0.3)',
    color: 'inherit',
    border: '1px solid rgba(255,255,255,0.15)',
    borderRadius: '3px',
    padding: '0 2px',
    cursor: 'pointer',
};

export default function PulseTimelineViewer({ personaId }: Props) {
    const [items, setItems] = useState<PulseItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [expanded, setExpanded] = useState<string | null>(null);
    const [detail, setDetail] = useState<Record<string, PulseDetail>>({});
    const [edits, setEdits] = useState<Record<string, { line_role?: string; scope?: string }>>({});
    const [saving, setSaving] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/pulse-timeline?limit=200`);
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
            const res = await fetch(`/api/people/${personaId}/messages`, {
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
                const res = await fetch(`/api/people/${personaId}/pulse-timeline/${pulseId}`);
                if (res.ok) {
                    const data = await res.json();
                    setDetail((prev) => ({
                        ...prev,
                        [pulseId]: { messages: data.messages || [], prompts: data.prompts || [] },
                    }));
                }
            } catch (e) {
                console.error('[PulseTimeline] detail failed', e);
            }
        }
    };

    return (
        // .content が overflow:hidden なので、スクロールはこの Viewer 側が担う
        <div style={{ padding: '0.5rem', height: '100%', overflowY: 'auto', boxSizing: 'border-box' }}>
            <div style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                position: 'sticky',
                top: 0,
                zIndex: 2,
                background: '#1f2023',
                padding: '0.5rem',
                margin: '-0.5rem -0.5rem 0.5rem -0.5rem',
            }}>
                <span style={{ fontSize: '0.85rem', color: '#888' }}>
                    {items.length} Pulse (新しい順、最大 200)
                </span>
                <div style={{ display: 'flex', gap: '0.3rem' }}>
                    {Object.keys(edits).length > 0 && (
                        <button onClick={saveEdits} disabled={saving} style={{ ...btnStyle, background: 'rgba(105,219,124,0.2)', borderColor: '#69db7c' }}>
                            {saving ? '保存中…' : `保存 (${Object.keys(edits).length}件)`}
                        </button>
                    )}
                    <button onClick={load} disabled={loading} style={btnStyle}>
                        {loading ? '読込中…' : '再読込'}
                    </button>
                </div>
            </div>

            {items.map((p) => (
                <div key={p.pulse_id} style={cardStyle}>
                    <div style={headerStyle} onClick={() => toggle(p.pulse_id)}>
                        <span style={{ color: '#888', fontSize: '0.75rem' }}>{fmtTime(p.last_created_at)}</span>
                        <span style={{ fontWeight: 600 }}>
                            {p.track_title || '(no track)'}
                            {p.track_type ? ` (${p.track_type})` : ''}
                        </span>
                        {p.track_seq != null && (
                            <span style={{ background: 'rgba(255,255,255,0.1)', padding: '1px 6px', borderRadius: '8px', fontSize: '0.75rem' }}>
                                #{p.track_seq}
                            </span>
                        )}
                        {p.line_roles.map((lr) => (
                            <span
                                key={lr}
                                style={{ color: roleColor(lr), fontSize: '0.72rem', border: `1px solid ${roleColor(lr)}`, borderRadius: '8px', padding: '0 5px' }}
                            >
                                {lr}
                            </span>
                        ))}
                        <span style={{ marginLeft: 'auto', fontSize: '0.72rem', color: '#777' }}>
                            {p.message_count} msg · {p.pulse_id.slice(0, 8)} · {expanded === p.pulse_id ? '▲' : '▼'}
                        </span>
                    </div>

                    {expanded === p.pulse_id && (
                        <div style={{ padding: '0.4rem 0.6rem', borderTop: '1px solid rgba(255,255,255,0.08)' }}>
                            {(() => {
                                const msgs = detail[p.pulse_id]?.messages || [];
                                const prompts = detail[p.pulse_id]?.prompts || [];
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
                                    return <span style={{ fontSize: '0.75rem', color: '#888' }}>(no messages)</span>;
                                }

                                const renderPrompt = (pr: PulsePrompt, key: string) => (
                                    <details key={key} style={{ marginBottom: '0.3rem' }}>
                                        <summary style={{ fontSize: '0.72rem', color: '#9ad', cursor: 'pointer' }}>
                                            入力プロンプト · {fmtTime(pr.created_at)}
                                        </summary>
                                        <pre style={{
                                            fontSize: '0.7rem',
                                            whiteSpace: 'pre-wrap',
                                            wordBreak: 'break-word',
                                            background: 'rgba(0,0,0,0.2)',
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
                                        <div style={{ fontSize: '0.7rem', display: 'flex', gap: '0.4rem', marginBottom: '2px', alignItems: 'center' }}>
                                            <span style={{ color: '#aaa' }}>{m.role}</span>
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
                                                style={{ ...selectStyle, color: m.scope === 'discardable' ? '#ff8787' : '#888' }}
                                            >
                                                {SCOPES.map((s) => <option key={s} value={s}>{s}</option>)}
                                            </select>
                                            {m.spell_seq != null && (
                                                <span style={{ color: '#c4a7e7', fontSize: '0.68rem' }}>
                                                    spell round {m.spell_seq}
                                                </span>
                                            )}
                                            {edits[m.entry_id] && (
                                                <span style={{ color: '#69db7c', fontSize: '0.65rem' }}>*</span>
                                            )}
                                        </div>
                                        <div
                                            style={{
                                                fontSize: '0.8rem',
                                                whiteSpace: 'pre-wrap',
                                                wordBreak: 'break-word',
                                                background: 'rgba(0,0,0,0.15)',
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
                                                fontSize: '0.68rem', color: '#c4a7e7',
                                                borderTop: '1px dashed rgba(196,167,231,0.3)',
                                                paddingTop: '0.3rem', marginTop: '0.2rem', marginBottom: '0.15rem',
                                            }}>
                                                spell loop ▸ {spellId.slice(0, 8)}
                                            </div>
                                        );
                                    } else if (!spellId && inSpellGroup) {
                                        inSpellGroup = false;
                                        currentSpellOrigin = null;
                                        elements.push(
                                            <div key={`spell-end-${i}`} style={{
                                                borderTop: '1px dashed rgba(196,167,231,0.3)',
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
                <div style={{ color: '#888', fontSize: '0.85rem', padding: '1rem' }}>
                    pulse_id 付きの messages がまだありません。
                </div>
            )}
        </div>
    );
}
