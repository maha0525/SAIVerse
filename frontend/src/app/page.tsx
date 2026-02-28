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
import ToolModeSelector from '@/components/ToolModeSelector';
import RightSidebar from '@/components/RightSidebar';
import PeopleModal from '@/components/PeopleModal';
import TutorialWizard from '@/components/tutorial/TutorialWizard';
import SaiverseLink from '@/components/SaiverseLink';
import ItemModal from '@/components/ItemModal';
import ContextPreviewModal, { ContextPreviewData } from '@/components/ContextPreviewModal';
import PlaybookPermissionDialog, { PermissionRequestData } from '@/components/PlaybookPermissionDialog';
import TweetConfirmDialog, { TweetConfirmData } from '@/components/TweetConfirmDialog';
import ChronicleConfirmDialog, { ChronicleConfirmData } from '@/components/ChronicleConfirmDialog';
import ModalOverlay from '@/components/common/ModalOverlay';
import { Send, Plus, Paperclip, Eye, X, Info, Users, Menu, Copy, Check, SlidersHorizontal, ChevronDown, AlertTriangle, ArrowUpCircle, Loader, RefreshCw, Square, Bell } from 'lucide-react';
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
    'h', 'hpp', 'go', 'rs', 'rb', 'swift', 'kt', 'scala', 'r', 'lua', 'pl', 'pdf']);
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
    const [permissionRequest, setPermissionRequest] = useState<PermissionRequestData | null>(null);
    const [tweetConfirm, setTweetConfirm] = useState<TweetConfirmData | null>(null);
    const [chronicleConfirm, setChronicleConfirm] = useState<ChronicleConfirmData | null>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const chatAreaRef = useRef<HTMLDivElement>(null); // Ref for the scrollable area
    const [isHistoryLoaded, setIsHistoryLoaded] = useState(false);

    // Pagination State
    const [hasMore, setHasMore] = useState(true);
    const [isLoadingMore, setIsLoadingMore] = useState(false);
    const previousScrollHeightRef = useRef<number>(0);
    const prevNewestIdRef = useRef<string | undefined>(undefined); // Track newest message ID
    const isProcessingRef = useRef(false); // Suppress polling during active request

    // User identity cache (for optimistic message display)
    const userDisplayNameRef = useRef<string>('');
    const userAvatarRef = useRef<string>('');

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

    // Auto-resize textarea based on content (max 10 lines)
    const adjustTextareaHeight = useCallback(() => {
        const textarea = textareaRef.current;
        if (!textarea) return;

        const lineHeight = 24; // px (1.5 * ~16px font-size)
        const maxLines = 10;
        const maxHeight = lineHeight * maxLines;

        // Temporarily override styles for accurate scrollHeight measurement
        // - min-height: 0 prevents CSS min-height from inflating scrollHeight
        // - overflow: hidden prevents scrollbar from affecting measurement
        // - height: 0 collapses textarea to measure true content height
        const prevMinHeight = textarea.style.minHeight;
        const prevOverflow = textarea.style.overflow;
        textarea.style.minHeight = '0';
        textarea.style.overflow = 'hidden';
        textarea.style.height = '0';

        const scrollH = textarea.scrollHeight;

        // Restore
        textarea.style.minHeight = prevMinHeight;
        textarea.style.overflow = prevOverflow;

        const newHeight = Math.max(lineHeight, Math.min(scrollH, maxHeight));
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

    // Check tutorial status on mount and when backend reconnects
    useEffect(() => {
        // Skip if tutorial is already showing
        if (showTutorial) return;

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
    }, [backendConnected]);

    // Startup warnings
    const [startupWarnings, setStartupWarnings] = useState<string[]>([]);
    const [showStartupWarnings, setShowStartupWarnings] = useState(false);

    // Timezone mismatch popup
    const [tzMismatch, setTzMismatch] = useState<{cityTz: string; browserTz: string; cityId: number} | null>(null);
    const [tzUpdating, setTzUpdating] = useState(false);

    // Reembed notification
    const [reembedNeeded, setReembedNeeded] = useState<{persona_ids: string[], message: string} | null>(null);
    const [isReembeddingAll, setIsReembeddingAll] = useState(false);
    const [reembedBannerProgress, setReembedBannerProgress] = useState<string | null>(null);

    // Update system
    const [app_state_version, setAppStateVersion] = useState('');
    const [updateAvailable, setUpdateAvailable] = useState<{version: string; url: string} | null>(null);
    const [isUpdating, setIsUpdating] = useState(() => {
        if (typeof window !== 'undefined') {
            return sessionStorage.getItem('saiverse_updating') === 'true';
        }
        return false;
    });
    const updatingTargetVersion = useRef<string>('');

    // Announcements unread badge
    const [hasUnreadAnnouncements, setHasUnreadAnnouncements] = useState(false);

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


    type HistoryResponse = {
        history?: Message[];
        has_more?: boolean;
        error?: string;
    };

    const resolveHasMore = (data: HistoryResponse, newMessages: Message[]) => {
        return data.has_more !== undefined ? data.has_more : newMessages.length >= 20;
    };

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
                const data: HistoryResponse = await res.json();
                const newMessages: Message[] = data.history || [];
                const effectiveHasMore = resolveHasMore(data, newMessages);
                console.log(`[DEBUG] Fetched ${newMessages.length} items (beforeId=${beforeId}, server has_more=${data.has_more}, effectiveHasMore=${effectiveHasMore})`);

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
                const errorPayload: HistoryResponse | null = await res.json().catch(() => null);
                console.error("[DEBUG] Fetch failed", {
                    status: res.status,
                    beforeId,
                    buildingId: bid,
                    error: errorPayload?.error,
                });

                if (res.status >= 500) {
                    setBackendConnected(false);
                    setHasMore(false);
                }
                if (!beforeId) setMessages([]);
                setIsHistoryLoaded(true);
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
                            images: entry.msg.images || local.images,
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
                if (data?.display_name) userDisplayNameRef.current = data.display_name;
                if (data?.avatar) userAvatarRef.current = data.avatar;
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

        // Check timezone mismatch
        fetch('/api/db/tables/city')
            .then(res => res.ok ? res.json() : null)
            .then(cities => {
                if (!cities || cities.length === 0) return;
                const city = cities[0];
                const cityTz = city.TIMEZONE || 'UTC';
                const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
                if (cityTz !== browserTz) {
                    const dismissKey = `saiverse_tz_dismissed_${cityTz}_${browserTz}`;
                    if (!localStorage.getItem(dismissKey)) {
                        setTzMismatch({ cityTz, browserTz, cityId: city.CITYID });
                    }
                }
            })
            .catch(err => console.error('Timezone check failed:', err));

        // Check if embedding model changed and reembed is needed
        fetch('/api/config/reembed-check')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data?.needed) {
                    setReembedNeeded({ persona_ids: data.persona_ids, message: data.message });
                }
            })
            .catch(err => console.error('Failed to check reembed', err));

        // Check for updates
        fetch('/api/system/version')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data?.version) {
                    setAppStateVersion(data.version);
                }
                if (data?.update_available) {
                    setUpdateAvailable({
                        version: data.latest_version,
                        url: data.latest_release_url || '',
                    });
                }
            })
            .catch(() => { /* ignore - backend may not support this endpoint yet */ });

        // Check for unread announcements (and poll every 30 minutes)
        const checkAnnouncements = () => {
            fetch('/api/system/announcements')
                .then(res => res.ok ? res.json() : null)
                .then(data => {
                    if (data?.announcements?.length > 0) {
                        const raw = JSON.stringify(data.announcements);
                        let hash = 5381;
                        for (let i = 0; i < raw.length; i++) {
                            hash = ((hash << 5) + hash + raw.charCodeAt(i)) | 0;
                        }
                        const currentHash = (hash >>> 0).toString(16);
                        const savedHash = localStorage.getItem('saiverse_announcements_hash');
                        setHasUnreadAnnouncements(currentHash !== savedHash);
                    }
                })
                .catch(() => { /* ignore */ });
        };
        checkAnnouncements();
        const announcementInterval = setInterval(checkAnnouncements, 30 * 60 * 1000);

        // Also check when the tab becomes visible again
        const onVisibilityChange = () => {
            if (document.visibilityState === 'visible') checkAnnouncements();
        };
        document.addEventListener('visibilitychange', onVisibilityChange);

        return () => {
            clearInterval(announcementInterval);
            document.removeEventListener('visibilitychange', onVisibilityChange);
        };
    }, []);

    // Handle building deletion from WorldEditor — switch to another building
    // if the current building was the one deleted.
    useEffect(() => {
        const handleBuildingDeleted = async (e: Event) => {
            const deletedId = (e as CustomEvent).detail?.buildingId;
            if (!deletedId) return;

            if (currentBuildingIdRef.current === deletedId) {
                // Current building was deleted — move to the first available building
                try {
                    const res = await fetch('/api/user/buildings');
                    if (res.ok) {
                        const data = await res.json();
                        const buildings = data.buildings || [];
                        if (buildings.length > 0) {
                            const target = buildings[0];
                            const moveRes = await fetch('/api/user/move', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ target_building_id: target.id }),
                            });
                            if (moveRes.ok) {
                                setCurrentBuildingId(target.id);
                                currentBuildingIdRef.current = target.id;
                                setMessages([]);
                                setIsHistoryLoaded(false);
                                fetchHistory(undefined, target.id);
                                fetchBuildingInfo(target.id);
                                setMoveTrigger(prev => prev + 1);
                            }
                        }
                    }
                } catch (err) {
                    console.error('Failed to handle building deletion', err);
                }
            } else {
                // Another building was deleted — just refresh building info
                // to ensure sidebar/right panel are up to date
                fetchBuildingInfo();
                setMoveTrigger(prev => prev + 1);
            }
        };
        window.addEventListener('building-deleted', handleBuildingDeleted);
        return () => window.removeEventListener('building-deleted', handleBuildingDeleted);
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

    // Backend reconnection polling
    // During update: poll regardless of connection status (detect shutdown + restart)
    // Otherwise: only poll when disconnected
    useEffect(() => {
        if (!isUpdating && backendConnected) return;

        const reconnectInterval = setInterval(async () => {
            try {
                const res = await fetch('/api/user/status');
                if (res.ok) {
                    if (!backendConnected) {
                        setBackendConnected(true);
                        // Refresh data after reconnection
                        fetchHistory();
                        fetchBuildingInfo();

                        // If we were updating, show completion toast
                        if (isUpdating) {
                            setIsUpdating(false);
                            sessionStorage.removeItem('saiverse_updating');
                            const toastId = `update-complete-${Date.now()}`;
                            setToasts(prev => [...prev, { id: toastId, content: 'Update complete! Application has been restarted.' }]);
                            setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 5000);
                        }
                    }
                    // backendConnected && isUpdating: backend hasn't shut down yet, keep waiting
                }
            } catch {
                // Backend not responding
                if (backendConnected) {
                    setBackendConnected(false);
                }
            }
        }, isUpdating ? 5000 : 10000); // Poll faster during update

        return () => clearInterval(reconnectInterval);
    }, [backendConnected, isUpdating]);

    // --- Reembed handlers ---
    const handleReembedAll = async () => {
        if (!reembedNeeded || isReembeddingAll) return;
        setIsReembeddingAll(true);
        setReembedBannerProgress('Starting...');

        for (const personaId of reembedNeeded.persona_ids) {
            try {
                setReembedBannerProgress(`Re-embedding ${personaId}...`);
                const res = await fetch(`/api/people/${personaId}/reembed`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ force: true }),
                });
                if (!res.ok) continue;

                // Poll until done
                let done = false;
                while (!done) {
                    await new Promise(r => setTimeout(r, 1500));
                    const statusRes = await fetch(`/api/people/${personaId}/reembed/status`);
                    if (!statusRes.ok) break;
                    const status = await statusRes.json();
                    if (status.running) {
                        setReembedBannerProgress(`${personaId}: ${status.message || `${status.progress}/${status.total}`}`);
                    } else {
                        done = true;
                    }
                }
            } catch (err) {
                console.error(`Reembed failed for ${personaId}`, err);
            }
        }

        setIsReembeddingAll(false);
        setReembedBannerProgress(null);
        setReembedNeeded(null);
    };

    const handleReembedLater = () => {
        setReembedNeeded(null);
        const toastId = `reembed-later-${Date.now()}`;
        setToasts(prev => [...prev, { id: toastId, content: '設定 > メモリ管理 > エンベディング管理から再実行できます。' }]);
        setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 8000);
    };

    const handleTriggerUpdate = async () => {
        if (!updateAvailable) return;
        const confirmed = window.confirm(
            `Update to v${updateAvailable.version}?\n\nThe application will restart automatically. This may take a few minutes.`
        );
        if (!confirmed) return;

        try {
            const res = await fetch('/api/system/update', { method: 'POST' });
            if (res.ok) {
                updatingTargetVersion.current = updateAvailable.version;
                setIsUpdating(true);
                sessionStorage.setItem('saiverse_updating', 'true');
                setUpdateAvailable(null);
            } else {
                const toastId = `update-error-${Date.now()}`;
                setToasts(prev => [...prev, { id: toastId, content: 'Failed to start update. Check backend logs.' }]);
                setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 5000);
            }
        } catch {
            const toastId = `update-error-${Date.now()}`;
            setToasts(prev => [...prev, { id: toastId, content: 'Failed to start update. Backend may be unreachable.' }]);
            setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 5000);
        }
    };

    const handleTzUpdate = async () => {
        if (!tzMismatch) return;
        setTzUpdating(true);
        try {
            const citiesRes = await fetch('/api/db/tables/city');
            if (!citiesRes.ok) throw new Error('Failed to fetch city data');
            const cities = await citiesRes.json();
            const city = cities.find((c: any) => c.CITYID === tzMismatch.cityId);
            if (!city) throw new Error('City not found');

            const res = await fetch(`/api/world/cities/${city.CITYID}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: city.CITYNAME,
                    description: city.DESCRIPTION || '',
                    online_mode: city.START_IN_ONLINE_MODE ?? false,
                    ui_port: city.UI_PORT,
                    api_port: city.API_PORT,
                    timezone: tzMismatch.browserTz,
                })
            });
            if (!res.ok) throw new Error('Failed to update timezone');

            const toastId = `tz-update-${Date.now()}`;
            setToasts(prev => [...prev, { id: toastId, content: `タイムゾーンを ${tzMismatch.browserTz} に更新しました` }]);
            setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 5000);
            setTzMismatch(null);
        } catch (err) {
            console.error('Failed to update timezone:', err);
            const toastId = `tz-error-${Date.now()}`;
            setToasts(prev => [...prev, { id: toastId, content: 'タイムゾーンの更新に失敗しました' }]);
            setTimeout(() => setToasts(prev => prev.filter(t => t.id !== toastId)), 5000);
        } finally {
            setTzUpdating(false);
        }
    };

    const handleTzDismiss = () => {
        if (tzMismatch) {
            const dismissKey = `saiverse_tz_dismissed_${tzMismatch.cityTz}_${tzMismatch.browserTz}`;
            localStorage.setItem(dismissKey, 'true');
        }
        setTzMismatch(null);
    };

    const handlePermissionResponse = useCallback(async (requestId: string, decision: string) => {
        setPermissionRequest(null);
        try {
            await fetch('/api/chat/permission-response', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, decision }),
            });
        } catch (e) {
            console.error('Failed to send permission response', e);
        }
    }, []);

    const handleTweetConfirmResponse = useCallback(async (requestId: string, decision: string, editedText?: string) => {
        setTweetConfirm(null);
        try {
            await fetch('/api/chat/tweet-confirmation-response', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, decision, edited_text: editedText }),
            });
        } catch (e) {
            console.error('Failed to send tweet confirmation response', e);
        }
    }, []);

    const handleChronicleConfirmResponse = useCallback(async (requestId: string, decision: string) => {
        setChronicleConfirm(null);
        try {
            await fetch('/api/chat/permission-response', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, decision }),
            });
        } catch (e) {
            console.error('Failed to send chronicle confirm response', e);
        }
    }, []);

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && attachments.length === 0) || loadingStatus) return;
        isProcessingRef.current = true;

        // Optimistic update
        // Temporary ID for key prop until refreshed
        const tempId = `temp-${Date.now()}`;
        const userMsg: Message = {
            id: tempId, role: 'user', content: inputValue,
            sender: userDisplayNameRef.current || undefined,
            avatar: userAvatarRef.current || undefined,
            images: attachments
                .filter(a => a.type === 'image')
                .map(a => ({ url: `data:${a.mimeType};base64,${a.base64}`, mime_type: a.mimeType })),
        };
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
                                    const actAvatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
                                    return [...prev, {
                                        role: 'assistant' as const, content: '', _streaming: true,
                                        sender: event.persona_name || undefined,
                                        avatar: actAvatarUrl,
                                        _activities: [entry], timestamp: new Date().toISOString()
                                    }];
                                }
                            });
                            setLoadingStatus(event.status === 'started' ? `Running ${event.name}...` : event.name);
                        } else if (event.type === 'streaming_thinking') {
                            // Streaming thinking: accumulate into _streamingThinking
                            const avatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
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
                            const avatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
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
                        } else if (event.type === 'streaming_discard') {
                            // Tool call detected after streaming — discard streamed text
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    return prev.slice(0, -1);
                                }
                                return prev;
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'streaming_complete') {
                            // Extract images from metadata if present (e.g., from image generation)
                            let streamCompleteImages: MessageImage[] | undefined;
                            const scMeta = event.metadata;
                            if (scMeta && (scMeta.images || scMeta.media)) {
                                const mediaItems = scMeta.images || scMeta.media || [];
                                streamCompleteImages = [];
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
                                        streamCompleteImages.push({
                                            url: `/api/static/uploads/${filename}`,
                                            mime_type: img.mime_type
                                        });
                                    }
                                }
                                if (streamCompleteImages.length === 0) streamCompleteImages = undefined;
                            }
                            // Mark streaming message as complete, finalize reasoning, activities, and images
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    const { _streaming, _streamingThinking, _activities, ...rest } = last;
                                    const reasoning = event.reasoning || _streamingThinking || undefined;
                                    return [...prev.slice(0, -1), {
                                        ...rest,
                                        reasoning,
                                        ...((_activities && _activities.length > 0) && { activity_trace: _activities }),
                                        ...(streamCompleteImages && { images: streamCompleteImages }),
                                    }];
                                }
                                return prev;
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'say') {
                            console.log('[DEBUG] Received say event:', event);
                            const avatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);

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
                            if (event.status === 'completed') {
                                // Show completion message briefly, then transition
                                if (event.content) {
                                    setLoadingStatus(event.content);
                                    setTimeout(() => setLoadingStatus('Thinking...'), 2000);
                                } else {
                                    setLoadingStatus('Thinking...');
                                }
                            } else {
                                // started, running, etc. — show content as loading status
                                setLoadingStatus(event.content || '記憶を整理しています...');
                            }
                        } else if (event.type === 'permission_request') {
                            setPermissionRequest({
                                requestId: event.request_id,
                                playbookName: event.playbook_name,
                                playbookDisplayName: event.playbook_display_name || event.playbook_name,
                                playbookDescription: event.playbook_description || '',
                                personaName: event.persona_name || '',
                            });
                        } else if (event.type === 'tweet_confirmation') {
                            setTweetConfirm({
                                requestId: event.request_id,
                                tweetText: event.tweet_text,
                                personaId: event.persona_id || '',
                                xUsername: event.x_username || '',
                            });
                        } else if (event.type === 'chronicle_confirm') {
                            setChronicleConfirm({
                                requestId: event.request_id,
                                unprocessedMessages: event.unprocessed_messages,
                                totalMessages: event.total_messages,
                                estimatedLlmCalls: event.estimated_llm_calls,
                                modelName: event.model_name || '',
                                personaName: event.persona_name || '',
                            });
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
                        } else if (event.type === 'cancelled') {
                            // Server-side cancellation: finalize streaming message
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    const { _streaming, _streamingThinking, _activities, ...rest } = last;
                                    return [...prev.slice(0, -1), {
                                        ...rest,
                                        ...((_streamingThinking) && { reasoning: _streamingThinking }),
                                        ...((_activities && _activities.length > 0) && { activity_trace: _activities }),
                                    }];
                                }
                                return prev;
                            });
                            setLoadingStatus(null);
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
            // Finalize any orphaned _streaming messages left after the stream ends
            // (e.g. activity events that arrived after the last streaming_complete)
            setMessages(prev => {
                const lastIdx = prev.length - 1;
                if (lastIdx >= 0 && prev[lastIdx]._streaming) {
                    const msg = prev[lastIdx];
                    const { _streaming, _streamingThinking, _activities, ...rest } = msg;
                    // Empty content + no activities → discard entirely
                    if (!rest.content && (!_activities || _activities.length === 0)) {
                        return prev.slice(0, -1);
                    }
                    // Has activities or content → finalize as completed message
                    return [...prev.slice(0, -1), {
                        ...rest,
                        ...(_streamingThinking && { reasoning: _streamingThinking }),
                        ...((_activities && _activities.length > 0) && { activity_trace: _activities }),
                    }];
                }
                return prev;
            });
            await syncAfterResponse(); // Merge server state (IDs, avatars) without replacing messages
            isProcessingRef.current = false; // Allow polling AFTER sync completes
        }
    };

    const handleStopGeneration = async () => {
        // Signal backend to cancel active LLM generation
        // Don't abort() the fetch — let the backend's cancellation flow
        // send streaming_complete and cancelled events naturally.
        try {
            await fetch('/api/chat/stop', { method: 'POST' });
        } catch (e) {
            console.error('Failed to send stop request:', e);
        }
    };

    const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
        // Ctrl+Enter (or Cmd+Enter) sends the message on any device.
        // Regular Enter always inserts a newline.
        // No isMobile gate needed: actual mobile devices have no Ctrl key,
        // while touch-screen laptops do need Ctrl+Enter to work.
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            handleSendMessage();
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
                refreshTrigger={backendConnected}
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
                        {hasUnreadAnnouncements && (
                            <button
                                className={styles.iconBtn}
                                onClick={() => { window.location.href = '/announcements'; }}
                                title="お知らせ（未読あり）"
                            >
                                <span className={styles.bellWrapper}>
                                    <Bell size={20} />
                                    <span className={styles.bellDot} />
                                </span>
                            </button>
                        )}
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

                {isUpdating && (
                    <div className={styles.updatingBanner}>
                        <Loader size={16} className={styles.spinIcon} />
                        <div className={styles.updatingContent}>
                            <div>Updating{updatingTargetVersion.current ? ` to v${updatingTargetVersion.current}` : ''}... Please wait.</div>
                            <div>The application will restart automatically.</div>
                        </div>
                    </div>
                )}

                {!backendConnected && !isUpdating && (
                    <div className={styles.backendErrorBanner}>
                        <AlertTriangle size={16} />
                        <div className={styles.backendErrorContent}>
                            <div>Backend server is not running.</div>
                            <div>Please make sure the &quot;SAIVerse Backend&quot; window is open. This page will reconnect automatically.</div>
                        </div>
                    </div>
                )}

                {updateAvailable && !isUpdating && (
                    <div className={styles.updateAvailableBanner}>
                        <ArrowUpCircle size={16} />
                        <div className={styles.updateAvailableContent}>
                            <div>New version available: v{updateAvailable.version} (current: v{app_state_version})</div>
                        </div>
                        <button
                            className={styles.updateButton}
                            onClick={handleTriggerUpdate}
                        >
                            Update
                        </button>
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

                {reembedNeeded && (
                    <div className={styles.reembedBanner}>
                        <RefreshCw size={16} className={isReembeddingAll ? styles.spinIcon : undefined} />
                        <div className={styles.reembedBannerContent}>
                            <div>{reembedNeeded.message}</div>
                            {reembedBannerProgress && <div>{reembedBannerProgress}</div>}
                        </div>
                        <button
                            className={styles.reembedRunButton}
                            onClick={handleReembedAll}
                            disabled={isReembeddingAll}
                        >
                            {isReembeddingAll ? 'Processing...' : '再計算する'}
                        </button>
                        {!isReembeddingAll && (
                            <button
                                className={styles.reembedLaterButton}
                                onClick={handleReembedLater}
                            >
                                後で
                            </button>
                        )}
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
                                        src={msg.avatar || (msg.role === 'user' ? '/api/static/builtin_icons/user.png' : '/api/static/builtin_icons/host.png')}
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
                                                    {({rate_limit: '⏱️', timeout: '⏰', safety_filter: '🛡️', server_error: '🔧', empty_response: '📭', authentication: '🔑', payment: '💳'} as Record<string, string>)[msg.errorCode || ''] || '⚠️'}
                                                </span>
                                                <span className={styles.errorMessage}>{msg.content}</span>
                                            </div>
                                            <div style={{ fontSize: '0.85em', opacity: 0.75, lineHeight: 1.4, marginTop: '4px' }}>
                                                {({
                                                    empty_response: 'しばらく時間を置いてから再送信してください。繰り返し発生する場合は、サーバーの障害情報を確認してください。',
                                                    safety_filter: '送信した内容が安全性フィルターに該当した可能性があります。内容を変更して再送信してください。',
                                                    timeout: 'サーバーが混雑している可能性があります。しばらく時間を置いてから再送信してください。',
                                                    rate_limit: 'API利用制限に達しています。しばらく時間を置いてから再送信してください。',
                                                    payment: 'APIキーの残高や支払い設定を確認してください。',
                                                    authentication: 'APIキーの設定を確認してください。',
                                                    server_error: 'LLMサーバーで障害が発生しています。しばらく時間を置いてから再送信してください。',
                                                } as Record<string, string>)[msg.errorCode || ''] || '予期しないエラーが発生しました。問題が続く場合は管理者に連絡してください。'}
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
                    {loadingStatus && (
                        <div className={styles.loading} role="status" aria-label={loadingStatus}>
                            <span className={styles.loadingSpinner} aria-hidden="true" />
                            {loadingStatus !== 'Thinking...' && (
                                <span className={styles.loadingText}>{loadingStatus}</span>
                            )}
                        </div>
                    )}
                    <div ref={messagesEndRef} />
                </div>

                <div
                    className={styles.inputArea}
                    onDragEnter={handleDragEnter}
                    onDragOver={handleDragOver}
                    onDragLeave={handleDragLeave}
                    onDrop={handleDrop}
                >
                    {/* Options bar: Model display + settings button + tool mode */}
                    <div className={styles.optionsBar}>
                        <button
                            className={styles.optionsBtn}
                            onClick={() => setIsOptionsOpen(true)}
                            title="チャット設定"
                        >
                            <SlidersHorizontal size={16} />
                            {selectedModelDisplayName ? (
                                <span className={styles.modelName}>{selectedModelDisplayName}</span>
                            ) : null}
                            <ChevronDown size={14} className={styles.chevron} />
                        </button>
                        <ToolModeSelector
                            selectedPlaybook={selectedPlaybook}
                            onPlaybookChange={setSelectedPlaybook}
                            playbookParams={playbookParams}
                            onPlaybookParamsChange={setPlaybookParams}
                        />
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
                                }}>すべて削除</button>
                            )}
                        </div>
                    )}
                    <div className={`${styles.inputWrapper} ${isDragOver ? styles.inputWrapperDragOver : ''}`}>
                        {/* Drag & drop indicator */}
                        {isDragOver && (
                            <div className={styles.dropIndicator}>
                                ここにファイルをドロップして添付
                            </div>
                        )}
                        <div className={styles.plusMenuContainer} ref={plusMenuRef}>
                            <button
                                className={`${styles.attachBtn} ${showPlusMenu ? styles.plusBtnActive : ''}`}
                                onClick={() => setShowPlusMenu(prev => !prev)}
                                title="その他の操作"
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
                                        <span>ファイルを添付</span>
                                    </button>
                                    <button
                                        className={styles.plusMenuItem}
                                        onClick={handleContextPreview}
                                    >
                                        <Eye size={16} />
                                        <span>コンテキストプレビュー</span>
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
                            accept="image/*,.txt,.md,.py,.js,.ts,.tsx,.json,.yaml,.yml,.csv,.html,.css,.xml,.log,.sh,.bat,.sql,.java,.c,.cpp,.h,.hpp,.go,.rs,.rb,.swift,.kt,.scala,.r,.lua,.pl,.pdf"
                        />
                        <textarea
                            ref={textareaRef}
                            value={inputValue}
                            onChange={(e) => setInputValue(e.target.value)}
                            onKeyDown={handleKeyDown}
                            placeholder="メッセージを入力..."
                            rows={1}
                        />
                        {loadingStatus ? (
                            <button
                                className={styles.stopBtn}
                                onClick={handleStopGeneration}
                                title="生成を停止"
                            >
                                <Square size={16} />
                            </button>
                        ) : (
                            <button
                                className={styles.sendBtn}
                                onClick={handleSendMessage}
                                disabled={!inputValue.trim() && attachments.length === 0}
                            >
                                <Send size={20} />
                            </button>
                        )}
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

            {permissionRequest && (
                <PlaybookPermissionDialog
                    request={permissionRequest}
                    onRespond={handlePermissionResponse}
                />
            )}

            {tweetConfirm && (
                <TweetConfirmDialog
                    request={tweetConfirm}
                    onRespond={handleTweetConfirmResponse}
                />
            )}

            {chronicleConfirm && (
                <ChronicleConfirmDialog
                    request={chronicleConfirm}
                    onRespond={handleChronicleConfirmResponse}
                />
            )}

            {/* Initial Tutorial Wizard */}
            {tutorialChecked && (
                <TutorialWizard
                    isOpen={showTutorial}
                    onClose={() => setShowTutorial(false)}
                    onComplete={(roomId) => {
                        // Reload page to apply new settings.
                        // Do NOT call setShowTutorial(false) before reload — keeping
                        // showTutorial=true prevents stale tzMismatch state from
                        // flashing the timezone modal for one frame before the reload.
                        window.location.reload();
                    }}
                />
            )}

            {/* Timezone Mismatch Popup */}
            {tzMismatch && !showTutorial && (
                <ModalOverlay onClose={handleTzDismiss}>
                    <div className={styles.tzPopup} onClick={(e) => e.stopPropagation()}>
                        <h3 className={styles.tzPopupTitle}>タイムゾーンの不一致</h3>
                        <p className={styles.tzPopupText}>
                            City のタイムゾーンは <strong>{tzMismatch.cityTz}</strong> に設定されていますが、
                            システムのタイムゾーンは <strong>{tzMismatch.browserTz}</strong> です。
                        </p>
                        <p className={styles.tzPopupText}>
                            タイムゾーンを更新しますか？
                        </p>
                        <div className={styles.tzPopupActions}>
                            <button
                                className={styles.tzPopupDismiss}
                                onClick={handleTzDismiss}
                            >
                                閉じる
                            </button>
                            <button
                                className={styles.tzPopupUpdate}
                                onClick={handleTzUpdate}
                                disabled={tzUpdating}
                            >
                                {tzUpdating ? '更新中...' : `${tzMismatch.browserTz} に更新`}
                            </button>
                        </div>
                    </div>
                </ModalOverlay>
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
