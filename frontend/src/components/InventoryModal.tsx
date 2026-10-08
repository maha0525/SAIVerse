
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, FileText, Image as ImageIcon, Box, RefreshCw } from 'lucide-react';
import styles from './InventoryModal.module.css';
import ModalOverlay from './common/ModalOverlay';
import ItemReferenceModal from './ItemReferenceModal';

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
    // Closing or changing persona unmounts the whole viewing session.
    return isOpen ? <InventoryContents key={personaId} personaId={personaId} onClose={onClose} /> : null;
}

function InventoryContents({ personaId, onClose }: Omit<InventoryModalProps, 'isOpen'>) {
    useLocale();
    const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
    const [error, setError] = useState(false);
    const requestRef = useRef(0);
    const [items, setItems] = useState<InventoryItem[]>([]);
    const [loading, setLoading] = useState(false);

    const loadItems = useCallback(async () => {
        const request = ++requestRef.current;
        setLoading(true);
        setError(false);
        try {
            const res = await apiFetch(`/api/people/${encodeURIComponent(personaId)}/items`);
            if (!res.ok) throw new Error(`Inventory lookup failed: ${res.status}`);
            const data: InventoryItem[] = await res.json();
            if (request === requestRef.current) setItems(data);
        } catch {
            if (request === requestRef.current) {
                setItems([]);
                setError(true);
            }
        } finally {
            if (request === requestRef.current) setLoading(false);
        }
    }, [personaId]);

    useEffect(() => {
        void loadItems();
        return () => { requestRef.current += 1; };
    }, [loadItems]);

    const getIcon = (type: string) => {
        switch (type) {
            case 'document': return <FileText size={20} />;
            case 'picture': return <ImageIcon size={20} />;
            default: return <Box size={20} />;
        }
    };

    if (selectedItemId !== null) {
        return <ItemReferenceModal key={selectedItemId} itemId={selectedItemId}
            onClose={() => setSelectedItemId(null)} readOnly />;
    }

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
                    ) : error ? (
                        <div className={styles.emptyState} role="alert">{uiText('components.InventoryModal.error')}</div>
                    ) : items.length === 0 ? (
                        <div data-i18n="components.InventoryModal.text005" className={styles.emptyState}>{uiText("components.InventoryModal.text005")}</div>
                    ) : (
                        <div className={styles.grid}>
                            {items.map(item => (
                                <button type="button" key={item.id} className={styles.card}
                                    onClick={() => setSelectedItemId(item.id)}>
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
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
