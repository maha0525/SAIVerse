"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import ChatOptions from '@/components/ChatOptions';
import RightSidebar from '@/components/RightSidebar';
import PeopleModal from '@/components/PeopleModal';
import { Send, Paperclip, MapPin, Settings, X, Info, Users, Menu } from 'lucide-react';

interface Message {
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp?: string; // ISO string
    avatar?: string;
    sender?: string;
}

export default function Home() {
    const [messages, setMessages] = useState<Message[]>([]);
    const [inputValue, setInputValue] = useState('');
    const [loadingStatus, setLoadingStatus] = useState<string | null>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const chatAreaRef = useRef<HTMLDivElement>(null); // Ref for the scrollable area
    const [isHistoryLoaded, setIsHistoryLoaded] = useState(false);

    // Pagination State
    const [hasMore, setHasMore] = useState(true);
    const [isLoadingMore, setIsLoadingMore] = useState(false);
    const previousScrollHeightRef = useRef<number>(0);
    const prevNewestIdRef = useRef<string | undefined>(undefined); // Track newest message ID

    // New States
    const [isLeftOpen, setIsLeftOpen] = useState(false);
    const [isOptionsOpen, setIsOptionsOpen] = useState(false);
    const [isInfoOpen, setIsInfoOpen] = useState(false); // Default closed to prevent mobile flash
    const [moveTrigger, setMoveTrigger] = useState(0); // To trigger RightSidebar refresh

    useEffect(() => {
        // Detect mobile device (touch-based or narrow screen)
        const checkMobile = () => {
            const isTouchDevice = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
            const isNarrowScreen = window.innerWidth < 768;
            setIsMobile(isTouchDevice || isNarrowScreen);
        };
        checkMobile();
        window.addEventListener('resize', checkMobile);

        // Open Info sidebar by default on Desktop
        if (window.innerWidth >= 768) {
            setIsInfoOpen(true);
        }

        return () => window.removeEventListener('resize', checkMobile);
    }, []);

    // Auto-resize textarea based on content (max 10 lines)
    const adjustTextareaHeight = useCallback(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;

        // Reset height to calculate scrollHeight correctly
        textarea.style.height = 'auto';

        // Calculate line height (approximately 1.5 * font-size of 0.95rem ≈ 22.8px)
        const lineHeight = 24; // px, approximate
        const maxLines = 10;
        const maxHeight = lineHeight * maxLines;

        // Set new height (capped at max)
        const newHeight = Math.min(textarea.scrollHeight, maxHeight);
        textarea.style.height = `${newHeight}px`;
    }, []);

    // Adjust height when input value changes
    useEffect(() => {
        adjustTextareaHeight();
    }, [inputValue, adjustTextareaHeight]);
    const [isPeopleModalOpen, setIsPeopleModalOpen] = useState(false);
    const [selectedPlaybook, setSelectedPlaybook] = useState<string | null>(null);
    const [attachment, setAttachment] = useState<string | null>(null); // Base64
    const [attachmentName, setAttachmentName] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const [isMobile, setIsMobile] = useState(false);
    const swipeStartX = useRef<number | null>(null);
    const swipeStartY = useRef<number | null>(null);
    const swipeStartTime = useRef<number | null>(null);

    // Right Edge Swipe Logic (Now Global Swipe Left)
    // REMOVED window listeners. Added handlers directly to main container.
    const handleTouchStart = (e: React.TouchEvent) => {
        swipeStartX.current = e.touches[0].clientX;
        swipeStartY.current = e.touches[0].clientY;
        swipeStartTime.current = Date.now();
    };

    const handleTouchMove = (e: React.TouchEvent) => {
        if (swipeStartX.current === null || swipeStartY.current === null || swipeStartTime.current === null) return;

        // Disable swipe if any modal is open
        if (isOptionsOpen || isPeopleModalOpen) return;

        const currentX = e.touches[0].clientX;
        const currentY = e.touches[0].clientY;
        const diffX = currentX - swipeStartX.current;
        const diffY = currentY - swipeStartY.current;
        const timeDiff = Date.now() - swipeStartTime.current;

        // 1. Vertical Scroll Lock: If moving more vertically than horizontally, assume scroll and abort swipe
        if (Math.abs(diffY) > Math.abs(diffX)) {
            swipeStartX.current = null;
            return;
        }

        // 2. Time Expiration: If it takes too long (> 300ms), it's a drag/drift, not a quick swipe
        if (timeDiff > 300) {
            swipeStartX.current = null;
            return;
        }

        // Swipe Left (< -80px) -> Open
        // Slightly reduced threshold since we have a strict time limit now
        if (diffX < -80) {
            setIsInfoOpen(true);
            swipeStartX.current = null;
        }
    };

    // Scroll manipulation effect
    useEffect(() => {
        // Initial load scroll to bottom
        if (messages.length > 0 && !isLoadingMore && isHistoryLoaded) {
            // Only scroll to bottom if we are NOT loading more (i.e. new usage or initial load)
            // Check if we are near bottom or if it's a fresh load?
            // Simplest: if isHistoryLoaded just became true (initial load) OR we just sent a message.
            // But 'isHistoryLoaded' is true after initial fetch.
            // We can check if previousScrollHeightRef is 0 (initial)
            messagesEndRef.current?.scrollIntoView({
                behavior: 'auto', // Intial load instant
                block: 'end'
            });
        }
    }, [isHistoryLoaded]); // Only on initial history ready

    // Scroll to bottom on NEW user/assistant messages (append)
    useEffect(() => {
        const currentNewestId = messages[messages.length - 1]?.id;
        const prevNewestId = prevNewestIdRef.current;

        // Update ref
        prevNewestIdRef.current = currentNewestId;

        // If newest ID didn't change, old history was prepended - don't scroll
        if (prevNewestId !== undefined && currentNewestId === prevNewestId) {
            return;
        }

        if (messages.length > 0 && !isLoadingMore && isHistoryLoaded) {
            messagesEndRef.current?.scrollIntoView({
                behavior: 'smooth',
                block: 'end'
            });
        }
    }, [messages.length, isLoadingMore, isHistoryLoaded]);


    // Restore scroll position after loading previous history
    useEffect(() => {
        if (isLoadingMore && chatAreaRef.current) {
            const newScrollHeight = chatAreaRef.current.scrollHeight;
            const diff = newScrollHeight - previousScrollHeightRef.current;
            if (diff > 0) {
                chatAreaRef.current.scrollTop = diff;
            }
            setIsLoadingMore(false);
        }
    }, [messages, isLoadingMore]);


    const fetchHistory = async (beforeId?: string) => {
        try {
            if (!beforeId) {
                setIsHistoryLoaded(false);
                setHasMore(true);
            } else {
                setIsLoadingMore(true);
                if (chatAreaRef.current) {
                    previousScrollHeightRef.current = chatAreaRef.current.scrollHeight;
                }
            }

            const params = new URLSearchParams({ limit: '20' });
            if (beforeId) params.append('before', beforeId);

            console.log(`[DEBUG] Fetching history: before=${beforeId}`);

            const res = await fetch(`/api/chat/history?${params.toString()}`);
            if (res.ok) {
                const data = await res.json();
                const newMessages: Message[] = data.history || [];
                console.log(`[DEBUG] Fetched ${newMessages.length} items`);

                if (newMessages.length < 20) {
                    setHasMore(false);
                }

                if (beforeId) {
                    setMessages(prev => {
                        // Deduplicate
                        const existingIds = new Set(prev.map(m => m.id));
                        const filtered = newMessages.filter(m => !m.id || !existingIds.has(m.id));
                        if (filtered.length === 0) return prev;
                        return [...filtered, ...prev];
                    });
                } else {
                    setMessages(newMessages);
                    setTimeout(() => setIsHistoryLoaded(true), 150);
                }
            } else {
                console.error("[DEBUG] Fetch failed", res.status);
                if (!beforeId) setMessages([]);
            }
        } catch (err) {
            console.error("Failed to load history", err);
            if (!beforeId) setIsHistoryLoaded(true);
        } finally {
            setIsLoadingMore(false);
        }
    };

    // Scroll Restoration Logic
    // Runs when messages change. If we were loading more, adjust scroll.
    useEffect(() => {
        if (isLoadingMore && chatAreaRef.current && previousScrollHeightRef.current > 0) {
            const newScrollHeight = chatAreaRef.current.scrollHeight;
            const diff = newScrollHeight - previousScrollHeightRef.current;
            if (diff > 0) {
                chatAreaRef.current.scrollTop = diff;
                console.log(`[DEBUG] Restored scroll: +${diff}px`);
            }
        }
    }, [messages, isLoadingMore]);

    const handleScroll = () => {
        if (chatAreaRef.current) {
            const { scrollTop } = chatAreaRef.current;
            // Use a threshold (e.g. 10px) to catch scrolls near the top
            if (scrollTop < 10 && hasMore && !isLoadingMore && messages.length > 0 && isHistoryLoaded) {
                // Determine the oldest message ID
                const oldestId = messages[0].id;
                if (oldestId) {
                    fetchHistory(oldestId);
                }
            }
        }
    };

    useEffect(() => {
        fetchHistory();
    }, []);

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && !attachment) || loadingStatus) return;

        // Optimistic update
        // Temporary ID for key prop until refreshed
        const tempId = `temp-${Date.now()}`;
        const userMsg: Message = { id: tempId, role: 'user', content: inputValue };
        setMessages(prev => [...prev, userMsg]);
        setInputValue('');
        setLoadingStatus('Thinking...');

        const currentAttachment = attachment;
        const currentPlaybook = selectedPlaybook;

        setAttachment(null);
        setAttachmentName(null);

        try {
            const res = await fetch('/api/chat/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: userMsg.content,
                    attachment: currentAttachment,
                    meta_playbook: currentPlaybook
                })
            });

            if (!res.ok) {
                let errorDetails = `Status: ${res.status} ${res.statusText}`;
                try {
                    const errorText = await res.text();
                    errorDetails += ` - Body: ${errorText}`;
                } catch (e) { }
                throw new Error(`Failed to send message. ${errorDetails}`);
            }

            if (!res.body) throw new Error("No response body");
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || ''; // Keep the last partial line

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const event = JSON.parse(line);

                        if (event.type === 'status') {
                            setLoadingStatus(event.content === 'processing' ? 'Processing...' : event.content);
                        } else if (event.type === 'think') {
                            setLoadingStatus(`Thinking: ${event.content.substring(0, 50)}${event.content.length > 50 ? '...' : ''}`);
                        } else if (event.type === 'say') {
                            const avatarUrl = event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined;

                            setMessages(prev => [...prev, {
                                role: 'assistant',
                                content: event.content,
                                sender: event.persona_name || 'Assistant',
                                avatar: avatarUrl,
                                timestamp: new Date().toISOString()
                            }]);
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'error') {
                            setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${event.content}` }]);
                        } else if (event.response) {
                            setMessages(prev => [...prev, { role: 'assistant', content: event.response }]);
                        }

                    } catch (e) {
                        console.error("Error parsing NDJSON line", e, line);
                    }
                }
            }

        } catch (error) {
            console.error(error);
            setMessages(prev => [...prev, { role: 'assistant', content: "Error: Failed to send message." }]);
        } finally {
            setLoadingStatus(null);
            fetchHistory(); // Sync final state (avatars, names etc)
        }
    };

    const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
        // Mobile: only send button sends (Enter = newline)
        // PC: Ctrl+Enter sends (Enter = newline)
        if (e.key === 'Enter') {
            if (isMobile) {
                // On mobile, Enter always inserts newline (default behavior)
                return;
            } else {
                // On PC, Ctrl+Enter sends, regular Enter inserts newline
                if (e.ctrlKey || e.metaKey) {
                    e.preventDefault();
                    handleSendMessage();
                }
                // Regular Enter: let default behavior insert newline
            }
        }
    };

    const handleFileUpload = (e: ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files[0]) {
            const file = e.target.files[0];
            const reader = new FileReader();
            reader.onloadend = () => {
                setAttachment(reader.result as string);
                setAttachmentName(file.name);
            };
            reader.readAsDataURL(file);
        }
    };

    const clearAttachment = () => {
        setAttachment(null);
        setAttachmentName(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
    };

    return (
        <div
            className={styles.container}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
        >
            <Sidebar
                onMove={() => {
                    setMessages([]);
                    setIsHistoryLoaded(false);
                    fetchHistory();
                    setMoveTrigger(prev => prev + 1);
                }}
                isOpen={isLeftOpen}
                onOpen={() => setIsLeftOpen(true)}
                onClose={() => setIsLeftOpen(false)}
            />

            <main className={styles.contentWrapper}>
                <header className={styles.header}>
                    <div className={styles.headerLeft}>
                        <button
                            className={styles.mobileMenuBtn} // New class needed
                            onClick={() => setIsLeftOpen(true)}
                            title="Open Menu"
                        >
                            <Menu size={20} />
                        </button>
                        <h1>SAIVerse City</h1>
                        <span className={styles.status}>● Online</span>
                    </div>
                    <div className={styles.headerRight}>
                        <button
                            className={styles.iconBtn}
                            onClick={() => setIsPeopleModalOpen(true)}
                            title="Manage People"
                        >
                            <Users size={20} />
                        </button>
                        <button
                            className={styles.iconBtn}
                            onClick={() => setIsOptionsOpen(true)}
                            title="Chat Options"
                        >
                            <Settings size={20} />
                        </button>
                        <button
                            className={`${styles.iconBtn} ${isInfoOpen ? styles.active : ''}`}
                            onClick={() => setIsInfoOpen(!isInfoOpen)}
                            title="Toggle Info Sidebar"
                        >
                            <Info size={20} />
                        </button>
                    </div>
                </header>

                <div
                    className={styles.chatArea}
                    ref={chatAreaRef}
                    onScroll={handleScroll}
                >
                    {isLoadingMore && <div style={{ textAlign: 'center', padding: '10px', color: '#666' }}>Loading history...</div>}
                    {messages.map((msg, idx) => (
                        <div key={msg.id || idx} className={`${styles.message} ${styles[msg.role]}`}>
                            <div className={styles.card}>
                                <div className={styles.cardHeader}>
                                    <img
                                        src={msg.avatar || (msg.role === 'user' ? '/api/static/icons/user.png' : '/api/static/icons/host.png')}
                                        alt="avatar"
                                        className={styles.avatar}
                                    />
                                    <span className={styles.sender}>{msg.sender || (msg.role === 'user' ? 'You' : 'Assistant')}</span>
                                </div>
                                <div className={styles.cardBody}>
                                    <ReactMarkdown>{msg.content}</ReactMarkdown>
                                </div>
                                {msg.timestamp && (
                                    <div className={styles.cardFooter}>
                                        {new Date(msg.timestamp).toLocaleString()}
                                    </div>
                                )}
                            </div>
                        </div>
                    ))}
                    {loadingStatus && <div className={styles.loading}>{loadingStatus}</div>}
                    <div ref={messagesEndRef} />
                </div>

                <div className={styles.inputArea}>
                    {attachmentName && (
                        <div style={{
                            fontSize: '0.8rem',
                            marginBottom: '0.5rem',
                            padding: '0.25rem 0.5rem',
                            background: '#eee',
                            borderRadius: '4px',
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '0.5rem',
                            color: '#333'
                        }}>
                            <span>📎 {attachmentName}</span>
                            <button onClick={clearAttachment} style={{ border: 'none', background: 'none', cursor: 'pointer', padding: '0 4px' }}><X size={14} /></button>
                        </div>
                    )}
                    <div className={styles.inputWrapper}>
                        <button
                            className={styles.attachBtn}
                            onClick={() => fileInputRef.current?.click()}
                            title="Attach File"
                        >
                            <Paperclip size={20} />
                        </button>
                        <input
                            type="file"
                            ref={fileInputRef}
                            style={{ display: 'none' }}
                            onChange={handleFileUpload}
                        />
                        <textarea
                            ref={textareaRef}
                            value={inputValue}
                            onChange={(e) => setInputValue(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder={selectedPlaybook ? `Message (Playbook: ${selectedPlaybook})...` : "Type a message..."}
                            rows={1}
                        />
                        <button
                            className={styles.sendBtn}
                            onClick={handleSendMessage}
                            disabled={!!loadingStatus || (!inputValue.trim() && !attachment)}
                        >
                            <Send size={20} />
                        </button>
                    </div>
                </div>
            </main>

            <RightSidebar
                isOpen={isInfoOpen}
                onClose={() => setIsInfoOpen(false)}
                refreshTrigger={moveTrigger}
            />

            <ChatOptions
                isOpen={isOptionsOpen}
                onClose={() => setIsOptionsOpen(false)}
                currentPlaybook={selectedPlaybook}
                onPlaybookChange={setSelectedPlaybook}
            />

            <PeopleModal
                isOpen={isPeopleModalOpen}
                onClose={() => setIsPeopleModalOpen(false)}
            />
        </div>
    );
}
