'use client';

import React, { useState, useEffect, useCallback } from 'react';
import { Send } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './TweetConfirmDialog.module.css';

export interface TweetConfirmData {
    requestId: string;
    tweetText: string;
    personaId: string;
    xUsername: string;
}

interface TweetConfirmDialogProps {
    request: TweetConfirmData;
    onRespond: (requestId: string, decision: string, editedText?: string) => void;
}

const TIMEOUT_SEC = 120;
const MAX_CHARS = 280;

export default function TweetConfirmDialog({ request, onRespond }: TweetConfirmDialogProps) {
    const [remaining, setRemaining] = useState(TIMEOUT_SEC);
    const [text, setText] = useState(request.tweetText);
    const isEdited = text !== request.tweetText;
    const overLimit = text.length > MAX_CHARS;

    useEffect(() => {
        setText(request.tweetText);
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
    }, [request.requestId, request.tweetText]);

    const respond = useCallback((decision: string, editedText?: string) => {
        onRespond(request.requestId, decision, editedText);
    }, [request.requestId, onRespond]);

    return (
        <ModalOverlay onClose={() => respond('reject')}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.icon}>
                        <Send size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3>ツイート投稿の確認</h3>
                        <p>@{request.xUsername} として投稿します</p>
                    </div>
                </div>

                <div className={styles.body}>
                    <textarea
                        className={`${styles.tweetInput} ${overLimit ? styles.overLimit : ''}`}
                        value={text}
                        onChange={e => setText(e.target.value)}
                        rows={4}
                    />
                    <div className={styles.charCount}>
                        <span className={overLimit ? styles.overLimit : ''}>
                            {text.length}/{MAX_CHARS}
                        </span>
                    </div>
                    <div className={`${styles.timer} ${remaining <= 15 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? `${remaining}秒後に自動キャンセル` : 'タイムアウト...'}
                    </div>
                </div>

                <div className={styles.actions}>
                    <button
                        className={styles.postBtn}
                        onClick={() => isEdited ? respond('edit', text) : respond('approve')}
                        disabled={overLimit || text.length === 0}
                    >
                        {isEdited ? '編集して投稿' : '投稿する'}
                    </button>
                    <button className={styles.cancelBtn} onClick={() => respond('reject')}>
                        キャンセル
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
