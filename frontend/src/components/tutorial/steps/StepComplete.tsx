"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from 'react';
import { CheckCircle, MessageSquare } from 'lucide-react';
import styles from './Steps.module.css';

interface StepCompleteProps {
    onStart: () => void;
}

export default function StepComplete({ onStart }: StepCompleteProps) {
    useLocale();
    return (
        <div className={styles.completeContainer}>
            <CheckCircle size={64} className={styles.successIcon} />

            <h2 data-i18n="components.tutorial.steps.StepComplete.text001" className={styles.completeTitle}>{uiText("components.tutorial.steps.StepComplete.text001")}</h2>

            <p data-i18n="components.tutorial.steps.StepComplete.text002 components.tutorial.steps.StepComplete.text003 components.tutorial.steps.StepComplete.text004 components.tutorial.steps.StepComplete.text005" className={styles.completeSubtitle}>{uiText("components.tutorial.steps.StepComplete.text002")}<br />{uiText("components.tutorial.steps.StepComplete.text003")}<br /><br />{uiText("components.tutorial.steps.StepComplete.text004")}<br />{uiText("components.tutorial.steps.StepComplete.text005")}</p>

            <button data-i18n="components.tutorial.steps.StepComplete.text006" className={styles.startButton} onClick={onStart}>
                <MessageSquare size={20} />{uiText("components.tutorial.steps.StepComplete.text006")}</button>
        </div>
    );
}
