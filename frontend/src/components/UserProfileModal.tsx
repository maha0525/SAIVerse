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
            setError("名前を入力してください");
            return;
        }

        setLoading(true);
        setError(null);
        try {
            const res = await fetch('/api/user/me', {
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
                setError(data.detail || "プロフィールの更新に失敗しました");
            }
        } catch (e) {
            console.error(e);
            setError("ネットワークエラー");
        } finally {
            setLoading(false);
        }
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>プロフィール編集</h2>
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
                        <label className={styles.label}>表示名</label>
                        <input
                            type="text"
                            className={styles.input}
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="表示名を入力"
                        />
                    </div>

                    <div className={styles.formGroup}>
                        <label className={styles.label}>メールアドレス</label>
                        <input
                            type="email"
                            className={styles.input}
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            placeholder="user@example.com"
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
                    <button className={styles.cancelBtn} onClick={onClose} disabled={loading}>
                        キャンセル
                    </button>
                    <button className={styles.saveBtn} onClick={handleSave} disabled={loading}>
                        {loading ? "保存中..." : (
                            <>
                                <Save size={16} /> 保存
                            </>
                        )}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
