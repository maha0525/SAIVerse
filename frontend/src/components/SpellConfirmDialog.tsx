'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { ShieldCheck } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './SpellConfirmDialog.module.css';

export interface SpellConfirmData {
    requestId: string;
    title: string;
    body: string;
    editable?: boolean;
    text?: string;
    addon?: string;
    confirmText?: string;
    maxChars?: number;
}

interface SpellConfirmDialogProps {
    request: SpellConfirmData;
    onRespond: (requestId: string, decision: string, editedText?: string) => void;
}

const TIMEOUT_SEC = 120;

export default function SpellConfirmDialog({ request, onRespond }: SpellConfirmDialogProps) {
    const editable = !!request.editable;
    const initialText = request.text ?? '';
    const [remaining, setRemaining] = useState(TIMEOUT_SEC);
    const [text, setText] = useState(initialText);

    const isEdited = editable && text !== initialText;
    const overLimit = editable && typeof request.maxChars === 'number' && text.length > request.maxChars;
    const empty = editable && text.length === 0;

    useEffect(() => {
        setText(request.text ?? '');
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
    }, [request.requestId, request.text]);

    const respond = useCallback((decision: string, editedText?: string) => {
        onRespond(request.requestId, decision, editedText);
    }, [request.requestId, onRespond]);

    const confirmLabel = request.confirmText ?? (isEdited ? '編集して実行' : '実行する');

    return (
        <ModalOverlay onClose={() => respond('reject')}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.icon}>
                        <ShieldCheck size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3>{request.title}</h3>
                        {request.addon && <p>{request.addon}</p>}
                    </div>
                </div>

                <div className={styles.body}>
                    <p className={styles.message}>{request.body}</p>
                    {editable && (
                        <>
                            <textarea
                                className={`${styles.editInput} ${overLimit ? styles.overLimit : ''}`}
                                value={text}
                                onChange={e => setText(e.target.value)}
                                rows={4}
                            />
                            {typeof request.maxChars === 'number' && (
                                <div className={styles.charCount}>
                                    <span className={overLimit ? styles.overLimit : ''}>
                                        {text.length}/{request.maxChars}
                                    </span>
                                </div>
                            )}
                        </>
                    )}
                    <div className={`${styles.timer} ${remaining <= 15 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? `${remaining}秒後に自動キャンセル` : 'タイムアウト...'}
                    </div>
                </div>

                <div className={styles.actions}>
                    <button
                        className={styles.confirmBtn}
                        onClick={() => (isEdited ? respond('edit', text) : respond('approve'))}
                        disabled={overLimit || empty}
                    >
                        {confirmLabel}
                    </button>
                    <button className={styles.cancelBtn} onClick={() => respond('reject')}>
                        キャンセル
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
