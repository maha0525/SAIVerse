'use client';
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useCallback, useEffect, useRef, useState } from 'react';
import { X, Copy, CheckCircle, XCircle, Loader, ExternalLink } from 'lucide-react';
import styles from './CodexLoginModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

/**
 * ログイン方式。
 * - siwc: 公式の Sign in with ChatGPT。ブラウザで OpenAI の承認ページを開き、
 *   承認後にブラウザが SAIVerse の 127.0.0.1 コールバックへ戻ってくる。
 * - device: 従来のデバイスコード方式。表示したコードを OpenAI のページで入力する。
 *
 * 二つの方式は lease / 世代番号 / ポーリングの規則を共有し、違うのは
 * エンドポイントの接頭辞と待機画面の中身だけ。
 */
type LoginFlow = 'siwc' | 'device';

const FLOW_PREFIX: Record<LoginFlow, string> = {
    siwc: '/api/codex-auth/siwc',
    device: '/api/codex-auth/login',
};

interface LoginStatus {
    state: 'idle' | 'starting' | 'waiting' | 'success' | 'error';
    attempt_id?: number;
    lease_id?: string;
    // device flow
    user_code?: string;
    verification_url?: string;
    // siwc flow
    auth_url?: string;
    redirect_uri?: string;
    error?: string;
    account_id?: string | null;
    // complete (貼り付けたアドレスの処理) の応答だけが持つ
    ok?: boolean;
    message?: string;
}

interface LoginLease {
    flow: LoginFlow;
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
 * ChatGPT アカウントへのログイン。既定は公式の Sign in with ChatGPT で、
 * 従来のデバイスコード方式へ切り替えられる。
 *
 * バックエンドが認可の受け取りとトークン保存を担い、このモーダルは
 * 「承認ページ (またはコード) を見せる」「進行状態を映す」だけを行う。
 * トークンはブラウザに来ない。
 */
export default function CodexLoginModal({ isOpen, onClose, onSuccess }: Props) {
    useLocale();
    const [flow, setFlow] = useState<LoginFlow>('siwc');
    const [status, setStatus] = useState<LoginStatus>({ state: 'idle' });
    const [startError, setStartError] = useState<string | null>(null);
    const [copied, setCopied] = useState(false);
    // 別端末で承認した場合に貼り付ける、コールバック先のアドレス。
    const [callbackUrl, setCallbackUrl] = useState('');
    const [completeError, setCompleteError] = useState<string | null>(null);
    const [completeSubmitting, setCompleteSubmitting] = useState(false);
    const completeSubmittingRef = useRef(false);
    // Poll timer + "did we already fire onSuccess" guard survive re-renders.
    const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const successNotifiedRef = useRef(false);
    // この modal の UI 世代番号と、この modal が記録した lease 群。閉じる
    // (= returnAllLeases) たびに加えて「やり直す」「方式の切り替え」を含む
    // start のたびにも世代を進める。遅れて settle した旧世代の応答 (start 応答・
    // poll 応答・complete 応答) は、自分の世代が死んでいると分かったら画面に
    // 触らず、start 応答で lease を受け取っていればその場で返却する — 記録には
    // 加えない。旧世代の cleanup が新世代の lease を巻き込む事故 (R4-①) と、
    // リトライ前の試行の遅延 poll がリトライ後の画面を上書きする事故 (R6-②) を、
    // 同じ一つの番号で塞ぐ。
    // 返却の時期は経路で違う: 方式の切り替えと閉じる操作は記録済みの lease を
    // その場で全部返すが、「やり直す」は世代を進めるだけで、それまでに記録した
    // lease は返さずに持ち続ける (閉じるときにまとめて返す)。遅れて返る旧試行の
    // lease は attempt_id が古いので、バックエンドは新しい試行に触れずに無視する。
    // lease はそれぞれ自分の方式を覚えていて、返却はその方式の cancel へ送る。
    const openSeqRef = useRef(0);
    const leasesRef = useRef<LoginLease[]>([]);

    const stopPolling = useCallback(() => {
        if (timerRef.current !== null) {
            clearInterval(timerRef.current);
            timerRef.current = null;
        }
    }, []);

    const sendCancel = useCallback((lease: LoginLease) => {
        apiFetch(`${FLOW_PREFIX[lease.flow]}/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ attempt_id: lease.attempt_id, lease_id: lease.lease_id }),
        }).catch(() => {});
    }, []);

    // 現世代を閉じる: 世代番号を進めて (以後の遅延応答は自力返却に回る)、
    // この世代が記録済みの lease を全部返す。冪等 — handleClose・方式の切り替え・
    // unmount cleanup のどこから呼んでもよい。
    const returnAllLeases = useCallback(() => {
        openSeqRef.current += 1;
        const leases = [...leasesRef.current];
        leasesRef.current = [];
        for (const lease of leases) {
            sendCancel(lease);
        }
    }, [sendCancel]);

    // 現世代・現試行の状態応答を画面へ反映する (poll と complete の共通出口)。
    // 旧世代の応答は黙って捨てる。判断は必ず setStatus の中で、その時点の
    // 最新の画面状態 (prev) に対して行う — poll と complete の応答は前後して
    // 届くので、応答を出した時点の status で判断すると古い応答が新しい結果を
    // 上書きする。
    const applyStatus = useCallback((next: LoginStatus, mySeq: number, myAttempt: number | undefined) => {
        if (mySeq !== openSeqRef.current) return;
        setStatus(prev => {
            // 同じ試行の結果 (success / error) は確定。後から届いた途中経過
            // (waiting / starting / idle) で覆さず、success の後の error も無視する。
            const prevIsThisAttempt = myAttempt === undefined || prev.attempt_id === myAttempt;
            if (prevIsThisAttempt && prev.state === 'success') return prev;
            if (prevIsThisAttempt && prev.state === 'error' && next.state !== 'success') return prev;

            if (next.attempt_id !== undefined && myAttempt !== undefined
                && next.attempt_id !== myAttempt) {
                // サーバーの試行が別のものに入れ替わった (別の画面が新しい
                // ログインを始めた・別のプロセスでのログアウトの後に開き直された)。
                // この画面が待っていた試行はもう結果を出さないので、やり直しを
                // 案内する。別の試行の中身 (承認ページのリンクやコード) は
                // この画面に取り込まない。
                if (prev.state !== 'waiting' || prev.attempt_id !== myAttempt) return prev;
                return {
                    ...prev,
                    state: 'error',
                    error: uiText("components.settings.CodexLoginModal.text030"),
                };
            }
            if (next.state === 'idle' && (next.ok === false || myAttempt !== undefined)) {
                // この画面が見ていた試行が、成功も失敗もせずに消えた (取り下げ・
                // 同じ SAIVerse の別の画面でのログアウト、または貼り付けたアドレスの
                // 処理時に試行がもう生きていなかった)。idle のまま映すと「準備中」の
                // くるくるで止まってやり直しようがないので、理由とやり直しボタンを出す。
                // message は complete の応答が失敗 (ok === false) を伝えるときだけ使う。
                return {
                    ...prev,
                    state: 'error',
                    error: (next.ok === false ? next.message : undefined)
                        || next.error
                        || uiText("components.settings.CodexLoginModal.text029"),
                };
            }
            return { ...prev, ...next };
        });
    }, []);

    // 成功の通知は画面状態が success になったときに一度だけ行う (setStatus の
    // 中では副作用を起こさない)。この effect は開いたときの effect より前に
    // 宣言しておく — 開き直した最初の描画では status がまだ前回の success の
    // ままなので、先に開いたときの effect が通知済みの印を戻すと、ここで
    // 前回の成功をもう一度通知してしまう。
    useEffect(() => {
        if (status.state === 'success' && !successNotifiedRef.current) {
            successNotifiedRef.current = true;
            onSuccess();
        }
    }, [status.state, onSuccess]);

    const startLogin = useCallback((nextFlow: LoginFlow) => {
        setFlow(nextFlow);
        setStartError(null);
        setCopied(false);
        setCallbackUrl('');
        setCompleteError(null);
        completeSubmittingRef.current = false;
        setCompleteSubmitting(false);
        successNotifiedRef.current = false;
        // start のたびに世代を進める: リトライ前の試行の飛行中応答は、settle
        // した時点で世代不一致となり、この start が作る新しい画面状態に触れない。
        // 記録済みの lease はここでは返さない (閉じるときにまとめて返す。古い
        // attempt_id の返却はバックエンドが無視する)。
        openSeqRef.current += 1;
        const mySeq = openSeqRef.current;
        (async () => {
            try {
                const res = await apiFetch(`${FLOW_PREFIX[nextFlow]}/start`, { method: 'POST' });
                if (!res.ok) {
                    const body = await res.json().catch(() => null);
                    if (mySeq === openSeqRef.current) {
                        setStartError(body?.detail || uiText("components.settings.CodexLoginModal.text001", { p1: res.status }));
                    }
                    return;
                }
                const data: LoginStatus = await res.json();
                if (mySeq !== openSeqRef.current) {
                    // この応答が属する世代はもう閉じられた — 受け取った lease を
                    // その場で返し、画面には何も反映しない。
                    if (data.attempt_id && data.lease_id) {
                        sendCancel({ flow: nextFlow, attempt_id: data.attempt_id, lease_id: data.lease_id });
                    }
                    return;
                }
                if (data.attempt_id && data.lease_id) {
                    leasesRef.current.push({ flow: nextFlow, attempt_id: data.attempt_id, lease_id: data.lease_id });
                }
                if (data.state === 'idle') {
                    // 準備中に取り下げ・ログアウトで無効になった試行。くるくるで
                    // 止めず、やり直しボタンを出す。
                    setStatus({ ...data, state: 'error', error: uiText("components.settings.CodexLoginModal.text029") });
                    return;
                }
                setStatus(data);
            } catch (e) {
                if (mySeq === openSeqRef.current) {
                    setStartError(uiText("components.settings.CodexLoginModal.text002", { p1: e }));
                }
            }
        })();
    }, [sendCancel]);

    // 方式の切り替え: いまの方式で受け取った lease を全部返して (世代も進む)
    // から、もう一方の方式を始める。
    const switchFlow = useCallback((nextFlow: LoginFlow) => {
        stopPolling();
        returnAllLeases();
        setStatus({ state: 'idle' });
        startLogin(nextFlow);
    }, [stopPolling, returnAllLeases, startLogin]);

    // Open → start the default (SIWC) flow; close (isOpen=false) → stop polling.
    // lease の返却は handleClose が行う (下の unmount effect が最後の受け皿)。
    useEffect(() => {
        if (!isOpen) return;
        setStatus({ state: 'idle' });
        startLogin('siwc');
        return () => {
            stopPolling();
        };
    }, [isOpen, startLogin, stopPolling]);

    // unmount の受け皿: 設定画面ごと閉じられた・タブが切り替わったなど、
    // handleClose を通らずにこのコンポーネントが消える経路でも lease を返す。
    // 返さないと、サーバー側のログイン試行が生き続け、その間にブラウザ側の
    // 承認を完了するとトークンが保存されてしまう。
    useEffect(() => {
        return () => {
            stopPolling();
            returnAllLeases();
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // While waiting, poll the active flow's status endpoint. 応答が返る頃には
    // 世代が変わっている (閉じた・開き直した・方式を切り替えた) ことがあるので、
    // start 応答と同じく世代と attempt を照合してから画面へ反映する — 旧世代の
    // 飛行中応答が新しい画面の waiting を上書きしてポーリングを止めてしまわないように。
    useEffect(() => {
        if (!isOpen || status.state !== 'waiting') return;
        const mySeq = openSeqRef.current;
        const myAttempt = status.attempt_id;
        const prefix = FLOW_PREFIX[flow];
        timerRef.current = setInterval(async () => {
            try {
                const res = await apiFetch(`${prefix}/status`);
                if (mySeq !== openSeqRef.current) return;
                if (!res.ok) return;
                const next: LoginStatus = await res.json();
                applyStatus(next, mySeq, myAttempt);
            } catch {
                // 一時的な取得失敗は次のポーリングに任せる
            }
        }, POLL_INTERVAL_MS);
        return () => stopPolling();
    }, [isOpen, flow, status.state, status.attempt_id, applyStatus, stopPolling]);

    // 別端末で承認した場合: 表示に失敗したコールバック先のアドレスを
    // バックエンドへ渡して、認可の受け取りを代行してもらう。失敗は待機画面の
    // まま、入力欄の下に表示する。
    const submitCallbackUrl = useCallback(async () => {
        const url = callbackUrl.trim();
        if (!url || status.attempt_id === undefined) return;
        if (completeSubmittingRef.current) return;
        completeSubmittingRef.current = true;
        setCompleteSubmitting(true);
        setCompleteError(null);
        const mySeq = openSeqRef.current;
        const myAttempt = status.attempt_id;
        try {
            const res = await apiFetch(`${FLOW_PREFIX.siwc}/complete`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ attempt_id: myAttempt, callback_url: url }),
            });
            if (mySeq !== openSeqRef.current) return;
            if (!res.ok) {
                const body = await res.json().catch(() => null);
                if (mySeq !== openSeqRef.current) return;
                setCompleteError(body?.detail || uiText("components.settings.CodexLoginModal.text023", { p1: res.status }));
                return;
            }
            const next: LoginStatus = await res.json();
            applyStatus(next, mySeq, myAttempt);
        } catch (e) {
            if (mySeq === openSeqRef.current) {
                setCompleteError(uiText("components.settings.CodexLoginModal.text024", { p1: e }));
            }
        } finally {
            // 世代が変わっていたら、新しい世代の start が送信中フラグを既に戻している。
            if (mySeq === openSeqRef.current) {
                completeSubmittingRef.current = false;
                setCompleteSubmitting(false);
            }
        }
    }, [callbackUrl, status.attempt_id, applyStatus]);

    const handleClose = useCallback(() => {
        stopPolling();
        // どの状態・どの方式で閉じても、この modal が受け取った lease を全部返却する
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

    const siwcWaiting = (
        <>
            <p data-i18n="components.settings.CodexLoginModal.text017" className={styles.lead}>{uiText("components.settings.CodexLoginModal.text017")}</p>
            {status.auth_url && (
                <a
                    data-i18n="components.settings.CodexLoginModal.text018"
                    className={styles.primaryLink}
                    href={status.auth_url}
                    target="_blank"
                    rel="noopener noreferrer"
                >
                    {uiText("components.settings.CodexLoginModal.text018")} <ExternalLink size={14} />
                </a>
            )}
            <div data-i18n="components.settings.CodexLoginModal.text019" className={styles.waitingRow}>
                <Loader size={14} className={styles.spinner} />{uiText("components.settings.CodexLoginModal.text019")}</div>
            <details className={styles.otherDevice}>
                <summary data-i18n="components.settings.CodexLoginModal.text020">{uiText("components.settings.CodexLoginModal.text020")}</summary>
                <p data-i18n="components.settings.CodexLoginModal.text021" className={styles.hint}>{uiText("components.settings.CodexLoginModal.text021")}</p>
                <form
                    className={styles.callbackRow}
                    onSubmit={e => {
                        e.preventDefault();
                        submitCallbackUrl();
                    }}
                >
                    <input
                        data-i18n="components.settings.CodexLoginModal.text027"
                        className={styles.callbackInput}
                        type="text"
                        inputMode="url"
                        autoComplete="off"
                        spellCheck={false}
                        placeholder="http://127.0.0.1:.../auth/callback?code=..."
                        aria-label={uiText("components.settings.CodexLoginModal.text027")}
                        value={callbackUrl}
                        onChange={e => setCallbackUrl(e.target.value)}
                    />
                    <button
                        data-i18n="components.settings.CodexLoginModal.text022 components.settings.CodexLoginModal.text025"
                        className={styles.copyBtn}
                        type="submit"
                        disabled={completeSubmitting || !callbackUrl.trim()}
                    >
                        {completeSubmitting
                            ? uiText("components.settings.CodexLoginModal.text025")
                            : uiText("components.settings.CodexLoginModal.text022")}
                    </button>
                </form>
                {completeError && (
                    <div className={styles.inlineError}>
                        <XCircle size={14} /> {completeError}
                    </div>
                )}
            </details>
        </>
    );

    const deviceWaiting = (
        <>
            <ol className={styles.steps}>
                <li data-i18n="components.settings.CodexLoginModal.text010">{uiText("components.settings.CodexLoginModal.text010")}<div>
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
                <li data-i18n="components.settings.CodexLoginModal.text011">{uiText("components.settings.CodexLoginModal.text011")}<div className={styles.codeRow}>
                        <span className={styles.userCode}>{status.user_code}</span>
                        <button data-i18n="components.settings.CodexLoginModal.text012 components.settings.CodexLoginModal.text013" className={styles.copyBtn} onClick={copyCode}>
                            {copied ? <CheckCircle size={14} /> : <Copy size={14} />}
                            {copied ? uiText("components.settings.CodexLoginModal.text012") : uiText("components.settings.CodexLoginModal.text013")}
                        </button>
                    </div>
                </li>
                <li data-i18n="components.settings.CodexLoginModal.text014">{uiText("components.settings.CodexLoginModal.text014")}</li>
            </ol>
            <div data-i18n="components.settings.CodexLoginModal.text015" className={styles.waitingRow}>
                <Loader size={14} className={styles.spinner} />{uiText("components.settings.CodexLoginModal.text015")}</div>
        </>
    );

    return (
        <ModalOverlay onClose={handleClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3 data-i18n="components.settings.CodexLoginModal.text003">{uiText("components.settings.CodexLoginModal.text003")}</h3>
                    <button data-i18n="components.settings.CodexLoginModal.text004" className={styles.closeBtn} onClick={handleClose} aria-label={uiText("components.settings.CodexLoginModal.text004")}>
                        <X size={18} />
                    </button>
                </div>
                <div className={styles.content}>
                    {startError ? (
                        <div className={styles.errorBox}>
                            <XCircle size={16} /> {startError}
                            <button data-i18n="components.settings.CodexLoginModal.text005" className={styles.retryBtn} onClick={() => startLogin(flow)}>{uiText("components.settings.CodexLoginModal.text005")}</button>
                        </div>
                    ) : status.state === 'success' ? (
                        <div className={styles.successBox}>
                            <CheckCircle size={20} />
                            <div>
                                <div data-i18n="components.settings.CodexLoginModal.text006" className={styles.successTitle}>{uiText("components.settings.CodexLoginModal.text006")}</div>
                                <div data-i18n="components.settings.CodexLoginModal.text007" className={styles.hint}>{uiText("components.settings.CodexLoginModal.text007")}</div>
                            </div>
                        </div>
                    ) : status.state === 'error' ? (
                        <div data-i18n="components.settings.CodexLoginModal.text008" className={styles.errorBox}>
                            <XCircle size={16} /> {status.error || uiText("components.settings.CodexLoginModal.text008")}
                            <button data-i18n="components.settings.CodexLoginModal.text009" className={styles.retryBtn} onClick={() => startLogin(flow)}>{uiText("components.settings.CodexLoginModal.text009")}</button>
                        </div>
                    ) : status.state === 'waiting' ? (
                        flow === 'siwc' ? siwcWaiting : deviceWaiting
                    ) : (
                        <div data-i18n="components.settings.CodexLoginModal.text016" className={styles.waitingRow}>
                            <Loader size={14} className={styles.spinner} />{uiText("components.settings.CodexLoginModal.text016")}</div>
                    )}
                    {status.state !== 'success' && (
                        <div className={styles.switchRow}>
                            {flow === 'siwc' ? (
                                <button data-i18n="components.settings.CodexLoginModal.text026" type="button" className={styles.switchLink} onClick={() => switchFlow('device')}>
                                    {uiText("components.settings.CodexLoginModal.text026")}
                                </button>
                            ) : (
                                <button data-i18n="components.settings.CodexLoginModal.text028" type="button" className={styles.switchLink} onClick={() => switchFlow('siwc')}>
                                    {uiText("components.settings.CodexLoginModal.text028")}
                                </button>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
