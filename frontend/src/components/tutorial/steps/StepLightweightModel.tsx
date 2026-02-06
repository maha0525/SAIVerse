"use client";

import React from 'react';
import styles from './Steps.module.css';

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
}

interface StepLightweightModelProps {
    models: ModelInfo[];
    selected: string;
    onChange: (modelId: string) => void;
}

export default function StepLightweightModel({
    models,
    selected,
    onChange
}: StepLightweightModelProps) {
    // Sort models: available first, then alphabetically
    const sortedModels = [...models].sort((a, b) => {
        if (a.is_available !== b.is_available) {
            return a.is_available ? -1 : 1;
        }
        return a.display_name.localeCompare(b.display_name);
    });

    return (
        <div className={styles.modelSelectContainer}>
            <h3 className={styles.title}>軽量モデルの選択</h3>
            <p className={styles.subtitle}>
                ルーティングやツール利用の判断に使用する軽量モデルを選択してください。<br />
                安価で高速なモデルを推奨します。
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
