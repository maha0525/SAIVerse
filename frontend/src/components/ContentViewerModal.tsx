'use client';

import React, { useState, useEffect } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import SaiverseLink from './SaiverseLink';
import { X } from 'lucide-react';
import ModalOverlay from '@/components/common/ModalOverlay';
import styles from './ContentViewerModal.module.css';

interface ContentViewerModalProps {
    isOpen: boolean;
    onClose: () => void;
    uri: string;
    personaId?: string;
}

/** Maps content_type from the API to a human-readable label. */
function contentTypeLabel(ct: string): string {
    const map: Record<string, string> = {
        message: 'Message',
        message_log: 'Message Log',
        memopedia_page: 'Memopedia',
        chronicle_entry: 'Chronicle',
        chronicle_list: 'Chronicle',
        document: 'Document',
        item_content: 'Item',
        image: 'Image',
    };
    return map[ct] || ct;
}

interface ParsedMessage {
    role: string;
    timestamp: string;
    content: string;
    isHighlighted: boolean;
}

/** Parse AI-formatted message log into structured messages.
 *  Format: "[role] YYYY-MM-DD HH:MM: content" joined by \n\n.
 *  Messages may contain newlines, so we split on the header pattern rather than \n\n.
 */
function parseMessageLog(raw: string): ParsedMessage[] | null {
    // Find all header positions: "[word] YYYY-MM-DD HH:MM: "
    const headerRe = /\[(\w+)\]\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\s*/g;
    const matches: { role: string; timestamp: string; contentStart: number; matchStart: number }[] = [];

    let m;
    while ((m = headerRe.exec(raw)) !== null) {
        matches.push({
            role: m[1],
            timestamp: m[2],
            contentStart: m.index + m[0].length,
            matchStart: m.index,
        });
    }

    if (matches.length === 0) return null;

    const messages: ParsedMessage[] = [];
    for (let i = 0; i < matches.length; i++) {
        const contentEnd = i + 1 < matches.length ? matches[i + 1].matchStart : raw.length;
        let content = raw.slice(matches[i].contentStart, contentEnd).trim();
        const isHighlighted = content.endsWith('<<<');
        if (isHighlighted) content = content.slice(0, -3).trim();
        messages.push({
            role: matches[i].role,
            timestamp: matches[i].timestamp,
            content,
            isHighlighted,
        });
    }

    return messages.length > 0 ? messages : null;
}

function MessageLogView({ content, personaId }: { content: string; personaId?: string }) {
    const messages = parseMessageLog(content);
    if (!messages) {
        // Fallback to plain markdown
        return (
            <div className={styles.content}>
                <ReactMarkdown
                    remarkPlugins={[remarkBreaks]}
                    urlTransform={(url) => url.startsWith('saiverse://') ? url : defaultUrlTransform(url)}
                    components={{
                        a: ({ href, children }) => <SaiverseLink href={href} personaId={personaId}>{children}</SaiverseLink>,
                    }}
                >{content}</ReactMarkdown>
            </div>
        );
    }

    return (
        <div className={styles.chatLog}>
            {messages.map((msg, idx) => (
                <div
                    key={idx}
                    className={`${styles.chatMessage} ${styles[`chat_${msg.role}`] || ''} ${msg.isHighlighted ? styles.chatHighlighted : ''}`}
                >
                    <div className={styles.chatMeta}>
                        <span className={styles.chatRole}>{msg.role}</span>
                        <span className={styles.chatTime}>{msg.timestamp}</span>
                    </div>
                    <div className={styles.chatContent}>{msg.content}</div>
                </div>
            ))}
        </div>
    );
}

export default function ContentViewerModal({ isOpen, onClose, uri, personaId }: ContentViewerModalProps) {
    const [content, setContent] = useState<string | null>(null);
    const [contentType, setContentType] = useState<string>('');
    const [metadata, setMetadata] = useState<Record<string, unknown>>({});
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!isOpen || !uri) return;

        setIsLoading(true);
        setError(null);
        setContent(null);

        const params = new URLSearchParams({ uri });
        if (personaId) params.set('persona_id', personaId);

        fetch(`/api/uri/resolve?${params.toString()}`)
            .then(async (res) => {
                if (!res.ok) {
                    const detail = await res.json().catch(() => null);
                    throw new Error(detail?.detail || `HTTP ${res.status}`);
                }
                return res.json();
            })
            .then((data) => {
                setContent(data.content);
                setContentType(data.content_type || '');
                setMetadata(data.metadata || {});
            })
            .catch((err) => {
                setError(err.message || 'Failed to resolve URI');
            })
            .finally(() => {
                setIsLoading(false);
            });
    }, [isOpen, uri, personaId]);

    if (!isOpen) return null;

    const isMessageLog = contentType === 'message_log' || contentType === 'message';

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.headerInfo}>
                        {contentType && (
                            <span className={styles.badge}>{contentTypeLabel(contentType)}</span>
                        )}
                        <h2>{metadata?.title as string || uri}</h2>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={18} />
                    </button>
                </div>

                <div className={styles.body}>
                    {isLoading && (
                        <div className={styles.loading}>Loading...</div>
                    )}
                    {error && (
                        <div className={styles.error}>{error}</div>
                    )}
                    {content !== null && !isLoading && (
                        isMessageLog ? (
                            <MessageLogView content={content} personaId={personaId} />
                        ) : (
                            <div className={styles.content}>
                                <ReactMarkdown
                                    remarkPlugins={[remarkBreaks]}
                                    urlTransform={(url) => url.startsWith('saiverse://') ? url : defaultUrlTransform(url)}
                                    components={{
                                        a: ({ href, children }) => <SaiverseLink href={href} personaId={personaId}>{children}</SaiverseLink>,
                                    }}
                                >
                                    {content}
                                </ReactMarkdown>
                            </div>
                        )
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
