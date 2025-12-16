"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import ChatOptions from '@/components/ChatOptions';
import RightSidebar from '@/components/RightSidebar';
import PeopleModal from '@/components/PeopleModal';
import { Send, Paperclip, MapPin, Settings, X, Info, Users, Menu } from 'lucide-react';

interface Message {
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
    const [isHistoryLoaded, setIsHistoryLoaded] = useState(false);

    // New States
    const [isLeftOpen, setIsLeftOpen] = useState(false);
    const [isOptionsOpen, setIsOptionsOpen] = useState(false);
    const [isInfoOpen, setIsInfoOpen] = useState(false); // Default closed to prevent mobile flash
    const [moveTrigger, setMoveTrigger] = useState(0); // To trigger RightSidebar refresh

    useEffect(() => {
        // Open Info sidebar by default on Desktop
        if (window.innerWidth >= 768) {
            setIsInfoOpen(true);
        }
    }, []);
    const [isPeopleModalOpen, setIsPeopleModalOpen] = useState(false);
    const [selectedPlaybook, setSelectedPlaybook] = useState<string | null>(null);
    const [attachment, setAttachment] = useState<string | null>(null); // Base64
    const [attachmentName, setAttachmentName] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);
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

    // Scroll to bottom
    useEffect(() => {
        if (messages.length > 0) {
            messagesEndRef.current?.scrollIntoView({
                behavior: isHistoryLoaded ? 'smooth' : 'auto',
                block: 'end'
            });
        }
    }, [messages, isHistoryLoaded]);

    const fetchHistory = async () => {
        try {
            setIsHistoryLoaded(false);
            const res = await fetch('/api/chat/history');
            if (res.ok) {
                const data = await res.json();
                setMessages(data.history || []);
                setTimeout(() => setIsHistoryLoaded(true), 100);
            }
        } catch (err) {
            console.error("Failed to load history", err);
            setIsHistoryLoaded(true);
        }
    };

    useEffect(() => {
        fetchHistory();
    }, []);

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && !attachment) || loadingStatus) return;

        const userMsg: Message = { role: 'user', content: inputValue };
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
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendMessage();
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

                <div className={styles.chatArea}>
                    {messages.map((msg, idx) => (
                        <div key={idx} className={`${styles.message} ${styles[msg.role]}`}>
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
