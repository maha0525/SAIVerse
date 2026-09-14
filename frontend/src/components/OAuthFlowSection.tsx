'use client';
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    /** 親から渡される選択中ペルソナID。未指定時はステータス表示なし。 */
    personaId: string;
}

interface OAuthStatus {
    connected: boolean;
    params: Record<string, string | number | boolean>;
}

export default function OAuthFlowSection({
    addonName,
    flows,
    personaId,
}: OAuthFlowSectionProps) {
    useLocale();
    if (!flows || flows.length === 0) return null;

    return (
        <div className={styles.section}>
            {flows.map((flow) => (
                <FlowRow
                    key={flow.key}
                    addonName={addonName}
                    flow={flow}
                    personaId={personaId}
                />
            ))}
        </div>
    );
}

function FlowRow({
    addonName,
    flow,
    personaId,
}: {
    addonName: string;
    flow: OAuthFlow;
    personaId: string;
}) {
    useLocale();
    const selectedPersonaId = personaId;
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
            const res = await apiFetch(
                `/api/oauth/${addonName}/${flow.key}/${encodeURIComponent(selectedPersonaId)}/status`
            );
            if (res.ok) {
                setStatus(await res.json());
                setError(null);
            } else {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || uiText("components.OAuthFlowSection.text001", { p1: res.status }));
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
            const res = await apiFetch(
                `/api/oauth/start/${addonName}/${flow.key}?persona_id=${encodeURIComponent(selectedPersonaId)}`
            );
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || uiText("components.OAuthFlowSection.text002", { p1: res.status }));
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
        if (!confirm(uiText("components.OAuthFlowSection.text003", { p1: flow.label }))) return;
        try {
            const res = await apiFetch(
                `/api/oauth/${addonName}/${flow.key}/${encodeURIComponent(selectedPersonaId)}`,
                { method: 'DELETE' }
            );
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || uiText("components.OAuthFlowSection.text004", { p1: res.status }));
                return;
            }
            setError(null);
            fetchStatus();
        } catch (err) {
            setError(err instanceof Error ? err.message : String(err));
        }
    }, [addonName, flow.key, flow.label, selectedPersonaId, fetchStatus]);

    if (!selectedPersonaId) return null;

    return (
        <div className={styles.flow}>
            <div className={styles.label}>{flow.label}</div>
            {flow.description && (
                <div className={styles.description}>{flow.description}</div>
            )}

            {status?.connected ? (
                <div className={styles.statusRow}>
                    <span data-i18n="components.OAuthFlowSection.text005" className={styles.statusBadgeConnected}>{uiText("components.OAuthFlowSection.text005")}</span>
                    {Object.entries(status.params).map(([k, v]) => (
                        <span key={k} className={styles.statusParam}>
                            {k}: {String(v)}
                        </span>
                    ))}
                    <button data-i18n="components.OAuthFlowSection.text006" className={styles.disconnectBtn} onClick={handleDisconnect}>{uiText("components.OAuthFlowSection.text006")}</button>
                </div>
            ) : (
                <div className={styles.statusRow}>
                    <span data-i18n="components.OAuthFlowSection.text007" className={styles.statusBadgeDisconnected}>{uiText("components.OAuthFlowSection.text007")}</span>
                    <button data-i18n="components.OAuthFlowSection.text008"
                        className={styles.connectBtn}
                        onClick={handleConnect}
                        disabled={loading || !selectedPersonaId}
                    >
                        {loading && <Loader2 size={14} className={styles.spin} />}
                        {flow.label}{uiText("components.OAuthFlowSection.text008")}</button>
                </div>
            )}

            {error && <div className={styles.error}>{error}</div>}
        </div>
    );
}
