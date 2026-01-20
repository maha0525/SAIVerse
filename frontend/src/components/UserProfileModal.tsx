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
            setError("Name cannot be empty");
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
                setError(data.detail || "Failed to update profile");
            }
        } catch (e) {
            console.error(e);
            setError("Network error");
        } finally {
            setLoading(false);
        }
    };

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>Edit Profile</h2>
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
                        <label className={styles.label}>Display Name</label>
                        <input
                            type="text"
                            className={styles.input}
                            value={name}
                            onChange={(e) => setName(e.target.value)}
                            placeholder="Enter display name"
                        />
                    </div>

                    <div className={styles.formGroup}>
                        <label className={styles.label}>Email Address</label>
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
                        Cancel
                    </button>
                    <button className={styles.saveBtn} onClick={handleSave} disabled={loading}>
                        {loading ? "Saving..." : (
                            <>
                                <Save size={16} /> Save Changes
                            </>
                        )}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
