'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X, Copy, CheckCircle, XCircle, Loader, ExternalLink } from 'lucide-react';
import styles from './CodexLoginModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

interface LoginStatus {
    state: 'idle' | 'waiting' | 'success' | 'error';
    user_code?: string;
    verification_url?: string;
    error?: string;
    account_id?: string | null;
}

interface Props {
    isOpen: boolean;
    onClose: () => void;
    /** Called once when the login reaches success (before the modal closes). */
    onSuccess: () => void;
}

const POLL_INTERVAL_MS = 2000;

/**
 * ChatGPT アカウントへのデバイスコードログイン。
 *
 * バックエンドがコードの申請とポーリングを担い、このモーダルは
 * 「コードを見せる」「進行状態を映す」だけを行う。トークンはブラウザに来ない。
 */
export default function CodexLoginModal({ isOpen, onClose, onSuccess }: Props) {
    const [status, setStatus] = useState<LoginStatus>({ state: 'idle' });
    const [startError, setStartError] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);
    // Poll timer + "did we already fire onSuccess" guard survive re-renders.
    const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const successNotifiedRef = useRef(false);

    const stopPolling = useCallback(() => {
        if (timerRef.current !== null) {
            clearInterval(timerRef.current);
            timerRef.current = null;
        }
    }, []);

    const startLogin = useCallback(async () => {
        setStartError(null);
        setCopied(false);
        successNotifiedRef.current = false;
        try {
            const res = await fetch('/api/codex-auth/login/start', { method: 'POST' });
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                setStartError(body?.detail || `ログイン開始に失敗しました (HTTP ${res.status})`);
                return;
            }
            setStatus(await res.json());
        } catch (e) {
            setStartError(`ログイン開始に失敗しました: ${e}`);
        }
    }, []);

    // Open → start the flow; close → abandon it (server side keeps nothing).
    useEffect(() => {
        if (!isOpen) return;
        setStatus({ state: 'idle' });
        startLogin();
        return () => {
            stopPolling();
        };
    }, [isOpen, startLogin, stopPolling]);

    // While waiting, poll the backend for progress.
    useEffect(() => {
        if (!isOpen || status.state !== 'waiting') return;
        timerRef.current = setInterval(async () => {
            try {
                const res = await fetch('/api/codex-auth/login/status');
                if (!res.ok) return;
                const next: LoginStatus = await res.json();
                setStatus(prev => ({ ...prev, ...next }));
                if (next.state === 'success' && !successNotifiedRef.current) {
                    successNotifiedRef.current = true;
                    onSuccess();
                }
            } catch {
                // 一時的な取得失敗は次のポーリングに任せる
            }
        }, POLL_INTERVAL_MS);
        return () => stopPolling();
    }, [isOpen, status.state, onSuccess, stopPolling]);

    const handleClose = useCallback(() => {
        stopPolling();
        if (status.state === 'waiting') {
            // ログイン待ちを放棄。コードは OpenAI 側で自然失効する。
            fetch('/api/codex-auth/login/cancel', { method: 'POST' }).catch(() => {});
        }
        onClose();
    }, [status.state, onClose, stopPolling]);

    const copyCode = useCallback(async () => {
        if (!status.user_code) return;
        try {
            await navigator.clipboard.writeText(status.user_code);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
        } catch {
            // clipboard が使えない環境では手入力してもらう
        }
    }, [status.user_code]);

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={handleClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3>ChatGPT アカウントでログイン</h3>
                    <button className={styles.closeBtn} onClick={handleClose} aria-label="閉じる">
                        <X size={18} />
                    </button>
                </div>
                <div className={styles.content}>
                    {startError ? (
                        <div className={styles.errorBox}>
                            <XCircle size={16} /> {startError}
                            <button className={styles.retryBtn} onClick={startLogin}>やり直す</button>
                        </div>
                    ) : status.state === 'success' ? (
                        <div className={styles.successBox}>
                            <CheckCircle size={20} />
                            <div>
                                <div className={styles.successTitle}>ログインしました</div>
                                <div className={styles.hint}>
                                    Codex サブスク経由のモデルがこのまま使えます。
                                </div>
                            </div>
                        </div>
                    ) : status.state === 'error' ? (
                        <div className={styles.errorBox}>
                            <XCircle size={16} /> {status.error || 'ログインに失敗しました。'}
                            <button className={styles.retryBtn} onClick={startLogin}>やり直す</button>
                        </div>
                    ) : status.state === 'waiting' ? (
                        <>
                            <ol className={styles.steps}>
                                <li>
                                    下のリンクをブラウザで開く（スマホでも可）
                                    <div>
                                        <a
                                            className={styles.verifyLink}
                                            href={status.verification_url}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                        >
                                            {status.verification_url} <ExternalLink size={12} />
                                        </a>
                                    </div>
                                </li>
                                <li>
                                    このコードを入力する
                                    <div className={styles.codeRow}>
                                        <span className={styles.userCode}>{status.user_code}</span>
                                        <button className={styles.copyBtn} onClick={copyCode}>
                                            {copied ? <CheckCircle size={14} /> : <Copy size={14} />}
                                            {copied ? 'コピーしました' : 'コピー'}
                                        </button>
                                    </div>
                                </li>
                                <li>ChatGPT アカウントでログインを済ませる</li>
                            </ol>
                            <div className={styles.waitingRow}>
                                <Loader size={14} className={styles.spinner} />
                                ログインの完了を待っています…（この画面は開いたままで大丈夫です）
                            </div>
                        </>
                    ) : (
                        <div className={styles.waitingRow}>
                            <Loader size={14} className={styles.spinner} /> 準備中…
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
