"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    useLocale();
    // Sort models: available first, then alphabetically
    const sortedModels = [...models].sort((a, b) => {
        if (a.is_available !== b.is_available) {
            return a.is_available ? -1 : 1;
        }
        return a.display_name.localeCompare(b.display_name);
    });

    return (
        <div className={styles.modelSelectContainer}>
            <h3 data-i18n="components.tutorial.steps.StepLightweightModel.text001" className={styles.title}>{uiText("components.tutorial.steps.StepLightweightModel.text001")}</h3>
            <p data-i18n="components.tutorial.steps.StepLightweightModel.text002 components.tutorial.steps.StepLightweightModel.text003" className={styles.subtitle}>{uiText("components.tutorial.steps.StepLightweightModel.text002")}<br />{uiText("components.tutorial.steps.StepLightweightModel.text003")}</p>

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
                            <div data-i18n="components.tutorial.steps.StepLightweightModel.text004" className={styles.modelUnavailable}>{uiText("components.tutorial.steps.StepLightweightModel.text004")}</div>
                        )}
                    </div>
                ))}
            </div>
        </div>
    );
}
