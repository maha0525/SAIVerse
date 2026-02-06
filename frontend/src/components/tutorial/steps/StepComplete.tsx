"use client";

import React from 'react';
import { CheckCircle, MessageSquare } from 'lucide-react';
import styles from './Steps.module.css';

interface StepCompleteProps {
    onStart: () => void;
}

export default function StepComplete({ onStart }: StepCompleteProps) {
    return (
        <div className={styles.completeContainer}>
            <CheckCircle size={64} className={styles.successIcon} />

            <h2 className={styles.completeTitle}>セットアップ完了!</h2>

            <p className={styles.completeSubtitle}>
                基本的な設定が完了しました。<br />
                ペルソナとの生活をお楽しみください。<br /><br />
                SAIVerseにはその他にも様々な機能があります。<br />
                知りたい場合は、サイドバーの「チュートリアル」ボタンから選択してください。
            </p>

            <button className={styles.startButton} onClick={onStart}>
                <MessageSquare size={20} />
                始める
            </button>
        </div>
    );
}
