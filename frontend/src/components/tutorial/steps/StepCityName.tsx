"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from 'react';
import styles from './Steps.module.css';

interface StepCityNameProps {
    value: string;
    onChange: (value: string) => void;
    timezone?: string;
    onTimezoneChange?: (value: string) => void;
    language?: string;
    onLanguageChange?: (value: string) => void;
}

export default function StepCityName({
    value,
    onChange,
    timezone,
    onTimezoneChange,
    language,
    onLanguageChange
}: StepCityNameProps) {
    useLocale();
    return (
        <div className={styles.formContainer}>
            <h3 data-i18n="components.tutorial.steps.StepCityName.text001" className={styles.title}>{uiText("components.tutorial.steps.StepCityName.text001")}</h3>
            <p data-i18n="components.tutorial.steps.StepCityName.text002" className={styles.subtitle}>{uiText("components.tutorial.steps.StepCityName.text002")}</p>

            <div className={styles.field}>
                <label data-i18n="components.tutorial.steps.StepCityName.text003">{uiText("components.tutorial.steps.StepCityName.text003")}</label>
                <input data-i18n="components.tutorial.steps.StepCityName.text004"
                    type="text"
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder={uiText("components.tutorial.steps.StepCityName.text004")}
                    autoFocus
                />
                <p data-i18n="components.tutorial.steps.StepCityName.text005" className={styles.fieldHint}>{uiText("components.tutorial.steps.StepCityName.text005")}</p>
            </div>

            {onLanguageChange && (
                <div className={styles.field}>
                    <label data-i18n="components.tutorial.steps.StepCityName.language">{uiText("components.tutorial.steps.StepCityName.language")}</label>
                    <select
                        value={language || 'ja'}
                        onChange={(e) => onLanguageChange(e.target.value)}
                        className={styles.select}
                    >
                        <option data-i18n="components.tutorial.steps.StepCityName.languageJa" value="ja">{uiText("components.tutorial.steps.StepCityName.languageJa")}</option>
                        <option value="en">English</option>
                    </select>
                    <p data-i18n="components.tutorial.steps.StepCityName.languageHint" className={styles.fieldHint}>{uiText("components.tutorial.steps.StepCityName.languageHint")}</p>
                </div>
            )}

            {onTimezoneChange && (
                <div className={styles.field}>
                    <label data-i18n="components.tutorial.steps.StepCityName.text006">{uiText("components.tutorial.steps.StepCityName.text006")}</label>
                    <input data-i18n="components.tutorial.steps.StepCityName.text007"
                        type="text"
                        value={timezone || ''}
                        onChange={(e) => onTimezoneChange(e.target.value)}
                        placeholder={uiText("components.tutorial.steps.StepCityName.text007")}
                    />
                    <p data-i18n="components.tutorial.steps.StepCityName.text008" className={styles.fieldHint}>{uiText("components.tutorial.steps.StepCityName.text008")}</p>
                </div>
            )}
        </div>
    );
}
