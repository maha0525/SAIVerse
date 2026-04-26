'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';
import styles from './OAuthFlowSection.module.css';

// addon.json の oauth_flows[] と一致
export interface OAuthFlow {
    key: string;
    label: string;
    description?: string;
    provider: string;
    authorize_url: string;
    token_url: string;
    scopes: string[];
    client_id_param: string;
    client_secret_param?: string;
    callback_path?: string;
    result_mapping: Record<string, string>;
    post_authorize_handler?: string;
}

interface OAuthFlowSectionProps {
    addonName: string;
    flows: OAuthFlow[];
    personas: { id: string; name: string }[];
}

interface OAuthStatus {
    connected: boolean;
    params: Record<string, string | number | boolean>;
}

export default function OAuthFlowSection({
    addonName,
    flows,
    personas,
}: OAuthFlowSectionProps) {
    if (!flows || flows.length === 0) return null;

    return (
        <div className={styles.section}>
            {flows.map((flow) => (
                <FlowRow
                    key={flow.key}
                    addonName={addonName}
                    flow={flow}
                    personas={personas}
                />
            ))}
        </div>
    );
}

function FlowRow({
    addonName,
    flow,
    personas,
}: {
    addonName: string;
    flow: OAuthFlow;
    personas: { id: string; name: string }[];
}) {
    const [selectedPersonaId, setSelectedPersonaId] = useState<string>(
        personas[0]?.id ?? ''
    );
    const [status, setStatus] = useState<OAuthStatus | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const popupRef = useRef<Window | null>(null);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const fetchStatus = useCallback(async () => {
        if (!selectedPersonaId) {
            setStatus(null);
            return;
        }
        try {
            const res = await fetch(
                `/api/oauth/${addonName}/${flow.key}/${encodeURIComponent(selectedPersonaId)}/status`
            );
            if (res.ok) {
                setStatus(await res.json());
                setError(null);
            } else {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || `ステータス取得に失敗しました (${res.status})`);
            }
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    }, [addonName, flow.key, selectedPersonaId]);

    useEffect(() => {
        fetchStatus();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [fetchStatus]);

    const handleConnect = useCallback(async () => {
        if (!selectedPersonaId) return;
        setLoading(true);
        setError(null);
        try {
            const res = await fetch(
                `/api/oauth/start/${addonName}/${flow.key}?persona_id=${encodeURIComponent(selectedPersonaId)}`
            );
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || `認可URL取得に失敗しました (${res.status})`);
                setLoading(false);
                return;
            }
            const { auth_url } = await res.json();

            const popup = window.open(
                auth_url,
                `oauth_${addonName}_${flow.key}`,
                'width=600,height=720,scrollbars=yes'
            );
            popupRef.current = popup;

            pollRef.current = setInterval(() => {
                if (!popup || popup.closed) {
                    if (pollRef.current) clearInterval(pollRef.current);
                    pollRef.current = null;
                    popupRef.current = null;
                    setLoading(false);
                    fetchStatus();
                }
            }, 500);
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
            setLoading(false);
        }
    }, [addonName, flow.key, selectedPersonaId, fetchStatus]);

    const handleDisconnect = useCallback(async () => {
        if (!selectedPersonaId) return;
        if (!confirm(`${flow.label}: 連携を解除しますか？`)) return;
        try {
            const res = await fetch(
                `/api/oauth/${addonName}/${flow.key}/${encodeURIComponent(selectedPersonaId)}`,
                { method: 'DELETE' }
            );
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || `切断に失敗しました (${res.status})`);
                return;
            }
            setError(null);
            fetchStatus();
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    }, [addonName, flow.key, flow.label, selectedPersonaId, fetchStatus]);

    return (
        <div className={styles.flow}>
            <div className={styles.label}>{flow.label}</div>
            {flow.description && (
                <div className={styles.description}>{flow.description}</div>
            )}

            <div className={styles.personaRow}>
                <span className={styles.personaLabel}>ペルソナ:</span>
                <select
                    className={styles.personaSelect}
                    value={selectedPersonaId}
                    onChange={(e) => setSelectedPersonaId(e.target.value)}
                >
                    {personas.length === 0 && <option value="">（ペルソナなし）</option>}
                    {personas.map((p) => (
                        <option key={p.id} value={p.id}>
                            {p.name}
                        </option>
                    ))}
                </select>
            </div>

            {status?.connected ? (
                <div className={styles.statusRow}>
                    <span className={styles.statusBadgeConnected}>連携中</span>
                    {Object.entries(status.params).map(([k, v]) => (
                        <span key={k} className={styles.statusParam}>
                            {k}: {String(v)}
                        </span>
                    ))}
                    <button className={styles.disconnectBtn} onClick={handleDisconnect}>
                        切断
                    </button>
                </div>
            ) : (
                <div className={styles.statusRow}>
                    <span className={styles.statusBadgeDisconnected}>未連携</span>
                    <button
                        className={styles.connectBtn}
                        onClick={handleConnect}
                        disabled={loading || !selectedPersonaId}
                    >
                        {loading && <Loader2 size={14} className={styles.spin} />}
                        {flow.label}に接続
                    </button>
                </div>
            )}

            {error && <div className={styles.error}>{error}</div>}
        </div>
    );
}
