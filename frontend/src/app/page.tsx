"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, useCallback } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import ChatOptions from '@/components/ChatOptions';
import RightSidebar from '@/components/RightSidebar';
import PeopleModal from '@/components/PeopleModal';
import TutorialWizard from '@/components/tutorial/TutorialWizard';
import SaiverseLink from '@/components/SaiverseLink';
import ItemModal from '@/components/ItemModal';
import ContextPreviewModal, { ContextPreviewData } from '@/components/ContextPreviewModal';
import { Send, Plus, Paperclip, Eye, X, Info, Users, Menu, Copy, Check, SlidersHorizontal, ChevronDown, AlertTriangle } from 'lucide-react';
import { useActivityTracker } from '@/hooks/useActivityTracker';

// Allow className on HTML elements used by thinking blocks (<details>, <div>, <summary>)
const sanitizeSchema = {
    ...defaultSchema,
    attributes: {
        ...defaultSchema.attributes,
        details: [...(defaultSchema.attributes?.details || []), 'className'],
        div: [...(defaultSchema.attributes?.div || []), 'className'],
        summary: [...(defaultSchema.attributes?.summary || []), 'className'],
    },
    protocols: {
        ...defaultSchema.protocols,
        href: [...(defaultSchema.protocols?.href || []), 'saiverse'],
    },
};

interface MessageImage {
    url: string;
    mime_type?: string;
}

interface MessageLLMUsage {
    model: string;
    model_display_name?: string;
    input_tokens: number;
    output_tokens: number;
    cached_tokens?: number;  // Tokens served from cache
    cost_usd?: number;
}

interface MessageLLMUsageTotal {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cached_tokens?: number;  // Total cached tokens across all calls
    total_cost_usd: number;
    call_count: number;
    models_used: string[];
}

interface Message {
    id?: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    timestamp?: string; // ISO string
    avatar?: string;
    sender?: string;
    images?: MessageImage[];
    llm_usage?: MessageLLMUsage;
    llm_usage_total?: MessageLLMUsageTotal;
    // Error information
    isError?: boolean;
    errorCode?: string;
    errorDetail?: string;
    // Warning information
    isWarning?: boolean;
    warningCode?: string;
    // Reasoning (thinking) from LLM
    reasoning?: string;
    // Activity trace (exec/tool steps before final response)
    activity_trace?: ActivityEntry[];
    // Streaming state
    _streaming?: boolean;
    _streamingThinking?: string;
    _activities?: ActivityEntry[];
}

interface ActivityEntry {
    action: 'exec' | 'tool' | 'memorize';
    name: string;
    playbook?: string;
    status?: string;
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
    const isProcessingRef = useRef(false); // Suppress polling during active request

    // New States
    const [isLeftOpen, setIsLeftOpen] = useState(false);
    const [isOptionsOpen, setIsOptionsOpen] = useState(false);
    const [isInfoOpen, setIsInfoOpen] = useState(false); // Default closed to prevent mobile flash
    const [moveTrigger, setMoveTrigger] = useState(0); // To trigger RightSidebar refresh
    const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null); // Track which message was copied
    const [usageTooltipId, setUsageTooltipId] = useState<string | null>(null); // Track which message's usage tooltip is open

    // ItemModal for saiverse:// item links
    const [linkItemModalItem, setLinkItemModalItem] = useState<{ id: string; name: string; description?: string; type: string } | null>(null);
    const handleOpenItemFromLink = useCallback(async (itemId: string) => {
        try {
            const res = await fetch(`/api/info/details?building_id=${currentBuildingIdRef.current}`);
            if (!res.ok) return;
            const data = await res.json();
            const found = data.items?.find((it: { id: string }) => it.id === itemId);
            if (found) {
                setLinkItemModalItem(found);
            } else {
                // Item not in current building, create minimal item object
                setLinkItemModalItem({ id: itemId, name: itemId, type: 'document' });
            }
        } catch {
            setLinkItemModalItem({ id: itemId, name: itemId, type: 'document' });
        }
    }, []);

    // Copy message content to clipboard
    const handleCopyMessage = useCallback(async (messageId: string, content: string) => {
        try {
            await navigator.clipboard.writeText(content);
            setCopiedMessageId(messageId);
            // Reset after 2 seconds
            setTimeout(() => setCopiedMessageId(null), 2000);
        } catch (err) {
            console.error('Failed to copy:', err);
        }
    }, []);

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

    // Check tutorial status on mount
    useEffect(() => {
        const checkTutorial = async () => {
            try {
                const res = await fetch('/api/tutorial/status');
                if (res.ok) {
                    const data = await res.json();
                    // Show tutorial if not completed or if initial setup is needed
                    if (!data.tutorial_completed || data.needs_initial_setup) {
                        setShowTutorial(true);
                    }
                }
            } catch (e) {
                console.error('Failed to check tutorial status', e);
            } finally {
                setTutorialChecked(true);
            }
        };

        checkTutorial();
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
    const [selectedPlaybook, setSelectedPlaybook] = useState<string | null>('meta_user');
    const [playbookParams, setPlaybookParams] = useState<Record<string, any>>({});
    const [selectedModel, setSelectedModel] = useState<string>(''); // Model ID selected in Chat Options
    const [selectedModelDisplayName, setSelectedModelDisplayName] = useState<string>(''); // Model display name
    const [isDragOver, setIsDragOver] = useState(false); // Drag & drop state
    const [attachments, setAttachments] = useState<FileAttachment[]>([]); // Multiple attachments
    const fileInputRef = useRef<HTMLInputElement>(null);
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const [showPlusMenu, setShowPlusMenu] = useState(false);
    const [showContextPreview, setShowContextPreview] = useState(false);
    const [contextPreviewData, setContextPreviewData] = useState<ContextPreviewData | null>(null);
    const [contextPreviewLoading, setContextPreviewLoading] = useState(false);
    const plusMenuRef = useRef<HTMLDivElement>(null);
    const [isMobile, setIsMobile] = useState(false);
    const [currentBuildingName, setCurrentBuildingName] = useState<string>('SAIVerse');
    const [currentBuildingId, setCurrentBuildingId] = useState<string | null>(null);
    const currentBuildingIdRef = useRef<string | null>(null);

    // Tutorial state
    const [showTutorial, setShowTutorial] = useState(false);
    const [tutorialChecked, setTutorialChecked] = useState(false);

    // Backend connection status
    const [backendConnected, setBackendConnected] = useState(true);

    // Startup warnings
    const [startupWarnings, setStartupWarnings] = useState<string[]>([]);
    const [showStartupWarnings, setShowStartupWarnings] = useState(false);

    // Toast notifications
    const [toasts, setToasts] = useState<{id: string; content: string}[]>([]);
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


    const fetchHistory = async (beforeId?: string, overrideBuildingId?: string) => {
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
            const bid = overrideBuildingId || currentBuildingIdRef.current;
            if (bid) params.append('building_id', bid);

            console.log(`[DEBUG] Fetching history: before=${beforeId}, building_id=${bid}`);

            const res = await fetch(`/api/chat/history?${params.toString()}`);
            if (res.ok) {
                setBackendConnected(true);
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
                if (res.status >= 500) setBackendConnected(false);
                if (!beforeId) setMessages([]);
            }
        } catch (err) {
            console.error("Failed to load history", err);
            setBackendConnected(false);
            if (!beforeId) setIsHistoryLoaded(true);
        } finally {
            setIsLoadingMore(false);
        }
    };

    // Smart merge after AI response: updates IDs/metadata without replacing the whole array
    const syncAfterResponse = async () => {
        // Purpose: Update IDs and metadata on recently-added messages so that
        // polling dedup and scroll tracking work with server-assigned IDs.
        // Does NOT add or remove messages — polling handles new-message detection.
        try {
            const bid = currentBuildingIdRef.current;
            const params = new URLSearchParams();
            params.append('limit', '10');
            if (bid) params.append('building_id', bid);

            const res = await fetch(`/api/chat/history?${params.toString()}`);
            if (!res.ok) return;
            const data = await res.json();
            const serverMessages: Message[] = data.history || [];
            if (serverMessages.length === 0) return;

            setMessages(prev => {
                const result = [...prev];

                // Build lookup from server messages: match by role + content prefix
                const serverMap = new Map<string, { msg: Message; used: boolean }>();
                for (const sm of serverMessages) {
                    const key = `${sm.role}:${(sm.content || '').substring(0, 120)}`;
                    serverMap.set(key, { msg: sm, used: false });
                }

                // Walk backwards through local messages, match with server
                let matched = 0;
                for (let i = result.length - 1; i >= 0; i--) {
                    const local = result[i];
                    const key = `${local.role}:${(local.content || '').substring(0, 120)}`;
                    const entry = serverMap.get(key);
                    if (entry && !entry.used) {
                        result[i] = {
                            ...local,
                            id: entry.msg.id,
                            avatar: entry.msg.avatar || local.avatar,
                            sender: entry.msg.sender || local.sender,
                            llm_usage: entry.msg.llm_usage || local.llm_usage,
                            llm_usage_total: entry.msg.llm_usage_total || local.llm_usage_total,
                            timestamp: entry.msg.timestamp || local.timestamp,
                        };
                        entry.used = true;
                        matched++;
                    }
                }

                console.log(`[syncAfterResponse] local=${prev.length} server=${serverMessages.length} matched=${matched} final=${result.length}`);
                return result;
            });
        } catch (err) {
            console.error("syncAfterResponse failed", err);
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

    const fetchBuildingInfo = async (overrideBuildingId?: string) => {
        try {
            const bid = overrideBuildingId || currentBuildingIdRef.current;
            const url = bid ? `/api/info/details?building_id=${bid}` : '/api/info/details';
            const res = await fetch(url);
            if (res.ok) {
                const data = await res.json();
                setCurrentBuildingName(data.name || 'SAIVerse');
            }
        } catch (err) {
            console.error('Failed to fetch building info', err);
        }
    };

    useEffect(() => {
        // Fetch current building_id for multi-device safety
        fetch('/api/user/status')
            .then(res => {
                if (!res.ok) {
                    setBackendConnected(false);
                    return null;
                }
                setBackendConnected(true);
                return res.json();
            })
            .then(data => {
                if (data?.current_building_id) {
                    setCurrentBuildingId(data.current_building_id);
                    currentBuildingIdRef.current = data.current_building_id;
                }
            })
            .catch(() => setBackendConnected(false));
        fetchHistory();
        fetchBuildingInfo();
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

        // Fetch current model setting
        Promise.all([
            fetch('/api/config/config').then(res => res.ok ? res.json() : null),
            fetch('/api/config/models').then(res => res.ok ? res.json() : null)
        ]).then(([config, models]) => {
            if (config?.current_model && models) {
                const modelId = config.current_model;
                const modelInfo = models.find((m: { id: string; name: string }) => m.id === modelId);
                setSelectedModel(modelId);
                setSelectedModelDisplayName(modelInfo?.name || '');
            }
        }).catch(err => console.error('Failed to load model setting', err));

        // Fetch startup warnings
        fetch('/api/config/startup-warnings')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data?.warnings?.length > 0) {
                    setStartupWarnings(data.warnings.map((w: { message: string }) => w.message));
                    setShowStartupWarnings(true);
                }
            })
            .catch(err => console.error('Failed to fetch startup warnings', err));
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
            if (isProcessingRef.current) return; // Skip polling during active request
            const newestId = latestMessageIdRef.current;
            if (!newestId) return; // Skip if no real ID

            try {
                const pollBid = currentBuildingIdRef.current;
                const bidParam = pollBid ? `&building_id=${pollBid}` : '';
                const res = await fetch(`/api/chat/history?after=${newestId}&limit=50${bidParam}`);
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

    // Backend reconnection polling: when disconnected, check every 10 seconds
    useEffect(() => {
        if (backendConnected) return;

        const reconnectInterval = setInterval(async () => {
            try {
                const res = await fetch('/api/user/status');
                if (res.ok) {
                    setBackendConnected(true);
                    // Refresh data after reconnection
                    fetchHistory();
                    fetchBuildingInfo();
                }
            } catch {
                // Still disconnected
            }
        }, 10000);

        return () => clearInterval(reconnectInterval);
    }, [backendConnected]);

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && attachments.length === 0) || loadingStatus) return;
        isProcessingRef.current = true;

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
                    building_id: currentBuildingIdRef.current || undefined,
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
                } catch (e) { console.error('Failed to read error response body:', e); }
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
                        } else if (event.type === 'activity') {
                            // Activity trace: accumulate tool/memorize steps
                            const entry: ActivityEntry = { action: event.action, name: event.name, ...(event.playbook && { playbook: event.playbook }), status: event.status };
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    const activities = [...(last._activities || [])];
                                    if (event.status === 'completed' || event.status === 'error') {
                                        const idx = activities.findIndex(
                                            a => a.action === entry.action && a.name === entry.name && a.status === 'started'
                                        );
                                        if (idx >= 0) {
                                            activities[idx] = { ...activities[idx], status: event.status };
                                        } else {
                                            activities.push(entry);
                                        }
                                    } else {
                                        activities.push(entry);
                                    }
                                    return [...prev.slice(0, -1), { ...last, _activities: activities }];
                                } else {
                                    return [...prev, {
                                        role: 'assistant' as const, content: '', _streaming: true,
                                        _activities: [entry], timestamp: new Date().toISOString()
                                    }];
                                }
                            });
                            setLoadingStatus(event.status === 'started' ? `Running ${event.name}...` : event.name);
                        } else if (event.type === 'streaming_thinking') {
                            // Streaming thinking: accumulate into _streamingThinking
                            const avatarUrl = event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined;
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        _streamingThinking: (last._streamingThinking || '') + event.content
                                    }];
                                } else {
                                    return [...prev, {
                                        role: 'assistant',
                                        content: '',
                                        sender: event.persona_name || 'Assistant',
                                        avatar: avatarUrl,
                                        timestamp: new Date().toISOString(),
                                        _streaming: true,
                                        _streamingThinking: event.content
                                    }];
                                }
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'streaming_chunk') {
                            // Streaming: append chunk to last message or create new one
                            const avatarUrl = event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined;
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        content: last.content + event.content
                                    }];
                                } else {
                                    return [...prev, {
                                        role: 'assistant',
                                        content: event.content,
                                        sender: event.persona_name || 'Assistant',
                                        avatar: avatarUrl,
                                        timestamp: new Date().toISOString(),
                                        _streaming: true
                                    }];
                                }
                            });
                            setLoadingStatus('Streaming...');
                        } else if (event.type === 'streaming_complete') {
                            // Mark streaming message as complete, finalize reasoning and activities
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    const { _streaming, _streamingThinking, _activities, ...rest } = last;
                                    const reasoning = event.reasoning || _streamingThinking || undefined;
                                    return [...prev.slice(0, -1), {
                                        ...rest,
                                        reasoning,
                                        ...((_activities && _activities.length > 0) && { activity_trace: _activities }),
                                    }];
                                }
                                return prev;
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'say') {
                            console.log('[DEBUG] Received say event:', event);
                            const avatarUrl = event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined;

                            // Extract images from metadata (mirrors chat.py logic)
                            let sayImages: MessageImage[] | undefined;
                            const sayMeta = event.metadata;
                            if (sayMeta && (sayMeta.images || sayMeta.media)) {
                                const mediaItems = sayMeta.images || sayMeta.media || [];
                                sayImages = [];
                                for (const img of mediaItems) {
                                    let imgPath: string = img.path || "";
                                    if (!imgPath && img.uri) {
                                        const prefix = "saiverse://image/";
                                        if (img.uri.startsWith(prefix)) {
                                            imgPath = img.uri.replace(prefix, "");
                                        }
                                    }
                                    if (imgPath) {
                                        const filename = imgPath.split('/').pop() || imgPath.split('\\').pop() || imgPath;
                                        sayImages.push({
                                            url: `/api/static/uploads/${filename}`,
                                            mime_type: img.mime_type
                                        });
                                    }
                                }
                                if (sayImages.length === 0) sayImages = undefined;
                            }

                            // Extract LLM usage total from metadata
                            let sayUsageTotal: MessageLLMUsageTotal | undefined;
                            if (sayMeta?.llm_usage_total) {
                                const ut = sayMeta.llm_usage_total;
                                sayUsageTotal = {
                                    total_input_tokens: ut.total_input_tokens || 0,
                                    total_output_tokens: ut.total_output_tokens || 0,
                                    total_cached_tokens: ut.total_cached_tokens,
                                    total_cost_usd: ut.total_cost_usd || 0,
                                    call_count: ut.call_count || 0,
                                    models_used: ut.models_used || [],
                                };
                            }

                            const sayReasoning = event.reasoning || undefined;
                            const sayActivityTrace = event.activity_trace || undefined;
                            setMessages(prev => {
                                // Check if last message already has this content (from streaming completion)
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && !last._streaming
                                    && last.content === event.content) {
                                    // Already have this message, just update metadata
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        avatar: avatarUrl || last.avatar,
                                        sender: event.persona_name || last.sender,
                                        ...(sayImages && { images: sayImages }),
                                        ...(sayUsageTotal && { llm_usage_total: sayUsageTotal }),
                                        ...(sayReasoning && { reasoning: sayReasoning }),
                                        ...(sayActivityTrace && { activity_trace: sayActivityTrace }),
                                    }];
                                }
                                return [...prev, {
                                    role: 'assistant',
                                    content: event.content,
                                    sender: event.persona_name || 'Assistant',
                                    avatar: avatarUrl,
                                    timestamp: new Date().toISOString(),
                                    ...(sayImages && { images: sayImages }),
                                    ...(sayUsageTotal && { llm_usage_total: sayUsageTotal }),
                                    ...(sayReasoning && { reasoning: sayReasoning }),
                                    ...(sayActivityTrace && { activity_trace: sayActivityTrace }),
                                }];
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'error') {
                            setMessages(prev => [...prev, {
                                role: 'assistant',
                                content: event.content || 'An error occurred',
                                isError: true,
                                errorCode: event.error_code || 'unknown',
                                errorDetail: event.technical_detail,
                                timestamp: new Date().toISOString()
                            }]);
                        } else if (event.type === 'metabolism') {
                            if (event.status === 'started') {
                                setLoadingStatus(event.content || '記憶を整理しています...');
                            } else if (event.status === 'completed') {
                                setLoadingStatus('Thinking...');
                            }
                        } else if (event.type === 'warning') {
                            if (event.display === 'toast') {
                                const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
                                setToasts(prev => [...prev, { id, content: event.content || '' }]);
                                setTimeout(() => {
                                    setToasts(prev => prev.filter(t => t.id !== id));
                                }, 5000);
                            } else {
                                setMessages(prev => [...prev, {
                                    role: 'system',
                                    content: event.content || '',
                                    isWarning: true,
                                    warningCode: event.warning_code,
                                    timestamp: new Date().toISOString()
                                }]);
                            }
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
            await syncAfterResponse(); // Merge server state (IDs, avatars) without replacing messages
            isProcessingRef.current = false; // Allow polling AFTER sync completes
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

    // Close plus menu on outside click
    useEffect(() => {
        if (!showPlusMenu) return;
        const handleClickOutside = (e: MouseEvent) => {
            if (plusMenuRef.current && !plusMenuRef.current.contains(e.target as Node)) {
                setShowPlusMenu(false);
            }
        };
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [showPlusMenu]);

    // Close usage tooltip when tapping outside
    useEffect(() => {
        if (!usageTooltipId) return;
        const handleClickOutside = () => setUsageTooltipId(null);
        document.addEventListener('mousedown', handleClickOutside);
        return () => document.removeEventListener('mousedown', handleClickOutside);
    }, [usageTooltipId]);

    const handleContextPreview = async () => {
        setShowPlusMenu(false);
        setShowContextPreview(true);
        setContextPreviewLoading(true);
        setContextPreviewData(null);

        try {
            const attachmentTypes = attachments.map(a => a.type === 'image' ? 'image' : 'document');
            const res = await fetch('/api/chat/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: inputValue || '(empty)',
                    building_id: currentBuildingIdRef.current,
                    meta_playbook: selectedPlaybook || undefined,
                    attachment_count: attachments.length,
                    attachment_types: attachmentTypes,
                }),
            });
            if (!res.ok) {
                throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            setContextPreviewData(data);
        } catch (err) {
            console.error('Context preview failed:', err);
            setContextPreviewData({ personas: [] });
        } finally {
            setContextPreviewLoading(false);
        }
    };

    // Drag & Drop handlers (using counter to prevent flickering)
    const dragCounter = useRef(0);

    const handleDragEnter = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current++;
        if (dragCounter.current === 1) {
            setIsDragOver(true);
        }
    };

    const handleDragOver = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
    };

    const handleDragLeave = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current--;
        if (dragCounter.current === 0) {
            setIsDragOver(false);
        }
    };

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current = 0;
        setIsDragOver(false);

        const files = Array.from(e.dataTransfer.files);
        if (files.length === 0) return;

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
    };

    return (
        <div
            className={styles.container}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
        >
            <Sidebar
                onMove={(buildingId?: string) => {
                    if (!buildingId) return;
                    setCurrentBuildingId(buildingId);
                    currentBuildingIdRef.current = buildingId;
                    setMessages([]);
                    setIsHistoryLoaded(false);
                    fetchHistory(undefined, buildingId);
                    fetchBuildingInfo(buildingId);
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
                            className={styles.mobileMenuBtn}
                            onClick={() => setIsLeftOpen(true)}
                            title="Open Menu"
                        >
                            <Menu size={20} />
                        </button>
                        <h1>{currentBuildingName}</h1>
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
                            className={`${styles.iconBtn} ${isInfoOpen ? styles.active : ''}`}
                            onClick={() => setIsInfoOpen(!isInfoOpen)}
                            title="Toggle Info Sidebar"
                        >
                            <Info size={20} />
                        </button>
                    </div>
                </header>

                {!backendConnected && (
                    <div className={styles.backendErrorBanner}>
                        <AlertTriangle size={16} />
                        <div className={styles.backendErrorContent}>
                            <div>Backend server is not running.</div>
                            <div>Please make sure the &quot;SAIVerse Backend&quot; window is open. This page will reconnect automatically.</div>
                        </div>
                    </div>
                )}

                {showStartupWarnings && startupWarnings.length > 0 && (
                    <div className={styles.startupWarningBanner}>
                        <AlertTriangle size={16} />
                        <div className={styles.startupWarningContent}>
                            {startupWarnings.map((msg, i) => (
                                <div key={i}>{msg}</div>
                            ))}
                        </div>
                        <button
                            className={styles.startupWarningClose}
                            onClick={() => setShowStartupWarnings(false)}
                            title="Dismiss"
                        >
                            <X size={14} />
                        </button>
                    </div>
                )}

                <div
                    className={styles.chatArea}
                    ref={chatAreaRef}
                    onScroll={handleScroll}
                >
                    {isLoadingMore && <div style={{ textAlign: 'center', padding: '10px', color: '#666' }}>Loading history...</div>}
                    {messages.map((msg, idx) => (
                        <div key={msg.id || idx} className={`${styles.message} ${styles[msg.role]}`}>
                            <div className={`${styles.card} ${msg.isError ? styles.errorCard : ''} ${msg.isWarning ? styles.warningCard : ''} ${msg.isError && msg.errorCode ? styles[`error_${msg.errorCode}`] : ''}`}>
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
                                    {msg.isError ? (
                                        <div className={styles.errorContent}>
                                            <div className={styles.errorHeader}>
                                                <span className={styles.errorIcon}>
                                                    {msg.errorCode === 'rate_limit' && '⏱️'}
                                                    {msg.errorCode === 'timeout' && '⏰'}
                                                    {msg.errorCode === 'safety_filter' && '🛡️'}
                                                    {msg.errorCode === 'server_error' && '🔧'}
                                                    {msg.errorCode === 'empty_response' && '📭'}
                                                    {msg.errorCode === 'authentication' && '🔑'}
                                                    {msg.errorCode === 'payment' && '💳'}
                                                    {(!msg.errorCode || msg.errorCode === 'unknown' || !['rate_limit', 'timeout', 'safety_filter', 'server_error', 'empty_response', 'authentication', 'payment'].includes(msg.errorCode)) && '⚠️'}
                                                </span>
                                                <span className={styles.errorMessage}>{msg.content}</span>
                                            </div>
                                            {msg.errorDetail && (
                                                <details className={styles.errorDetails}>
                                                    <summary>Technical Details</summary>
                                                    <pre>{msg.errorDetail}</pre>
                                                </details>
                                            )}
                                        </div>
                                    ) : msg.isWarning ? (
                                        <div className={styles.warningContent}>
                                            <span className={styles.warningMessage}>{msg.content}</span>
                                        </div>
                                    ) : (
                                        <>
                                            {(msg.activity_trace || msg._activities) && (() => {
                                                const activities = msg.activity_trace || msg._activities || [];
                                                if (activities.length === 0) return null;
                                                const isStreaming = !!msg._streaming;
                                                return (
                                                    <details className={styles.activityBlock} open={isStreaming}>
                                                        <summary className={styles.activitySummary}>
                                                            <span className={styles.activityIcon}>
                                                                {isStreaming ? (
                                                                    <span className={styles.activitySpinner} />
                                                                ) : (
                                                                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                                                                )}
                                                            </span>
                                                            <span>{isStreaming ? 'Working...' : `${activities.length} step${activities.length > 1 ? 's' : ''}`}</span>
                                                        </summary>
                                                        <div className={styles.activityContent}>
                                                            {activities.map((a, i) => (
                                                                <div key={i} className={styles.activityItem}>
                                                                    <span className={styles.activityItemStatus}>
                                                                        {a.status === 'started' ? (
                                                                            <span className={styles.activitySpinner} />
                                                                        ) : (
                                                                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                                                                        )}
                                                                    </span>
                                                                    <span className={styles.activityItemLabel}>
                                                                        {a.name}
                                                                        {a.playbook && <span className={styles.activityItemPlaybook}> ({a.playbook})</span>}
                                                                    </span>
                                                                </div>
                                                            ))}
                                                        </div>
                                                    </details>
                                                );
                                            })()}
                                            {(msg.reasoning || msg._streamingThinking) && (
                                                <details className={styles.thinkingBlock} open={!!msg._streaming}>
                                                    <summary className={styles.thinkingSummary}>
                                                        <span className={styles.thinkingIcon}>
                                                            {msg._streaming ? (
                                                                <span className={styles.thinkingSpinner} />
                                                            ) : (
                                                                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                                                            )}
                                                        </span>
                                                        <span>{msg._streaming ? 'Thinking...' : 'Thought process'}</span>
                                                    </summary>
                                                    <div className={styles.thinkingContent}>
                                                        {msg.reasoning || msg._streamingThinking}
                                                    </div>
                                                </details>
                                            )}
                                            <ReactMarkdown
                                                remarkPlugins={[remarkGfm, remarkBreaks]}
                                                rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema]]}
                                                urlTransform={(url) => url.startsWith('saiverse://') ? url : defaultUrlTransform(url)}
                                                components={{
                                                    a: ({ href, children }) => <SaiverseLink href={href} children={children} onOpenItem={handleOpenItemFromLink} />,
                                                }}
                                            >{msg.content}</ReactMarkdown>
                                        </>
                                    )}
                                </div>
                                {(msg.timestamp || msg.llm_usage || msg.llm_usage_total) && (
                                    <div className={styles.cardFooter}>
                                        {msg.timestamp && <span>{new Date(msg.timestamp).toLocaleString()}</span>}
                                        {msg.llm_usage_total && msg.llm_usage_total.call_count > 1 ? (
                                            // Show total usage when multiple LLM calls were made
                                            <span className={styles.llmUsageWrap}>
                                                <span className={styles.llmUsage} onClick={(e) => { e.stopPropagation(); setUsageTooltipId(prev => prev === (msg.id || `msg-${idx}`) ? null : (msg.id || `msg-${idx}`)); }}>
                                                    {msg.llm_usage_total.call_count} calls · {(msg.llm_usage_total.total_input_tokens + msg.llm_usage_total.total_output_tokens).toLocaleString()} tokens · ${msg.llm_usage_total.total_cost_usd.toFixed(4)}
                                                </span>
                                                {usageTooltipId === (msg.id || `msg-${idx}`) && (
                                                    <div className={styles.usageTooltip}>
                                                        <div>Models: {msg.llm_usage_total.models_used.join(', ')}</div>
                                                        <div>LLM Calls: {msg.llm_usage_total.call_count}</div>
                                                        <div>Total Input: {msg.llm_usage_total.total_input_tokens.toLocaleString()} tokens{msg.llm_usage_total.total_cached_tokens ? ` (${msg.llm_usage_total.total_cached_tokens.toLocaleString()} cached)` : ''}</div>
                                                        <div>Total Output: {msg.llm_usage_total.total_output_tokens.toLocaleString()} tokens</div>
                                                        <div>Total Cost: ${msg.llm_usage_total.total_cost_usd.toFixed(4)}</div>
                                                    </div>
                                                )}
                                            </span>
                                        ) : msg.llm_usage && (
                                            // Show single call usage
                                            <span className={styles.llmUsageWrap}>
                                                <span className={styles.llmUsage} onClick={(e) => { e.stopPropagation(); setUsageTooltipId(prev => prev === (msg.id || `msg-${idx}`) ? null : (msg.id || `msg-${idx}`)); }}>
                                                    {msg.llm_usage.model_display_name || msg.llm_usage.model} · {(msg.llm_usage.input_tokens + msg.llm_usage.output_tokens).toLocaleString()} tokens
                                                </span>
                                                {usageTooltipId === (msg.id || `msg-${idx}`) && (
                                                    <div className={styles.usageTooltip}>
                                                        <div>Model: {msg.llm_usage.model}</div>
                                                        <div>Input: {msg.llm_usage.input_tokens.toLocaleString()} tokens{msg.llm_usage.cached_tokens ? ` (${msg.llm_usage.cached_tokens.toLocaleString()} cached)` : ''}</div>
                                                        <div>Output: {msg.llm_usage.output_tokens.toLocaleString()} tokens</div>
                                                        <div>Cost: ${(msg.llm_usage.cost_usd || 0).toFixed(4)}</div>
                                                    </div>
                                                )}
                                            </span>
                                        )}
                                    </div>
                                )}
                                <div className={styles.cardActions}>
                                    <button
                                        className={`${styles.actionBtn} ${copiedMessageId === (msg.id || `msg-${idx}`) ? styles.copied : ''}`}
                                        onClick={() => handleCopyMessage(msg.id || `msg-${idx}`, msg.content)}
                                        title="Copy message"
                                    >
                                        {copiedMessageId === (msg.id || `msg-${idx}`) ? <Check size={14} /> : <Copy size={14} />}
                                    </button>
                                </div>
                            </div>
                        </div>
                    ))}
                    {loadingStatus && <div className={styles.loading}>{loadingStatus}</div>}
                    <div ref={messagesEndRef} />
                </div>

                <div
                    className={styles.inputArea}
                    onDragEnter={handleDragEnter}
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                >
                    {/* Options bar: Model display + settings button */}
                    <div className={styles.optionsBar}>
                        <button
                            className={styles.optionsBtn}
                            onClick={() => setIsOptionsOpen(true)}
                            title="Chat Options"
                        >
                            <SlidersHorizontal size={16} />
                            {selectedModelDisplayName ? (
                                <span className={styles.modelName}>{selectedModelDisplayName}</span>
                            ) : null}
                            <ChevronDown size={14} className={styles.chevron} />
                        </button>
                    </div>

                    {attachments.length > 0 && (
                        <div style={{
                            fontSize: '0.8rem',
                            marginBottom: '0.5rem',
                            display: 'flex',
                            flexWrap: 'wrap',
                            gap: '0.5rem',
                            pointerEvents: 'auto'
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
                    <div className={`${styles.inputWrapper} ${isDragOver ? styles.inputWrapperDragOver : ''}`}>
                        {/* Drag & drop indicator */}
                        {isDragOver && (
                            <div className={styles.dropIndicator}>
                                Drop files here to attach
                            </div>
                        )}
                        <div className={styles.plusMenuContainer} ref={plusMenuRef}>
                            <button
                                className={`${styles.attachBtn} ${showPlusMenu ? styles.plusBtnActive : ''}`}
                                onClick={() => setShowPlusMenu(prev => !prev)}
                                title="More actions"
                            >
                                <Plus size={20} />
                            </button>
                            {showPlusMenu && (
                                <div className={styles.plusMenu}>
                                    <button
                                        className={styles.plusMenuItem}
                                        onClick={() => {
                                            setShowPlusMenu(false);
                                            fileInputRef.current?.click();
                                        }}
                                    >
                                        <Paperclip size={16} />
                                        <span>Attach File</span>
                                    </button>
                                    <button
                                        className={styles.plusMenuItem}
                                        onClick={handleContextPreview}
                                    >
                                        <Eye size={16} />
                                        <span>Context Preview</span>
                                    </button>
                                </div>
                            )}
                        </div>
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
                currentModel={selectedModel}
                onModelChange={(id, displayName) => {
                    setSelectedModel(id);
                    setSelectedModelDisplayName(displayName);
                }}
            />

            <PeopleModal
                isOpen={isPeopleModalOpen}
                onClose={() => setIsPeopleModalOpen(false)}
            />

            <ItemModal
                isOpen={!!linkItemModalItem}
                onClose={() => setLinkItemModalItem(null)}
                item={linkItemModalItem}
            />

            <ContextPreviewModal
                isOpen={showContextPreview}
                onClose={() => setShowContextPreview(false)}
                data={contextPreviewData}
                isLoading={contextPreviewLoading}
            />

            {/* Initial Tutorial Wizard */}
            {tutorialChecked && (
                <TutorialWizard
                    isOpen={showTutorial}
                    onClose={() => setShowTutorial(false)}
                    onComplete={(roomId) => {
                        setShowTutorial(false);
                        // Reload page to apply new settings
                        // User move is already handled in TutorialWizard.handleComplete,
                        // so after reload the page will load the persona's room
                        window.location.reload();
                    }}
                />
            )}

            {/* Toast notifications */}
            {toasts.length > 0 && (
                <div className={styles.toastContainer}>
                    {toasts.map(toast => (
                        <div key={toast.id} className={styles.toast}>
                            <AlertTriangle size={16} />
                            <span>{toast.content}</span>
                            <button
                                className={styles.toastClose}
                                onClick={() => setToasts(prev => prev.filter(t => t.id !== toast.id))}
                            >
                                <X size={14} />
                            </button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );
}
