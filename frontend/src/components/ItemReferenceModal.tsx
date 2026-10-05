'use client';

import { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import { apiFetch } from '@/i18n/api';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import ItemModal from './ItemModal';
import ModalOverlay from './common/ModalOverlay';
import styles from './ItemModal.module.css';

interface ItemReferenceModalProps {
    itemId: string;
    onClose: () => void;
    currentBuildingId?: string | null;
    readOnly?: boolean;
}

interface ResolvedItem {
    id: string;
    name: string;
    type: string;
    description?: string;
}

/** Resolve identity before choosing a renderer: pictures return bytes, not JSON. */
export default function ItemReferenceModal({ itemId, onClose, currentBuildingId, readOnly = false }: ItemReferenceModalProps) {
    useLocale();
    const [result, setResult] = useState<{ key: string; item?: ResolvedItem; failed?: boolean } | null>(null);

    useEffect(() => {
        const controller = new AbortController();
        let active = true;
        setResult(null);
        async function load() {
            try {
                const response = await apiFetch(`/api/world/items/${encodeURIComponent(itemId)}`, { signal: controller.signal });
                if (!response.ok) throw new Error(`Item lookup failed: ${response.status}`);
                const details = await response.json();
                if (!active) return;
                setResult({ key: itemId, item: {
                    id: details.ITEM_ID,
                    name: details.NAME,
                    type: details.TYPE,
                    description: details.DESCRIPTION,
                } });
            } catch {
                if (active) setResult({ key: itemId, failed: true });
            }
        }
        void load();
        return () => {
            active = false;
            controller.abort();
        };
    }, [itemId]);

    const current = result?.key === itemId ? result : null;
    if (current?.item) {
        return <ItemModal key={current.item.id} isOpen item={current.item} onClose={onClose}
            currentBuildingId={currentBuildingId} readOnly={readOnly} />;
    }
    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={event => event.stopPropagation()}>
                <div className={styles.header}>
                    <h2>{itemId}</h2>
                    <button className={styles.closeBtn} onClick={onClose}
                        aria-label={uiText('components.ItemReferenceModal.close')}><X size={24} /></button>
                </div>
                <div className={current?.failed ? styles.error : styles.loading} role={current?.failed ? 'alert' : 'status'}>
                    {current?.failed ? uiText('components.ItemReferenceModal.error') : uiText('components.ItemReferenceModal.loading')}
                </div>
            </div>
        </ModalOverlay>
    );
}
