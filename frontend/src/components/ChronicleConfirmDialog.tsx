'use client';
import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState, useEffect, useCallback } from 'react';
import { BookOpen } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './ChronicleConfirmDialog.module.css';

export interface ChronicleConfirmData {
    requestId: string;
    unprocessedMessages: number;
    totalMessages: number;
    estimatedLlmCalls: number;
    modelName: string;
    personaName: string;
}

interface ChronicleConfirmDialogProps {
    request: ChronicleConfirmData;
    onRespond: (requestId: string, decision: string) => void;
}

const TIMEOUT_SEC = 60;

export default function ChronicleConfirmDialog({ request, onRespond }: ChronicleConfirmDialogProps) {
    useLocale();
    const [remaining, setRemaining] = useState(TIMEOUT_SEC);

    useEffect(() => {
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
    }, [request.requestId]);

    const respond = useCallback((decision: string) => {
        onRespond(request.requestId, decision);
    }, [request.requestId, onRespond]);

    return (
        <ModalOverlay onClose={() => respond('deny')}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.icon}>
                        <BookOpen size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3 data-i18n="components.ChronicleConfirmDialog.text001">{uiText("components.ChronicleConfirmDialog.text001")}</h3>
                        <p data-i18n="components.ChronicleConfirmDialog.text002">{request.personaName}{uiText("components.ChronicleConfirmDialog.text002")}</p>
                    </div>
                </div>

                <div className={styles.body}>
                    <div className={styles.info}>
                        <div className={styles.infoRow}>
                            <span data-i18n="components.ChronicleConfirmDialog.text003" className={styles.infoLabel}>{uiText("components.ChronicleConfirmDialog.text003")}</span>
                            <span data-i18n="components.ChronicleConfirmDialog.text004" className={styles.infoValue}>{request.unprocessedMessages.toLocaleString(getFormatLocale())}{uiText("components.ChronicleConfirmDialog.text004")}</span>
                        </div>
                        <div className={styles.infoRow}>
                            <span data-i18n="components.ChronicleConfirmDialog.text005" className={styles.infoLabel}>{uiText("components.ChronicleConfirmDialog.text005")}</span>
                            <span data-i18n="components.ChronicleConfirmDialog.text006" className={styles.infoValue}>{request.estimatedLlmCalls}{uiText("components.ChronicleConfirmDialog.text006")}</span>
                        </div>
                        <div className={styles.infoRow}>
                            <span data-i18n="components.ChronicleConfirmDialog.text007" className={styles.infoLabel}>{uiText("components.ChronicleConfirmDialog.text007")}</span>
                            <span className={styles.infoValue}>{request.modelName}</span>
                        </div>
                    </div>
                    <div data-i18n="components.ChronicleConfirmDialog.text008 components.ChronicleConfirmDialog.text009" className={`${styles.timer} ${remaining <= 10 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? uiText("components.ChronicleConfirmDialog.text008", { p1: remaining }) : uiText("components.ChronicleConfirmDialog.text009")}
                    </div>
                </div>

                <div className={styles.actions}>
                    <button data-i18n="components.ChronicleConfirmDialog.text010" className={styles.generateBtn} onClick={() => respond('allow')}>{uiText("components.ChronicleConfirmDialog.text010")}</button>
                    <button data-i18n="components.ChronicleConfirmDialog.text011" className={styles.skipBtn} onClick={() => respond('deny')}>{uiText("components.ChronicleConfirmDialog.text011")}</button>
                </div>
            </div>
        </ModalOverlay>
    );
}
