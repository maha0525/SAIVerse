"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from 'react';
import styles from './Steps.module.css';

interface StepUserNameProps {
    value: string;
    onChange: (value: string) => void;
}

export default function StepUserName({ value, onChange }: StepUserNameProps) {
    useLocale();
    return (
        <div className={styles.formContainer}>
            <h3 data-i18n="components.tutorial.steps.StepUserName.text001" className={styles.title}>{uiText("components.tutorial.steps.StepUserName.text001")}</h3>
            <p data-i18n="components.tutorial.steps.StepUserName.text002" className={styles.subtitle}>{uiText("components.tutorial.steps.StepUserName.text002")}</p>

            <div className={styles.field}>
                <label data-i18n="components.tutorial.steps.StepUserName.text003">{uiText("components.tutorial.steps.StepUserName.text003")}</label>
                <input data-i18n="components.tutorial.steps.StepUserName.text004"
                    type="text"
                    value={value}
                    onChange={(e) => onChange(e.target.value)}
                    placeholder={uiText("components.tutorial.steps.StepUserName.text004")}
                    autoFocus
                />
                <p data-i18n="components.tutorial.steps.StepUserName.text005" className={styles.fieldHint}>{uiText("components.tutorial.steps.StepUserName.text005")}</p>
            </div>
        </div>
    );
}
