'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { BookOpen } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './ChronicleConfirmDialog.module.css';

export interface ChronicleConfirmData {
    requestId: string;
    unprocessedMessages: number;
    totalMessages: number;
    estimatedLlmCalls: number;
    modelName: string;
    personaName: string;
}

interface ChronicleConfirmDialogProps {
    request: ChronicleConfirmData;
    onRespond: (requestId: string, decision: string) => void;
}

const TIMEOUT_SEC = 60;

export default function ChronicleConfirmDialog({ request, onRespond }: ChronicleConfirmDialogProps) {
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
                        <BookOpen size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3>Chronicle生成の確認</h3>
                        <p>{request.personaName} の記憶を整理します</p>
                    </div>
                </div>

                <div className={styles.body}>
                    <div className={styles.info}>
                        <div className={styles.infoRow}>
                            <span className={styles.infoLabel}>未処理メッセージ</span>
                            <span className={styles.infoValue}>{request.unprocessedMessages.toLocaleString()} 件</span>
                        </div>
                        <div className={styles.infoRow}>
                            <span className={styles.infoLabel}>推定LLM呼び出し</span>
                            <span className={styles.infoValue}>{request.estimatedLlmCalls} 回</span>
                        </div>
                        <div className={styles.infoRow}>
                            <span className={styles.infoLabel}>モデル</span>
                            <span className={styles.infoValue}>{request.modelName}</span>
                        </div>
                    </div>
                    <div className={`${styles.timer} ${remaining <= 10 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? `${remaining}秒後に自動スキップ` : 'タイムアウト...'}
                    </div>
                </div>

                <div className={styles.actions}>
                    <button className={styles.generateBtn} onClick={() => respond('allow')}>
                        生成する
                    </button>
                    <button className={styles.skipBtn} onClick={() => respond('deny')}>
                        スキップ
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
