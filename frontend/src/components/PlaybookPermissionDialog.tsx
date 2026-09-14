'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState, useEffect, useCallback } from 'react';
import { Shield } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './PlaybookPermissionDialog.module.css';

export interface PermissionRequestData {
    requestId: string;
    playbookName: string;
    playbookDisplayName: string;
    playbookDescription: string;
    personaName: string;
}

interface PlaybookPermissionDialogProps {
    request: PermissionRequestData;
    onRespond: (requestId: string, decision: string) => void;
}

const TIMEOUT_SEC = 60;

export default function PlaybookPermissionDialog({ request, onRespond }: PlaybookPermissionDialogProps) {
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
                        <Shield size={18} />
                    </div>
                    <div className={styles.headerText}>
                        <h3 data-i18n="components.PlaybookPermissionDialog.text001">{uiText("components.PlaybookPermissionDialog.text001")}</h3>
                        <p data-i18n="components.PlaybookPermissionDialog.text002">{request.personaName}{uiText("components.PlaybookPermissionDialog.text002")}</p>
                    </div>
                </div>

                <div className={styles.body}>
                    <p className={styles.description}>
                        <strong>{request.playbookDisplayName}</strong>
                        {request.playbookDescription && (
                            <><br />{request.playbookDescription}</>
                        )}
                    </p>
                    <div data-i18n="components.PlaybookPermissionDialog.text003 components.PlaybookPermissionDialog.text004" className={`${styles.timer} ${remaining <= 10 ? styles.timerWarn : ''}`}>
                        {remaining > 0 ? uiText("components.PlaybookPermissionDialog.text003", { p1: remaining }) : uiText("components.PlaybookPermissionDialog.text004")}
                    </div>
                </div>

                <div className={styles.actions}>
                    <div className={styles.primaryRow}>
                        <button data-i18n="components.PlaybookPermissionDialog.text005" className={styles.allowBtn} onClick={() => respond('allow')}>{uiText("components.PlaybookPermissionDialog.text005")}</button>
                        <button data-i18n="components.PlaybookPermissionDialog.text006" className={styles.denyBtn} onClick={() => respond('deny')}>{uiText("components.PlaybookPermissionDialog.text006")}</button>
                    </div>
                    <div className={styles.secondaryRow}>
                        <button data-i18n="components.PlaybookPermissionDialog.text007" onClick={() => respond('always_allow')}>{uiText("components.PlaybookPermissionDialog.text007")}</button>
                        {/* 書き込まれる値は user_only (設定画面の表示は「ユーザー指定時のみ」)。
                            禁止の対象はペルソナであってユーザー本人ではないので、文面もそう書く。
                            「この」は付けない — 設定は City 単位で保存され、同じ街の全ペルソナに
                            効くため、目の前の一人だけに見える書き方は嘘になる (まはー指摘 2026-08-17)。 */}
                        <button data-i18n="components.PlaybookPermissionDialog.text008" onClick={() => respond('never_use')}>{uiText("components.PlaybookPermissionDialog.text008")}</button>
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
