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

/**
 * Extract building_id from a BARE saiverse://building/{building_id} URI.
 * Returns null for any other form — `saiverse://building/{id}/items` や
 * `/history?last=N` は資源 (中身を見るリンク) なので、従来どおり
 * ContentViewerModal 側に流す。表示切り替えになるのはサブパス無しの形だけ
 * (docs/reference/saiverse-uri.md)。
 */
function extractBuildingId(uri: string): string | null {
    const match = uri.match(/^saiverse:\/\/building\/([^/?#]+)\/?$/);
    if (!match) return null;
    try {
        return decodeURIComponent(match[1]);
    } catch {
        return match[1];
    }
}

interface SaiverseLinkProps {
    href?: string;
    children?: React.ReactNode;
    personaId?: string;
    /** Callback to open ItemModal for item URIs. Called with item_id. */
    onOpenItem?: (itemId: string) => void;
    /**
     * Callback for building URIs (移動イベントの行き先の部屋名リンク).
     * Called with building_id. 渡されなかった画面では building リンクは何もしない
     * (部屋は「中身を見るもの」ではなく「表示を切り替えるもの」なので、
     * ContentViewerModal の対象外)。
     */
    onNavigateBuilding?: (buildingId: string) => void;
}

/**
 * Custom link renderer for ReactMarkdown.
 * - saiverse://item/{id}/... → opens existing ItemModal via onOpenItem callback
 * - saiverse://building/{id} → switches the displayed building via onNavigateBuilding
 * - other saiverse:// URIs → opens ContentViewerModal
 * - regular URLs → normal <a> tag
 */
export default function SaiverseLink({ href, children, personaId, onOpenItem, onNavigateBuilding }: SaiverseLinkProps) {
    const [isModalOpen, setIsModalOpen] = useState(false);

    const handleClick = useCallback((e: React.MouseEvent) => {
        e.preventDefault();
        if (!href) return;

        const buildingId = extractBuildingId(href);
        if (buildingId) {
            // 部屋の URI は閲覧モーダルの対象外。切り替え手段を渡されていない
            // 画面 (モーダル内の描画など) では何もしない。
            if (onNavigateBuilding) onNavigateBuilding(buildingId);
            return;
        }

        const itemId = extractItemId(href);
        if (itemId && onOpenItem) {
            onOpenItem(itemId);
        } else {
            setIsModalOpen(true);
        }
    }, [href, onOpenItem, onNavigateBuilding]);

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
