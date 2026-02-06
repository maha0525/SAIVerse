"use client";

import React from 'react';
import styles from './Steps.module.css';

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
}

interface StepDefaultModelProps {
    models: ModelInfo[];
    selected: string;
    onChange: (modelId: string) => void;
}

export default function StepDefaultModel({
    models,
    selected,
    onChange
}: StepDefaultModelProps) {
    // Sort models: available first, then alphabetically
    const sortedModels = [...models].sort((a, b) => {
        if (a.is_available !== b.is_available) {
            return a.is_available ? -1 : 1;
        }
        return a.display_name.localeCompare(b.display_name);
    });

    return (
        <div className={styles.modelSelectContainer}>
            <h3 className={styles.title}>標準モデルの選択</h3>
            <p className={styles.subtitle}>
                会話や複雑な推論に使用するメインモデルを選択してください。<br />
                APIキーが設定されていないモデルは選択できません。
            </p>

            <div className={styles.modelGrid}>
                {sortedModels.map((model) => (
                    <div
                        key={model.id}
                        className={`${styles.modelCard} ${selected === model.id ? styles.selected : ''} ${!model.is_available ? styles.disabled : ''}`}
                        onClick={() => model.is_available && onChange(model.id)}
                    >
                        <div className={styles.modelName}>{model.display_name}</div>
                        <div className={styles.modelProvider}>{model.provider}</div>
                        {!model.is_available && (
                            <div className={styles.modelUnavailable}>APIキー未設定</div>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
