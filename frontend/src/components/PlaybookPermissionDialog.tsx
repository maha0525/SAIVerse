'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Shield } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './PlaybookPermissionDialog.module.css';

export interface PermissionRequestData {
    requestId: string;
    playbookName: string;
    playbookDisplayName: string;
    playbookDescription: string;
    personaName: string;
}

interface PlaybookPermissionDialogProps {
    request: PermissionRequestData;
    onRespond: (requestId: string, decision: string) => void;
}

const TIMEOUT_SEC = 60;

export default function PlaybookPermissionDialog({ request, onRespond }: PlaybookPermissionDialogProps) {
    const [remaining, setRemaining] = useState(TIMEOUT_SEC);

    useEffect(() => {
        setRemaining(TIMEOUT_SEC);
        const interval = setInterval(() => {
            setRemaining(prev => {
                if (prev <= 1) {
                    clearInterval(interval);
                    return 0;
                }
                return prev - 1;
            });
        }, 1000);
        return () => clearInterval(interval);
    }, [request.requestId]);

    const respond = useCallback((decision: string) => {
        onRespond(request.requestId, decision);
    }, [request.requestId, onRespond]);

    return (
        <ModalOverlay onClose={() => respond('deny')}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.icon}>
                        <Shield size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3>Playbook実行の確認</h3>
                        <p>{request.personaName} が実行しようとしています</p>
                    </div>
                </div>

                <div className={styles.body}>
                    <p className={styles.description}>
                        <strong>{request.playbookDisplayName}</strong>
                        {request.playbookDescription && (
                            <><br />{request.playbookDescription}</>
                        )}
                    </p>
                    <div className={`${styles.timer} ${remaining <= 10 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? `${remaining}秒後に自動拒否` : 'タイムアウト...'}
                    </div>
                </div>

                <div className={styles.actions}>
                    <div className={styles.primaryRow}>
                        <button className={styles.allowBtn} onClick={() => respond('allow')}>
                            許可
                        </button>
                        <button className={styles.denyBtn} onClick={() => respond('deny')}>
                            拒否
                        </button>
                    </div>
                    <div className={styles.secondaryRow}>
                        <button onClick={() => respond('always_allow')}>
                            常に許可する
                        </button>
                        {/* 書き込まれる値は user_only (設定画面の表示は「ユーザー指定時のみ」)。
                            禁止の対象はペルソナであってユーザー本人ではないので、文面もそう書く。
                            「この」は付けない — 設定は City 単位で保存され、同じ街の全ペルソナに
                            効くため、目の前の一人だけに見える書き方は嘘になる (まはー指摘 2026-08-17)。 */}
                        <button onClick={() => respond('never_use')}>
                            ペルソナには使わせない
                        </button>
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
