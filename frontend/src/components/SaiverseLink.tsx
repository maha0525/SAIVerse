'use client';

import React, { useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import ContentViewerModal from './ContentViewerModal';
import styles from './SaiverseLink.module.css';

/** Extract item_id from saiverse://item/{item_id}/... URI. Returns null if not an item URI. */
function extractItemId(uri: string): string | null {
    const match = uri.match(/^saiverse:\/\/item\/([^/]+)/);
    return match ? match[1] : null;
}

interface SaiverseLinkProps {
    href?: string;
    children?: React.ReactNode;
    personaId?: string;
    /** Callback to open ItemModal for item URIs. Called with item_id. */
    onOpenItem?: (itemId: string) => void;
}

/**
 * Custom link renderer for ReactMarkdown.
 * - saiverse://item/{id}/... → opens existing ItemModal via onOpenItem callback
 * - other saiverse:// URIs → opens ContentViewerModal
 * - regular URLs → normal <a> tag
 */
export default function SaiverseLink({ href, children, personaId, onOpenItem }: SaiverseLinkProps) {
    const [isModalOpen, setIsModalOpen] = useState(false);

    const handleClick = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        if (!href) return;

        const itemId = extractItemId(href);
        if (itemId && onOpenItem) {
            onOpenItem(itemId);
        } else {
            setIsModalOpen(true);
        }
    }, [href, onOpenItem]);

    if (!href || !href.startsWith('saiverse://')) {
        return (
            <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
            </a>
        );
    }

    return (
        <>
            <a
                href={href}
                onClick={handleClick}
                className={styles.saiverseLink}
                title={href}
            >
                {children}
            </a>
            {isModalOpen && createPortal(
                <ContentViewerModal
                    isOpen={isModalOpen}
                    onClose={() => setIsModalOpen(false)}
                    uri={href}
                    personaId={personaId}
                />,
                document.body
            )}
        </>
    );
}
