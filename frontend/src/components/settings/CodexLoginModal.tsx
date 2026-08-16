'use client';

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X, Copy, CheckCircle, XCircle, Loader, ExternalLink } from 'lucide-react';
import styles from './CodexLoginModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

interface LoginStatus {
    state: 'idle' | 'starting' | 'waiting' | 'success' | 'error';
    attempt_id?: number;
    lease_id?: string;
    user_code?: string;
    verification_url?: string;
    error?: string;
    account_id?: string | null;
}

interface LoginLease {
    attempt_id: number;
    lease_id: string;
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
    // この modal の UI 世代番号と、現世代が受け取った lease 群。閉じる
    // (= returnAllLeases) たびに加えて「やり直す」を含む start のたびにも
    // 世代を進める。遅れて settle した旧世代の応答 (start 応答も poll 応答も)
    // は、自分の世代が死んでいると分かったら画面に触らず、lease を持って
    // いれば自分で即返却する — 現世代の lease 群には決して混ざらない。
    // 旧世代の cleanup が新世代の lease を巻き込む事故 (R4-①) と、リトライ
    // 前の試行の遅延 poll がリトライ後の画面を上書きする事故 (R6-②) を、
    // 同じ一つの番号で塞ぐ。
    const openSeqRef = useRef(0);
    const leasesRef = useRef<LoginLease[]>([]);

    const stopPolling = useCallback(() => {
        if (timerRef.current !== null) {
            clearInterval(timerRef.current);
            timerRef.current = null;
        }
    }, []);

    const sendCancel = useCallback((lease: LoginLease) => {
        fetch('/api/codex-auth/login/cancel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(lease),
        }).catch(() => {});
    }, []);

    // 現世代を閉じる: 世代番号を進めて (以後の遅延応答は自力返却に回る)、
    // この世代が記録済みの lease を全部返す。冪等 — handleClose と unmount
    // cleanup の両方から呼んでよい。
    const returnAllLeases = useCallback(() => {
        openSeqRef.current += 1;
        const leases = [...leasesRef.current];
        leasesRef.current = [];
        for (const lease of leases) {
            sendCancel(lease);
        }
    }, [sendCancel]);

    const startLogin = useCallback(() => {
        setStartError(null);
        setCopied(false);
        successNotifiedRef.current = false;
        // start のたびに世代を進める: リトライ前の試行の飛行中応答は、settle
        // した時点で世代不一致となり、この start が作る新しい画面状態に触れない。
        openSeqRef.current += 1;
        const mySeq = openSeqRef.current;
        (async () => {
            try {
                const res = await fetch('/api/codex-auth/login/start', { method: 'POST' });
                if (!res.ok) {
                    const body = await res.json().catch(() => null);
                    if (mySeq === openSeqRef.current) {
                        setStartError(body?.detail || `ログイン開始に失敗しました (HTTP ${res.status})`);
                    }
                    return;
                }
                const data: LoginStatus = await res.json();
                if (mySeq !== openSeqRef.current) {
                    // この応答が属する世代はもう閉じられた — 受け取った lease を
                    // その場で返し、画面には何も反映しない。
                    if (data.attempt_id && data.lease_id) {
                        sendCancel({ attempt_id: data.attempt_id, lease_id: data.lease_id });
                    }
                    return;
                }
                if (data.attempt_id && data.lease_id) {
                    leasesRef.current.push({ attempt_id: data.attempt_id, lease_id: data.lease_id });
                }
                setStatus(data);
            } catch (e) {
                if (mySeq === openSeqRef.current) {
                    setStartError(`ログイン開始に失敗しました: ${e}`);
                }
            }
        })();
    }, [sendCancel]);

    // Open → start the flow; close (isOpen=false) → stop polling. lease の
    // 返却は handleClose が行う (下の unmount effect が最後の受け皿)。
    useEffect(() => {
        if (!isOpen) return;
        setStatus({ state: 'idle' });
        startLogin();
        return () => {
            stopPolling();
        };
    }, [isOpen, startLogin, stopPolling]);

    // unmount の受け皿: 設定画面ごと閉じられた・タブが切り替わったなど、
    // handleClose を通らずにこのコンポーネントが消える経路でも lease を返す。
    // 返さないと、サーバー側のログイン試行が最大 15 分生き続け、その間に
    // ブラウザ側の認証を完了するとトークンが保存されてしまう。
    useEffect(() => {
        return () => {
            stopPolling();
            returnAllLeases();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // While waiting, poll the backend for progress. 応答が返る頃には世代が
    // 変わっている (閉じた・開き直した) ことがあるので、start 応答と同じく
    // 世代と attempt を照合してから画面へ反映する — 旧世代の飛行中応答が
    // 新しい画面の waiting を上書きしてポーリングを止めてしまわないように。
    useEffect(() => {
        if (!isOpen || status.state !== 'waiting') return;
        const mySeq = openSeqRef.current;
        const myAttempt = status.attempt_id;
        timerRef.current = setInterval(async () => {
            try {
                const res = await fetch('/api/codex-auth/login/status');
                if (mySeq !== openSeqRef.current) return;
                if (!res.ok) return;
                const next: LoginStatus = await res.json();
                if (mySeq !== openSeqRef.current) return;
                if (next.attempt_id !== undefined && myAttempt !== undefined
                    && next.attempt_id !== myAttempt) return;
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
    }, [isOpen, status.state, status.attempt_id, onSuccess, stopPolling]);

    const handleClose = useCallback(() => {
        stopPolling();
        // どの状態で閉じても、この modal が受け取った lease を全部返却する
        // (成功・失敗の確定後や、他のモーダルが lease を持つ間の cancel は
        // バックエンドが無視する)。
        returnAllLeases();
        onClose();
    }, [onClose, stopPolling, returnAllLeases]);

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
