
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect } from 'react';
import { X, Package, FileText, Image as ImageIcon, Box, RefreshCw } from 'lucide-react';
import styles from './InventoryModal.module.css';
import ModalOverlay from './common/ModalOverlay';

interface InventoryItem {
    id: string;
    name: string;
    type: string;
    description: string;
    file_path?: string;
    created_at: string;
}

interface InventoryModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

export default function InventoryModal({ isOpen, onClose, personaId }: InventoryModalProps) {
    useLocale();
    const [items, setItems] = useState<InventoryItem[]>([]);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (isOpen) {
            loadItems();
        }
    }, [isOpen, personaId]);

    const loadItems = async () => {
        setLoading(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/items`);
            if (res.ok) {
                setItems(await res.json());
            }
        } catch (e) {
            console.error(e);
        } finally {
            setLoading(false);
        }
    };

    const getIcon = (type: string) => {
        switch (type) {
            case 'document': return <FileText size={20} />;
            case 'picture': return <ImageIcon size={20} />;
            default: return <Box size={20} />;
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 data-i18n="components.InventoryModal.text001" className={styles.title}>{uiText("components.InventoryModal.text001")}{personaId}</h2>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.content}>
                    <div className={styles.toolbar}>
                        <span data-i18n="components.InventoryModal.text002" className={styles.count}>{items.length}{uiText("components.InventoryModal.text002")}</span>
                        <button data-i18n="components.InventoryModal.text003" className={styles.refreshBtn} onClick={loadItems}>
                            <RefreshCw size={14} />{uiText("components.InventoryModal.text003")}</button>
                    </div>

                    {loading ? (
                        <div data-i18n="components.InventoryModal.text004" className={styles.loading}>{uiText("components.InventoryModal.text004")}</div>
                    ) : items.length === 0 ? (
                        <div data-i18n="components.InventoryModal.text005" className={styles.emptyState}>{uiText("components.InventoryModal.text005")}</div>
                    ) : (
                        <div className={styles.grid}>
                            {items.map(item => (
                                <div key={item.id} className={styles.card}>
                                    <div className={styles.iconWrapper}>
                                        {getIcon(item.type)}
                                    </div>
                                    <div className={styles.details}>
                                        <div className={styles.itemName}>{item.name}</div>
                                        <div className={styles.itemType}>{item.type}</div>
                                        {item.description && (
                                            <div className={styles.itemDesc} title={item.description}>{item.description}</div>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
