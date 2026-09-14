
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect } from 'react';
import { X, Save, User as UserIcon, AlertCircle } from 'lucide-react';
import styles from './UserProfileModal.module.css';
import ImageUpload from './common/ImageUpload';
import ModalOverlay from './common/ModalOverlay';

interface UserProfileModalProps {
    isOpen: boolean;
    onClose: () => void;
    currentName: string;
    currentAvatar: string | null;
    currentEmail?: string | null;
    onSaveSuccess: () => void;
}

export default function UserProfileModal({ isOpen, onClose, currentName, currentAvatar, currentEmail, onSaveSuccess }: UserProfileModalProps) {
    useLocale();
    const [name, setName] = useState(currentName);
    const [avatar, setAvatar] = useState(currentAvatar || "");
    const [email, setEmail] = useState(currentEmail || "");
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (isOpen) {
            setName(currentName);
            setAvatar(currentAvatar || "");
            setEmail(currentEmail || "");
            setError(null);
        }
    }, [isOpen, currentName, currentAvatar, currentEmail]);

    if (!isOpen) return null;

    const handleSave = async () => {
        if (!name.trim()) {
            setError(uiText("components.UserProfileModal.text001"));
            return;
        }

        setLoading(true);
        setError(null);
        try {
            const res = await apiFetch('/api/user/me', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    display_name: name,
                    avatar: avatar || null,
                    email: email || null
                })
            });

            if (res.ok) {
                onSaveSuccess();
                onClose();
            } else {
                const data = await res.json();
                setError(data.detail || uiText("components.UserProfileModal.text002"));
            }
        } catch (e) {
            console.error(e);
            setError(uiText("components.UserProfileModal.text003"));
        } finally {
            setLoading(false);
        }
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 data-i18n="components.UserProfileModal.text004" className={styles.title}>{uiText("components.UserProfileModal.text004")}</h2>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.content}>
                    <div className={styles.avatarPreview}>
                        <ImageUpload
                            value={avatar}
                            onChange={setAvatar}
                            circle={true}
                            width={110}
                            height={110}
                        />
                    </div>

                    <div className={styles.formGroup}>
                        <label data-i18n="components.UserProfileModal.text005" className={styles.label}>{uiText("components.UserProfileModal.text005")}</label>
                        <input data-i18n="components.UserProfileModal.text006"
                            type="text"
                            className={styles.input}
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder={uiText("components.UserProfileModal.text006")}
                        />
                    </div>

                    <div className={styles.formGroup}>
                        <label data-i18n="components.UserProfileModal.text007" className={styles.label}>{uiText("components.UserProfileModal.text007")}</label>
                        <input
                            type="email"
                            className={styles.input}
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            placeholder={uiText("components.UserProfileModal.label001")}
                        />
                    </div>

                    {error && (
                        <div className={styles.error}>
                            <AlertCircle size={16} />
                            <span>{error}</span>
                        </div>
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.UserProfileModal.text008" className={styles.cancelBtn} onClick={onClose} disabled={loading}>{uiText("components.UserProfileModal.text008")}</button>
                    <button data-i18n="components.UserProfileModal.text009 components.UserProfileModal.text010" className={styles.saveBtn} onClick={handleSave} disabled={loading}>
                        {loading ? uiText("components.UserProfileModal.text009") : (
                            <>
                                <Save size={16} />{uiText("components.UserProfileModal.text010")}</>
                        )}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
