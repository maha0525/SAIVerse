"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from 'react';
import styles from './Steps.module.css';

export default function StepWelcome() {
    useLocale();
    return (
        <div className={styles.welcomeContainer}>
            {/* Logo placeholder - can be replaced with actual logo */}
            <div className={styles.logoArea}>
                <img
                    src="/api/media/icon/saiverse-logo.png"
                    alt={uiText("components.tutorial.steps.StepWelcome.label001")}
                    className={styles.logo}
                    onError={(e) => {
                        // Hide if logo not found
                        (e.target as HTMLImageElement).style.display = 'none';
                    }}
                />
            </div>

            <h2 data-i18n="components.tutorial.steps.StepWelcome.text001" className={styles.welcomeTitle}>{uiText("components.tutorial.steps.StepWelcome.text001")}</h2>

            <div className={styles.descriptionBox}>
                <p data-i18n="components.tutorial.steps.StepWelcome.text002 components.tutorial.steps.StepWelcome.text002Suffix">{uiText("components.tutorial.steps.StepWelcome.text002")}<strong>{uiText("components.tutorial.steps.StepWelcome.label002")}</strong>{uiText("components.tutorial.steps.StepWelcome.text002Suffix")}</p>
                <p data-i18n="components.tutorial.steps.StepWelcome.text003">{uiText("components.tutorial.steps.StepWelcome.text003")}</p>
                <br />
                <p data-i18n="components.tutorial.steps.StepWelcome.text004 components.tutorial.steps.StepWelcome.text006">{uiText("components.tutorial.steps.StepWelcome.text004")}<strong data-i18n="components.tutorial.steps.StepWelcome.text005">{uiText("components.tutorial.steps.StepWelcome.text005")}</strong>{uiText("components.tutorial.steps.StepWelcome.text006")}</p>
                <p data-i18n="components.tutorial.steps.StepWelcome.text007">{uiText("components.tutorial.steps.StepWelcome.text007")}</p>
                <br />
                <p data-i18n="components.tutorial.steps.StepWelcome.text008 components.tutorial.steps.StepWelcome.text010">{uiText("components.tutorial.steps.StepWelcome.text008")}<strong data-i18n="components.tutorial.steps.StepWelcome.text009">{uiText("components.tutorial.steps.StepWelcome.text009")}</strong>{uiText("components.tutorial.steps.StepWelcome.text010")}</p>
                <p data-i18n="components.tutorial.steps.StepWelcome.text011">{uiText("components.tutorial.steps.StepWelcome.text011")}</p>
                <p data-i18n="components.tutorial.steps.StepWelcome.text012">{uiText("components.tutorial.steps.StepWelcome.text012")}</p>
            </div>

            <p data-i18n="components.tutorial.steps.StepWelcome.text013" className={styles.hint}>{uiText("components.tutorial.steps.StepWelcome.text013")}</p>
        </div>
    );
}
