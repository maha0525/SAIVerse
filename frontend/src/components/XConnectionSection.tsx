'use client';

import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Loader2 } from 'lucide-react';
import styles from './XConnectionSection.module.css';

interface XConnectionSectionProps {
    personaId: string;
    /** CSS class names from SettingsModal for consistent styling */
    fieldGroupClass?: string;
    labelClass?: string;
    descriptionClass?: string;
}

interface XStatus {
    connected: boolean;
    username: string | null;
    x_user_id: string | null;
    skip_confirmation: boolean;
}

export default function XConnectionSection({
    personaId,
    fieldGroupClass,
    labelClass,
    descriptionClass,
}: XConnectionSectionProps) {
    const [status, setStatus] = useState<XStatus | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const popupRef = useRef<Window | null>(null);
    const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

    const fetchStatus = useCallback(async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/x/status`);
            if (res.ok) {
                setStatus(await res.json());
                setError(null);
            }
        } catch {
            // Silently fail on status fetch - X integration is optional
        }
    }, [personaId]);

    useEffect(() => {
        fetchStatus();
        return () => {
            if (pollRef.current) clearInterval(pollRef.current);
        };
    }, [fetchStatus]);

    const handleConnect = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await fetch(`/api/people/${personaId}/x/auth-url`);
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                setError(data.detail || 'X連携URLの取得に失敗しました');
                setLoading(false);
                return;
            }
            const { auth_url } = await res.json();

            // Open popup for X authorization
            const popup = window.open(auth_url, 'x_auth', 'width=600,height=700,scrollbars=yes');
            popupRef.current = popup;

            // Poll for popup close, then refresh status
            pollRef.current = setInterval(() => {
                if (!popup || popup.closed) {
                    if (pollRef.current) clearInterval(pollRef.current);
                    pollRef.current = null;
                    popupRef.current = null;
                    setLoading(false);
                    fetchStatus();
                }
            }, 500);
        } catch {
            setError('X連携の開始に失敗しました');
            setLoading(false);
        }
    }, [personaId, fetchStatus]);

    const handleDisconnect = useCallback(async () => {
        if (!confirm('Xアカウントの連携を解除しますか？')) return;
        try {
            await fetch(`/api/people/${personaId}/x/disconnect`, { method: 'POST' });
            fetchStatus();
        } catch {
            setError('連携解除に失敗しました');
        }
    }, [personaId, fetchStatus]);

    const handleToggleConfirmation = useCallback(async (skip: boolean) => {
        try {
            await fetch(`/api/people/${personaId}/x/settings`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ skip_confirmation: skip }),
            });
            fetchStatus();
        } catch {
            setError('設定の更新に失敗しました');
        }
    }, [personaId, fetchStatus]);

    return (
        <div className={fieldGroupClass}>
            <label className={labelClass}>X (Twitter) 連携</label>
            {status?.connected ? (
                <div className={styles.connected}>
                    <div className={styles.statusRow}>
                        <span className={styles.username}>@{status.username}</span>
                        <span className={styles.badge}>連携中</span>
                    </div>
                    <label className={styles.checkboxLabel}>
                        <input
                            type="checkbox"
                            checked={status.skip_confirmation}
                            onChange={e => handleToggleConfirmation(e.target.checked)}
                        />
                        投稿前の確認をスキップする
                    </label>
                    <button className={styles.disconnectBtn} onClick={handleDisconnect}>
                        連携解除
                    </button>
                </div>
            ) : (
                <div className={styles.disconnected}>
                    <button
                        className={styles.connectBtn}
                        onClick={handleConnect}
                        disabled={loading}
                    >
                        {loading ? <Loader2 size={16} className="spin" /> : null}
                        Xアカウントを連携
                    </button>
                </div>
            )}
            {error && <div className={styles.error}>{error}</div>}
            <div className={descriptionClass}>
                ペルソナのXアカウントを連携すると、ツイートの投稿やタイムラインの閲覧が可能になります。
                X Developer App の API Key が .env に設定されている必要があります。
            </div>
        </div>
    );
}
