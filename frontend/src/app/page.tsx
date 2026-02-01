"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import ChatOptions from '@/components/ChatOptions';
import RightSidebar from '@/components/RightSidebar';
import PeopleModal from '@/components/PeopleModal';
import { Send, Paperclip, MapPin, Settings, X, Info, Users, Menu } from 'lucide-react';
import { useActivityTracker } from '@/hooks/useActivityTracker';

interface MessageImage {
    url: string;
    mime_type?: string;
}

interface MessageLLMUsage {
    model: string;
    model_display_name?: string;
    input_tokens: number;
    output_tokens: number;
    cost_usd?: number;
}

interface MessageLLMUsageTotal {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost_usd: number;
    call_count: number;
    models_used: string[];
}

interface Message {
    id?: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp?: string; // ISO string
    avatar?: string;
    sender?: string;
    images?: MessageImage[];
    llm_usage?: MessageLLMUsage;
    llm_usage_total?: MessageLLMUsageTotal;
}

// File attachment types for upload
interface FileAttachment {
    base64: string;
    name: string;
    type: 'image' | 'document' | 'unknown';
    mimeType: string;
}

// File type detection
const TEXT_EXTENSIONS = new Set(['txt', 'md', 'py', 'js', 'ts', 'tsx', 'json', 'yaml', 'yml', 'csv',
    'html', 'css', 'xml', 'log', 'sh', 'bat', 'sql', 'java', 'c', 'cpp',
    'h', 'hpp', 'go', 'rs', 'rb', 'swift', 'kt', 'scala', 'r', 'lua', 'pl']);
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);

function getFileType(filename: string, mimeType: string): 'image' | 'document' | 'unknown' {
    const ext = filename.split('.').pop()?.toLowerCase() || '';
    if (IMAGE_EXTENSIONS.has(ext) || mimeType.startsWith('image/')) {
        return 'image';
    }
    if (TEXT_EXTENSIONS.has(ext) || mimeType.startsWith('text/')) {
        return 'document';
    }
    return 'unknown';
}

export default function Home() {
    // Enable user presence tracking (heartbeat + visibility)
    useActivityTracker();

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
    const [playbookParams, setPlaybookParams] = useState<Record<string, any>>({});
    const [attachments, setAttachments] = useState<FileAttachment[]>([]); // Multiple attachments
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
                // Use server-provided has_more flag if available, fallback to count-based heuristic
                const serverHasMore = data.has_more;
                const effectiveHasMore = serverHasMore !== undefined ? serverHasMore : (newMessages.length >= 20);
                console.log(`[DEBUG] Fetched ${newMessages.length} items (beforeId=${beforeId}, server has_more=${serverHasMore}, effectiveHasMore=${effectiveHasMore})`);

                if (!effectiveHasMore) {
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
            if (scrollTop < 10) {
                console.log(`[DEBUG] Scroll near top: hasMore=${hasMore}, isLoadingMore=${isLoadingMore}, messages.length=${messages.length}, isHistoryLoaded=${isHistoryLoaded}`);
            }
            if (scrollTop < 10 && hasMore && !isLoadingMore && messages.length > 0 && isHistoryLoaded) {
                // Determine the oldest message ID
                const oldestId = messages[0].id;
                console.log(`[DEBUG] Triggering fetchHistory with before=${oldestId}`);
                if (oldestId) {
                    fetchHistory(oldestId);
                }
            }
        }
    };

    useEffect(() => {
        fetchHistory();
        // Fetch saved playbook setting and params from server
        fetch('/api/config/playbook')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data) {
                    if (data.playbook) {
                        setSelectedPlaybook(data.playbook);
                    }
                    if (data.playbook_params && Object.keys(data.playbook_params).length > 0) {
                        setPlaybookParams(data.playbook_params);
                    }
                }
            })
            .catch(err => console.error('Failed to load playbook setting', err));
    }, []);

    // Polling for new messages (schedule-triggered persona speech, etc.)
    const latestMessageIdRef = useRef<string | undefined>(undefined);

    // Keep ref updated with latest message ID
    useEffect(() => {
        const newestId = messages[messages.length - 1]?.id;
        if (newestId && !newestId.startsWith('temp-')) {
            latestMessageIdRef.current = newestId;
        }
    }, [messages]);

    useEffect(() => {
        if (!isHistoryLoaded) return; // Don't poll until initial load is done

        const pollInterval = setInterval(async () => {
            const newestId = latestMessageIdRef.current;
            if (!newestId) return; // Skip if no real ID

            try {
                const res = await fetch(`/api/chat/history?after=${newestId}&limit=50`);
                if (res.ok) {
                    const data = await res.json();
                    const newMessages: Message[] = data.history || [];

                    if (newMessages.length > 0) {
                        console.log(`[Polling] Found ${newMessages.length} new message(s)`);
                        setMessages(prev => {
                            // Deduplicate
                            const existingIds = new Set(prev.map(m => m.id));
                            const filtered = newMessages.filter(m => !m.id || !existingIds.has(m.id));
                            if (filtered.length === 0) return prev;
                            return [...prev, ...filtered];
                        });
                    }
                }
            } catch (err) {
                console.error("[Polling] Failed to check for new messages", err);
            }
        }, 5000); // Poll every 5 seconds

        return () => clearInterval(pollInterval);
    }, [isHistoryLoaded]);

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && attachments.length === 0) || loadingStatus) return;

        // Optimistic update
        // Temporary ID for key prop until refreshed
        const tempId = `temp-${Date.now()}`;
        const userMsg: Message = { id: tempId, role: 'user', content: inputValue };
        setMessages(prev => [...prev, userMsg]);
        setInputValue('');
        setLoadingStatus('Thinking...');

        const currentAttachments = attachments;
        const currentPlaybook = selectedPlaybook;
        const currentPlaybookParams = playbookParams;

        setAttachments([]);
        // Reset playbook params after sending
        setPlaybookParams({});

        try {
            const res = await fetch('/api/chat/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: userMsg.content,
                    attachments: currentAttachments.length > 0 ? currentAttachments.map(a => ({
                        data: a.base64,
                        filename: a.name,
                        type: a.type,
                        mime_type: a.mimeType
                    })) : undefined,
                    meta_playbook: currentPlaybook,
                    playbook_params: Object.keys(currentPlaybookParams).length > 0 ? currentPlaybookParams : undefined
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
                        } else if (event.type === 'streaming_chunk') {
                            // Streaming: append chunk to last message or create new one
                            const avatarUrl = event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined;
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                // Check if last message is a streaming message from same persona
                                if (last && last.role === 'assistant' && last._streaming) {
                                    // Append to existing streaming message
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        content: last.content + event.content
                                    }];
                                } else {
                                    // Create new streaming message
                                    return [...prev, {
                                        role: 'assistant',
                                        content: event.content,
                                        sender: event.persona_name || 'Assistant',
                                        avatar: avatarUrl,
                                        timestamp: new Date().toISOString(),
                                        _streaming: true  // Mark as streaming in progress
                                    }];
                                }
                            });
                            setLoadingStatus('Streaming...');
                        } else if (event.type === 'streaming_complete') {
                            // Mark streaming message as complete
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    const { _streaming, ...rest } = last;
                                    return [...prev.slice(0, -1), rest];
                                }
                                return prev;
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'say') {
                            console.log('[DEBUG] Received say event:', event);
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
        if (e.target.files && e.target.files.length > 0) {
            const files = Array.from(e.target.files);

            files.forEach(file => {
                const reader = new FileReader();
                reader.onloadend = () => {
                    const base64 = reader.result as string;
                    const mimeType = file.type || 'application/octet-stream';
                    const fileType = getFileType(file.name, mimeType);

                    setAttachments(prev => [...prev, {
                        base64,
                        name: file.name,
                        type: fileType,
                        mimeType
                    }]);
                };
                reader.readAsDataURL(file);
            });

            // Reset input to allow selecting the same files again
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    const removeAttachment = (index: number) => {
        setAttachments(prev => prev.filter((_, i) => i !== index));
    };

    const clearAllAttachments = () => {
        setAttachments([]);
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
                                    {msg.images && msg.images.length > 0 && (
                                        <div className={styles.messageImages}>
                                            {msg.images.map((img, imgIdx) => (
                                                <img
                                                    key={imgIdx}
                                                    src={img.url}
                                                    alt={`Attachment ${imgIdx + 1}`}
                                                    className={styles.messageImage}
                                                    onClick={() => window.open(img.url, '_blank')}
                                                />
                                            ))}
                                        </div>
                                    )}
                                    <ReactMarkdown remarkPlugins={[remarkBreaks]}>{msg.content}</ReactMarkdown>
                                </div>
                                {(msg.timestamp || msg.llm_usage || msg.llm_usage_total) && (
                                    <div className={styles.cardFooter}>
                                        {msg.timestamp && <span>{new Date(msg.timestamp).toLocaleString()}</span>}
                                        {msg.llm_usage_total && msg.llm_usage_total.call_count > 1 ? (
                                            // Show total usage when multiple LLM calls were made
                                            <span className={styles.llmUsage} title={`Models: ${msg.llm_usage_total.models_used.join(', ')}\nLLM Calls: ${msg.llm_usage_total.call_count}\nTotal Input: ${msg.llm_usage_total.total_input_tokens.toLocaleString()} tokens\nTotal Output: ${msg.llm_usage_total.total_output_tokens.toLocaleString()} tokens\nTotal Cost: $${msg.llm_usage_total.total_cost_usd.toFixed(4)}`}>
                                                {msg.llm_usage_total.call_count} calls · {(msg.llm_usage_total.total_input_tokens + msg.llm_usage_total.total_output_tokens).toLocaleString()} tokens · ${msg.llm_usage_total.total_cost_usd.toFixed(4)}
                                            </span>
                                        ) : msg.llm_usage && (
                                            // Show single call usage
                                            <span className={styles.llmUsage} title={`Model: ${msg.llm_usage.model}\nInput: ${msg.llm_usage.input_tokens.toLocaleString()} tokens\nOutput: ${msg.llm_usage.output_tokens.toLocaleString()} tokens\nCost: $${(msg.llm_usage.cost_usd || 0).toFixed(4)}`}>
                                                {msg.llm_usage.model_display_name || msg.llm_usage.model} · {(msg.llm_usage.input_tokens + msg.llm_usage.output_tokens).toLocaleString()} tokens
                                            </span>
                                        )}
                                    </div>
                                )}
                            </div>
                        </div>
                    ))}
                    {loadingStatus && <div className={styles.loading}>{loadingStatus}</div>}
                    <div ref={messagesEndRef} />
                </div>

                <div className={styles.inputArea}>
                    {attachments.length > 0 && (
                        <div style={{
                            fontSize: '0.8rem',
                            marginBottom: '0.5rem',
                            display: 'flex',
                            flexWrap: 'wrap',
                            gap: '0.5rem'
                        }}>
                            {attachments.map((att, idx) => (
                                <div key={idx} style={{
                                    padding: '0.25rem 0.5rem',
                                    background: '#eee',
                                    borderRadius: '4px',
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    gap: '0.5rem',
                                    color: '#333'
                                }}>
                                    <span>{att.type === 'image' ? '🖼' : '📄'} {att.name}</span>
                                    <button onClick={() => removeAttachment(idx)} style={{ border: 'none', background: 'none', cursor: 'pointer', padding: '0 4px' }}><X size={14} /></button>
                                </div>
                            ))}
                            {attachments.length > 1 && (
                                <button onClick={clearAllAttachments} style={{
                                    fontSize: '0.75rem',
                                    padding: '0.25rem 0.5rem',
                                    background: '#ddd',
                                    border: 'none',
                                    borderRadius: '4px',
                                    cursor: 'pointer',
                                    color: '#666'
                                }}>Clear All</button>
                            )}
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
                            multiple
                            accept="image/*,.txt,.md,.py,.js,.ts,.tsx,.json,.yaml,.yml,.csv,.html,.css,.xml,.log,.sh,.sql,.java,.c,.cpp,.go,.rs,.rb"
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
                            disabled={!!loadingStatus || (!inputValue.trim() && attachments.length === 0)}
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
                playbookParams={playbookParams}
                onPlaybookParamsChange={setPlaybookParams}
            />

            <PeopleModal
                isOpen={isPeopleModalOpen}
                onClose={() => setIsPeopleModalOpen(false)}
            />
        </div>
    );
}
