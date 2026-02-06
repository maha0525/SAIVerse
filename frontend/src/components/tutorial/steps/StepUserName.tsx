"use client";

import React from 'react';
import styles from './Steps.module.css';

interface StepUserNameProps {
    value: string;
    onChange: (value: string) => void;
}

export default function StepUserName({ value, onChange }: StepUserNameProps) {
    return (
        <div className={styles.formContainer}>
            <h3 className={styles.title}>あなたの名前を教えてください</h3>
            <p className={styles.subtitle}>
                ペルソナがあなたを呼ぶときに使う名前です
            </p>

            <div className={styles.field}>
                <label>ユーザー名</label>
                <input
                    type="text"
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder="例: バース"
                    autoFocus
                />
                <p className={styles.fieldHint}>
                    スキップした場合は「ユーザー」になります
                </p>
            </div>
        </div>
    );
}
