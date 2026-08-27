"use client";

import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, useCallback, useMemo, ReactNode } from 'react';
import ReactMarkdown, { defaultUrlTransform, Components } from 'react-markdown';
import type { PluggableList } from 'unified';
import remarkBreaks from 'remark-breaks';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import ChatOptions from '@/components/ChatOptions';
import ToolModeSelector, { TOOL_MODE_SELECTED } from '@/components/ToolModeSelector';
import { buildPreSpellsFromUI } from '@/lib/preSpells';
import { formatCost } from '@/lib/formatCost';
import { prepareMessageMarkdown } from '@/lib/messageMarkdown';
import RightSidebar from '@/components/RightSidebar';
import CityMap from '@/components/CityMap';
import cityMapStyles from '@/components/CityMap.module.css';
import PeopleModal from '@/components/PeopleModal';
import TutorialWizard from '@/components/tutorial/TutorialWizard';
import SaiverseLink from '@/components/SaiverseLink';
import ItemModal from '@/components/ItemModal';
import ContextPreviewModal, { ContextPreviewData } from '@/components/ContextPreviewModal';
import PlaybookPermissionDialog, { PermissionRequestData } from '@/components/PlaybookPermissionDialog';
import SpellConfirmDialog, { SpellConfirmData } from '@/components/SpellConfirmDialog';
import ChronicleConfirmDialog, { ChronicleConfirmData } from '@/components/ChronicleConfirmDialog';
import ModalOverlay from '@/components/common/ModalOverlay';
import { Send, Plus, Paperclip, Eye, X, Info, Users, Menu, Copy, Check, SlidersHorizontal, ChevronDown, AlertTriangle, ArrowUpCircle, Loader, RefreshCw, Square, Bell, Map as MapIcon, CornerDownRight, RotateCcw, Undo2 } from 'lucide-react';
import { useActivityTracker } from '@/hooks/useActivityTracker';
import { useAddonEvents } from '@/hooks/useAddonEvents';
import { useActiveClientTab } from '@/hooks/useActiveClientTab';
import { useClientActions } from '@/hooks/useClientActions';
import { ActiveClientIndicator } from '@/components/ActiveClientIndicator';
import AddonBubbleButtons, { BubbleButtonDef } from '@/components/AddonBubbleButtons';
import SystemAlertBanner from '@/components/SystemAlertBanner';

// Allow className on HTML elements used by thinking blocks (<details>, <div>, <summary>)
const sanitizeSchema = {
    ...defaultSchema,
    tagNames: [...(defaultSchema.tagNames || []), 'svg', 'path', 'polyline', 'circle'],
    attributes: {
        ...defaultSchema.attributes,
        details: [...(defaultSchema.attributes?.details || []), 'className'],
        div: [...(defaultSchema.attributes?.div || []), 'className'],
        summary: [...(defaultSchema.attributes?.summary || []), 'className'],
        span: [...(defaultSchema.attributes?.span || []), 'className'],
        code: [...(defaultSchema.attributes?.code || []), 'className'],
        svg: ['width', 'height', 'viewBox', 'fill', 'stroke', 'strokeWidth', 'stroke-width', 'className'],
        path: ['d', 'fill', 'stroke', 'strokeWidth', 'stroke-width'],
    },
    protocols: {
        ...defaultSchema.protocols,
        href: [...(defaultSchema.protocols?.href || []), 'saiverse'],
        src: [...(defaultSchema.protocols?.src || []), 'saiverse'],
    },
};

/**
 * Convert a saiverse:// URI to an actual API URL for <img src>.
 * - saiverse://item/<id>/content → /api/info/item/<id>  (FileResponse for picture items)
 * - saiverse://image/<filename>  → /api/static/uploads/<filename>
 * - other (https://, etc.) → returned as-is
 */
function resolveSaiverseImageSrc(src: string): string {
    if (!src) return src;
    const itemMatch = src.match(/^saiverse:\/\/item\/([^/]+)/);
    if (itemMatch) {
        return `/api/info/item/${itemMatch[1]}`;
    }
    if (src.startsWith('saiverse://image/')) {
        const remainder = src.replace('saiverse://image/', '');
        const filename = remainder.split('/').pop() || remainder.split('\\').pop() || remainder;
        return `/api/static/uploads/${filename}`;
    }
    return src;
}

// Stable module-level constants — passing fresh arrays/functions to ReactMarkdown each render
// causes it to remount its rendered tree, which makes <img> tags re-fetch on every keystroke.
const MARKDOWN_REMARK_PLUGINS: PluggableList = [remarkGfm, remarkBreaks];
const MARKDOWN_REHYPE_PLUGINS: PluggableList = [rehypeRaw, [rehypeSanitize, sanitizeSchema]];
const markdownUrlTransform = (url: string) =>
    url.startsWith('saiverse://') ? url : defaultUrlTransform(url);

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
    currency?: string;
}

interface MessageLLMUsageTotal {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cached_tokens?: number;  // Total cached tokens across all calls
    total_cost_usd: number;
    call_count: number;
    models_used: string[];
    currency?: string;
}

interface Message {
    id?: string;
    role: 'user' | 'assistant' | 'system' | 'host';
    content: string;
    timestamp?: string; // ISO string
    avatar?: string;
    sender?: string;
    persona_id?: string; // assistant メッセージで発話ペルソナを識別 (アドオン bubble button context 等で使用)
    images?: MessageImage[];
    audios?: MessageImage[];   // 音声添付。MessageImage と同じ {url, mime_type} 形式を流用
    videos?: MessageImage[];   // 動画添付
    llm_usage?: MessageLLMUsage;
    llm_usage_total?: MessageLLMUsageTotal;
    // Error information
    isError?: boolean;
    errorCode?: string;
    errorDetail?: string;
    // Warning information
    isWarning?: boolean;
    warningCode?: string;
    // Info notification (e.g. stream interrupted)
    isInfo?: boolean;
    // この発言は言い切っていない (生成が中断された)。立っている間だけ
    // 「続きの生成」ボタンを出す。サーバーの metadata["_interrupted"] 由来。
    interrupted?: boolean;
    // この発言に返事が来なかった。立っている間だけ「再送」ボタンを出す。
    // 発言そのものは残っているので、押しても送り直しにはならない (応答だけ)。
    needsRetry?: boolean;
    // 取り消しが「もう読まれている」で断られた印。断られても応答はまだ
    // 生まれていないので needsRetry は残す — 消すと、応答を得る唯一の手段
    // (再送) まで一緒に失われる。消えるのは「取り消す」だけ。
    withdrawBlocked?: boolean;
    // やり直しても結果が変わらない印 (応答できる相手がいない等)。
    // needsRetry と混ぜない: 「返事が来ていない」と「やり直す余地がある」は
    // 別の事実で、混ぜると相手が居ない部屋で**誰も読んでいない発言の
    // 「取り消す」まで消える**。消えるのは「再送」だけ。
    retryUseless?: boolean;
    // Reasoning (thinking) from LLM
    reasoning?: string;
    // 自動想起 (記憶アーキv2 §4.5): この Pulse で末尾注入された「ふと浮かんだ記憶」ブロック。
    // <system>...</system> を剥がした本文を保持し、スペル結果と同じ折りたたみで表示する。
    auto_recall?: string;
    // Activity trace (exec/tool steps before final response)
    activity_trace?: ActivityEntry[];
    // Streaming state
    _streaming?: boolean;
    _streamingThinking?: string;
    _activities?: ActivityEntry[];
    // Pulse identifier — groups say + activity events from the same pulse together
    _pulse_id?: string;
}

interface ActivityEntry {
    action: 'exec' | 'tool' | 'memorize';
    name: string;
    playbook?: string;
    status?: string;
}

// File attachment types for upload
interface FileAttachment {
    id: string;             // unique key for React + async state replace
    name: string;
    type: 'image' | 'document' | 'audio' | 'video' | 'unknown';
    mimeType: string;
    // Inline base64 (image/audio/document). Empty for video — see below.
    base64?: string;
    // Video flow uploads via multipart to /api/media/upload-video to avoid
    // base64 ballooning browser memory, then references the saved file by
    // saiverse:// URI. previewUrl is a blob URL used for the optimistic in-
    // message preview (released by the browser on tab unload).
    uri?: string;           // saiverse://video/<filename>
    previewUrl?: string;    // blob URL for inline <video> preview
    uploading?: boolean;    // video multipart upload still in flight
    error?: string;         // upload failure message (chip turns red)
}

// File type detection
const TEXT_EXTENSIONS = new Set(['txt', 'md', 'py', 'js', 'ts', 'tsx', 'json', 'yaml', 'yml', 'csv',
    'html', 'css', 'xml', 'log', 'sh', 'bat', 'sql', 'java', 'c', 'cpp',
    'h', 'hpp', 'go', 'rs', 'rb', 'swift', 'kt', 'scala', 'r', 'lua', 'pl', 'pdf']);
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);
const AUDIO_EXTENSIONS = new Set(['wav', 'mp3', 'ogg', 'oga', 'opus', 'aac', 'flac', 'aiff', 'm4a']);
const VIDEO_EXTENSIONS = new Set(['mp4', 'webm', 'mov', 'avi', 'mpeg', 'mpg', '3gp', 'mkv', 'flv', 'wmv']);

function getFileType(filename: string, mimeType: string): 'image' | 'document' | 'audio' | 'video' | 'unknown' {
    const ext = filename.split('.').pop()?.toLowerCase() || '';
    if (IMAGE_EXTENSIONS.has(ext) || mimeType.startsWith('image/')) {
        return 'image';
    }
    if (AUDIO_EXTENSIONS.has(ext) || mimeType.startsWith('audio/')) {
        return 'audio';
    }
    if (VIDEO_EXTENSIONS.has(ext) || mimeType.startsWith('video/')) {
        return 'video';
    }
    if (TEXT_EXTENSIONS.has(ext) || mimeType.startsWith('text/')) {
        return 'document';
    }
    return 'unknown';
}

/**
 * キャッシュヒットの点。
 *
 * その発言を作った生成が「温まったコンテキスト」から生まれたときだけ、
 * メッセージの足元に小さい点を常時出す。ホバー (キーボードなら focus) で
 * トークンの三つ組 — 入力 / うちキャッシュ読み / 出力 — を実数で見せる。
 *
 * **冷えているときは何も出さない。** 冷えの警告を出しても受け手に打てる手が
 * なく、サーバー側の一時的な不調でも冷えるため (2026-08-19 まはー裁定)。
 * 見えるのは「効いている」だけ。
 *
 * 数字の出どころは既存の building message metadata (llm_usage /
 * llm_usage_total) で、新しい API は要らない。
 */
function CacheHitDot({ usage, total }: {
    usage?: MessageLLMUsage;
    total?: MessageLLMUsageTotal;
}) {
    // 複数コールの Pulse は合算を、単発は単発の実数を見せる (足元の使用量
    // チップと同じ選び方)。ライブの say イベントは合算しか運ばないので、
    // 単発が無いときも合算へ落とす。
    const useTotal = !!total && (!usage || total.call_count > 1);
    const cached = useTotal ? (total?.total_cached_tokens ?? 0) : (usage?.cached_tokens ?? 0);
    if (!cached) return null;
    const input = useTotal ? (total?.total_input_tokens ?? 0) : (usage?.input_tokens ?? 0);
    const output = useTotal ? (total?.total_output_tokens ?? 0) : (usage?.output_tokens ?? 0);
    const label = `キャッシュヒット: 入力 ${input.toLocaleString()} トークンのうち ${cached.toLocaleString()} をキャッシュから読み込み / 出力 ${output.toLocaleString()} トークン`;
    return (
        <span className={styles.cacheDotWrap} tabIndex={0} role="img" aria-label={label}>
            <span className={styles.cacheDot} />
            <div className={styles.cacheDotTooltip}>
                <div>入力 {input.toLocaleString()} tokens</div>
                <div>うちキャッシュ読み {cached.toLocaleString()} tokens</div>
                <div>出力 {output.toLocaleString()} tokens</div>
            </div>
        </span>
    );
}

export default function Home() {
    // Enable user presence tracking (heartbeat + visibility)
    useActivityTracker();

    // アクティブクライアントタブ (最後にユーザー操作があったタブ) 判定
    const { isActive: isActiveClientTab } = useActiveClientTab();

    // addon metadata lookup (client action executor に渡すため useClientActions から使われる)
    const getAddonMetadata = useCallback(
        (messageId: string | undefined, addonName: string) => {
            if (!messageId) return {};
            return addonMetadata[messageId]?.[addonName] ?? {};
        },
        // addonMetadata は下で useState 宣言されるため TDZ 回避用に closure 参照とする
        // eslint-disable-next-line react-hooks/exhaustive-deps
        [],
    );

    // client_actions (addon.json の ui_extensions.client_actions) ディスパッチャ
    const { dispatch: dispatchClientActions } = useClientActions({
        isActiveTab: isActiveClientTab,
        getAddonMetadata,
    });

    // アドオンSSEイベント購読：audio_ready など非同期完了イベントを受信してメタデータを更新
    useAddonEvents(useCallback((event) => {
        if (event.message_id && event.data) {
            setAddonMetadata((prev) => ({
                ...prev,
                [event.message_id!]: {
                    ...(prev[event.message_id!] ?? {}),
                    [event.addon]: {
                        ...(prev[event.message_id!]?.[event.addon] ?? {}),
                        ...event.data,
                    },
                },
            }));
        }
        // 同時に client_actions をディスパッチ (event.data 経由で URL 等を解決)
        dispatchClientActions(event);
    }, [dispatchClientActions]));

    const [messages, setMessages] = useState<Message[]>([]);
    const [inputValue, setInputValue] = useState('');
    const [loadingStatus, setLoadingStatus] = useState<string | null>(null);
    // metabolism 完了メッセージを 2 秒見せてから 'Thinking...' に戻す遅延タイマー。
    // ストリームが閉じる finally / cancelled でクリアしないと、後始末で
    // loadingStatus=null にした後にこのタイマーが発火してスピナーが復活し、
    // 二度と消えなくなる (記憶整理が応答の最後の処理だった場合に多発)。
    const metabolismStatusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const [permissionRequest, setPermissionRequest] = useState<PermissionRequestData | null>(null);
    const [spellConfirm, setSpellConfirm] = useState<SpellConfirmData | null>(null);
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
    // 取り消しの進行中。ボタンを disabled にするための state と、連打を**同期的に**
    // 弾くための ref を両方持つ。state だけだと、再描画が挟まる前の 2 発目が
    // まだ null を見て通り抜け、/withdraw が 2 回飛ぶ。
    const [withdrawingId, setWithdrawingId] = useState<string | null>(null);
    const withdrawingRef = useRef<string | null>(null);
    // アドオン: 有効なバブルボタン定義
    const [addonBubbleButtons, setAddonBubbleButtons] = useState<BubbleButtonDef[]>([]);
    // アドオン: メッセージごとのメタデータ { message_id: { addon_name: { key: value } } }
    const [addonMetadata, setAddonMetadata] = useState<Record<string, Record<string, Record<string, unknown>>>>({});

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

    // Stable components map for ReactMarkdown — reconstructing this on every render
    // remounts <img> tags and re-fetches their src on every keystroke.
    const markdownComponents = useMemo<Components>(() => ({
        a: ({ href, children }) => (
            <SaiverseLink href={href} onOpenItem={handleOpenItemFromLink}>{children as ReactNode}</SaiverseLink>
        ),
        img: ({ src, alt }) => {
            const resolved = typeof src === 'string' ? resolveSaiverseImageSrc(src) : src;
            return (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                    src={resolved as string}
                    alt={alt || ''}
                    className={styles.markdownImage}
                    onClick={() => typeof resolved === 'string' && window.open(resolved, '_blank')}
                />
            );
        },
    }), [handleOpenItemFromLink]);

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

    // アドオン一覧を取得してバブルボタン定義を構築する
    useEffect(() => {
        fetch('/api/addon/')
            .then((r) => r.ok ? r.json() : [])
            .then((addons: Array<{
                addon_name: string;
                is_enabled: boolean;
                ui_extensions?: {
                    bubble_buttons?: Array<{
                        id: string;
                        icon: string;
                        label: string;
                        action?: string;
                        tool?: string;
                        metadata_key?: string;
                        show_when?: string;
                    }>;
                };
            }>) => {
                const buttons: BubbleButtonDef[] = [];
                for (const addon of addons) {
                    if (!addon.is_enabled) continue;
                    for (const btn of addon.ui_extensions?.bubble_buttons ?? []) {
                        buttons.push({ ...btn, addon_name: addon.addon_name });
                    }
                }
                setAddonBubbleButtons(buttons);
            })
            .catch(() => {/* addon APIが無い環境では無視 */});
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
    const [selectedPlaybook, setSelectedPlaybook] = useState<string | null>(null);
    const [playbookArgs, setPlaybookArgs] = useState<Record<string, any>>({});
    const [selectedModel, setSelectedModel] = useState<string>(''); // Model ID selected in Chat Options
    const [selectedModelDisplayName, setSelectedModelDisplayName] = useState<string>(''); // Model display name
    const [selectedModelRateLimit, setSelectedModelRateLimit] = useState<{ rpd: number; reset_timezone: string } | null>(null); // Rate limit config for selected model
    const [rpdUsage, setRpdUsage] = useState<{ used: number; limit: number } | null>(null); // Current RPD usage
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
    // C-1 閲覧モード: currentBuildingId は 「UI 上で閲覧中の building」 になり、
    // 「サーバ上の真の現在地」 とは乖離しうる (= サイドバークリックで viewing を
    // 切り替えても、 サーバの CURRENT_BUILDINGID は発言時の /chat/utter まで
    // 変わらない)。 expected_from CAS 用に server-side の現在地を ref で保持する。
    const serverCurrentBuildingIdRef = useRef<string | null>(null);
    // ref の state ミラー (描画用: ゲームモード表示の出し分けに使う)。
    // 更新は必ず updateServerBuildingId() 経由で ref と同時に行うこと。
    const [serverBuildingId, setServerBuildingId] = useState<string | null>(null);
    const updateServerBuildingId = (bid: string | null) => {
        serverCurrentBuildingIdRef.current = bid;
        setServerBuildingId(bid);
    };
    // City Map: 街全体をシンボルマップで俯瞰するモーダル。展示用にデフォルト ON で起動。
    const [isMapModalOpen, setIsMapModalOpen] = useState<boolean>(true);

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

    // Region RPG: ゲームモード (セッションログビュー表示) の状態。
    // /api/user/status の active_game で判定し、ゲーム中はチャット表示を
    // Building 単位ログからセッションログ (Region 横断 merge) に切り替える。
    // 入力の投稿先は従来どおり現在 Building (配送・取り込みは不変)。
    type ActiveGame = {
        region_id: string;
        region_name?: string;
        phase: string;
        scene?: string;
        party_location?: string;
        // 実在地がゲーム Region 内か。false なら (入口・ゲーム外を問わず)
        // 通常チャット + セッションログの read-only 閲覧トグル + 「復帰」
        // ボタンを出す (docs/intent/region.md §7)
        inside?: boolean;
        // 実在地が入口 (控室) か。表示文言の補足にのみ使う
        at_entrance?: boolean;
    };
    const [activeGame, setActiveGame] = useState<ActiveGame | null>(null);
    const activeGameRef = useRef<ActiveGame | null>(null);
    // ゲーム外からセッションログを閲覧中か (トグル)。fetch 系 closure 用に ref を併用
    const [sessionLogPeek, setSessionLogPeek] = useState(false);
    const sessionLogPeekRef = useRef(false);
    const updateSessionLogPeek = (v: boolean) => {
        sessionLogPeekRef.current = v;
        setSessionLogPeek(v);
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

            // セッションログビューの条件:
            // - Region 内 (inside): 閲覧中の建物 = 自分の実在地 のときだけ。
            //   閲覧モードで他の建物を見ている間はその建物の通常ログ。
            // - Region 外 (入口含む): トグル ON のときだけ read-only で出す。
            //   閲覧場所は問わない (復帰前にどこからでも文脈を確認できる)
            const game = activeGameRef.current;
            const gameView = !!game && (game.inside
                ? !!bid && bid === serverCurrentBuildingIdRef.current
                : sessionLogPeekRef.current);
            let url: string;
            if (gameView) {
                url = `/api/world/regions/${encodeURIComponent(game.region_id)}/game/log?${params.toString()}`;
            } else {
                if (bid) params.append('building_id', bid);
                url = `/api/chat/history?${params.toString()}`;
            }

            console.log(`[DEBUG] Fetching history: before=${beforeId}, building_id=${bid}, gameView=${gameView ? game.region_id : 'none'}`);

            const res = await fetch(url);
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

                // アシスタントメッセージに紐付くアドオンメタデータを先読みする。
                // SSE の audio_ready イベントは発生時に1回だけ配信されるため、
                // ページリロード後に過去メッセージのバブルボタンを復元するには
                // ここで明示的にフェッチする必要がある。
                const assistantIds = newMessages
                    .filter(m => m.role === 'assistant' && m.id)
                    .map(m => m.id as string);
                if (assistantIds.length > 0) {
                    void Promise.all(
                        assistantIds.map(async (mid) => {
                            try {
                                const r = await fetch(
                                    `/api/addon/messages/${encodeURIComponent(mid)}/metadata`,
                                );
                                if (!r.ok) return;
                                const body = await r.json() as {
                                    metadata?: Record<string, Record<string, unknown>>;
                                };
                                const meta = body.metadata;
                                if (meta && Object.keys(meta).length > 0) {
                                    setAddonMetadata(prev => ({
                                        ...prev,
                                        [mid]: { ...(prev[mid] ?? {}), ...meta },
                                    }));
                                }
                            } catch {
                                // 個別失敗は無視(他メッセージは継続)
                            }
                        }),
                    );
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

    // Region RPG: active_game の状態反映 (表示切替の判断は呼び出し側が行う)
    const applyActiveGame = (game: ActiveGame | null) => {
        activeGameRef.current = game;
        setActiveGame(game);
    };

    // ゲーム外での「セッションログを見る ⇄ 通常チャットに戻る」トグル。
    // 表示ソースが切り替わるので履歴をロードし直す
    const toggleSessionLogPeek = () => {
        updateSessionLogPeek(!sessionLogPeekRef.current);
        setMessages([]);
        setIsHistoryLoaded(false);
        fetchHistory(undefined, currentBuildingIdRef.current ?? undefined);
    };

    // 「復帰」ボタン: パーティーの現在地へ移動してゲームに戻る。
    // サーバー側で再集結 + 自動再開が発火する (lifecycle.rejoin_party)。
    // LocationSync の次 tick を待たず即時に状態同期する
    const handleRejoinGame = async () => {
        const game = activeGameRef.current;
        if (!game) return;
        try {
            const res = await fetch(
                `/api/world/regions/${encodeURIComponent(game.region_id)}/game/rejoin`,
                { method: 'POST' },
            );
            if (!res.ok) {
                console.error('[Game] rejoin failed', res.status);
                return;
            }
            updateSessionLogPeek(false);
            const statusRes = await fetch('/api/user/status');
            if (statusRes.ok) {
                const data = await statusRes.json();
                const serverBid: string | null = data.current_building_id ?? null;
                applyActiveGame((data.active_game as ActiveGame | undefined) ?? null);
                if (serverBid) {
                    updateServerBuildingId(serverBid);
                    setCurrentBuildingId(serverBid);
                    currentBuildingIdRef.current = serverBid;
                    fetchBuildingInfo(serverBid);
                    setMoveTrigger(prev => prev + 1);
                }
            }
            setMessages([]);
            setIsHistoryLoaded(false);
            fetchHistory(undefined, currentBuildingIdRef.current ?? undefined);
        } catch (e) {
            console.error('[Game] rejoin error', e);
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
                            audios: entry.msg.audios || local.audios,
                            videos: entry.msg.videos || local.videos,
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

            // Force latestMessageIdRef to the newest server-known message ID.
            // This prevents polling from using a stale ref and re-adding already-seen messages.
            const newestServerId = serverMessages[serverMessages.length - 1]?.id;
            if (newestServerId) {
                latestMessageIdRef.current = newestServerId;
                console.log(`[syncAfterResponse] latestMessageIdRef forced to ${newestServerId}`);
            }
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
            if (!bid) {
                // 起動直後の /api/user/status 完了前に呼ばれた場合は何もしない。
                // 引数なしで叩くと server-global の user_current_building_id に
                // 汚染されうる (エリス上書き事故の遠因)。
                return;
            }
            const res = await fetch(`/api/info/details?building_id=${encodeURIComponent(bid)}`);
            if (res.ok) {
                const data = await res.json();
                setCurrentBuildingName(data.name || 'SAIVerse');
            }
        } catch (err) {
            console.error('Failed to fetch building info', err);
        }
    };

    // CityMap で家アイコンをクリックされたとき: 閲覧モードで建物を切り替える。
    // C-1 (intent §C): サーバ側の CURRENT_BUILDINGID は据え置きのまま、 UI 上の
    // 表示建物だけ切り替える (= 閲覧)。 実際の入室は発言時に /chat/utter が
    // atomic に行う。
    const handleSelectBuildingFromMap = (buildingId: string) => {
        if (!buildingId) return;
        if (currentBuildingId === buildingId) {
            // 既に表示中の Building ならモーダルを閉じるだけ
            setIsMapModalOpen(false);
            return;
        }
        setCurrentBuildingId(buildingId);
        currentBuildingIdRef.current = buildingId;
        // 建物を選んだ = その建物のログを見たい。セッションログ閲覧は解除
        updateSessionLogPeek(false);
        setMessages([]);
        setIsHistoryLoaded(false);
        fetchHistory(undefined, buildingId);
        fetchBuildingInfo(buildingId);
        setMoveTrigger(prev => prev + 1);
        setIsMapModalOpen(false);
    };

    // Esc キーでマップモーダルを閉じる
    useEffect(() => {
        if (!isMapModalOpen) return;
        const handler = (e: globalThis.KeyboardEvent) => {
            if (e.key === 'Escape') setIsMapModalOpen(false);
        };
        window.addEventListener('keydown', handler);
        return () => window.removeEventListener('keydown', handler);
    }, [isMapModalOpen]);

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
                    updateServerBuildingId(data.current_building_id);
                }
                if (data?.display_name) userDisplayNameRef.current = data.display_name;
                if (data?.avatar) userAvatarRef.current = data.avatar;
                applyActiveGame(data?.active_game ?? null);
                // 起動直後の fetchHistory() は status 取得とレースするため、
                // ゲーム中 (Region 内) なら refs 確定後にセッションログで取り直す
                if (data?.active_game?.inside && data?.current_building_id) {
                    setMessages([]);
                    setIsHistoryLoaded(false);
                    fetchHistory(undefined, data.current_building_id);
                }
            })
            .catch(() => setBackendConnected(false));
        fetchHistory();
        fetchBuildingInfo();
        // Fetch saved playbook setting and params from server.
        // Legacy values from the pre-Phase 3 era (meta_user / meta_user_manual /
        // meta_simple_speak, and the old track_user_conversation explicit
        // selection) are collapsed to "auto" because the new 2-mode UI only
        // recognises null and the TOOL_MODE_SELECTED sentinel.
        fetch('/api/config/playbook')
            .then(res => res.ok ? res.json() : null)
            .then(data => {
                if (data) {
                    if (data.playbook === TOOL_MODE_SELECTED) {
                        setSelectedPlaybook(TOOL_MODE_SELECTED);
                    } else {
                        setSelectedPlaybook(null);
                    }
                    if (data.args && Object.keys(data.args).length > 0) {
                        setPlaybookArgs(data.args);
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
                const modelInfo = models.find((m: { id: string; name: string; rate_limit?: { rpd: number; reset_timezone: string } | null }) => m.id === modelId);
                setSelectedModel(modelId);
                setSelectedModelDisplayName(modelInfo?.name || '');
                setSelectedModelRateLimit(modelInfo?.rate_limit || null);
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

        // Check for updates (respects update-check toggle)
        fetch('/api/config/update-check')
            .then(res => res.ok ? res.json() : null)
            .then(cfg => {
                if (cfg && !cfg.enabled) return;  // Skip if disabled
                return fetch('/api/system/version')
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
                    });
            })
            .catch(() => { /* ignore - backend may not support this endpoint yet */ });

        // Check for unread announcements (and poll every 30 minutes)
        const checkAnnouncements = () => {
            // Check if announcements monitoring is enabled before fetching
            fetch('/api/config/announcements-monitor')
                .then(res => res.ok ? res.json() : null)
                .then(cfg => {
                    if (cfg && !cfg.enabled) return;  // Skip if disabled
                    return fetch('/api/system/announcements')
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
                        });
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

    // RPD usage polling - fetch when model has rate_limit, poll every 60 seconds
    useEffect(() => {
        if (!selectedModel || !selectedModelRateLimit) {
            setRpdUsage(null);
            return;
        }

        const fetchRpd = () => {
            fetch(`/api/usage/rpd?model_id=${encodeURIComponent(selectedModel)}`)
                .then(res => res.ok ? res.json() : null)
                .then((data: { model_id: string; used: number; limit: number }[] | null) => {
                    if (data && data.length > 0) {
                        setRpdUsage({ used: data[0].used, limit: data[0].limit });
                    } else {
                        setRpdUsage(null);
                    }
                })
                .catch(() => setRpdUsage(null));
        };

        fetchRpd();
        const interval = setInterval(fetchRpd, 60_000);
        return () => clearInterval(interval);
    }, [selectedModel, selectedModelRateLimit]);

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
                const game = activeGameRef.current;
                const gameView = !!game && (game.inside
                    ? !!pollBid && pollBid === serverCurrentBuildingIdRef.current
                    : sessionLogPeekRef.current);
                const pollUrl = gameView
                    ? `/api/world/regions/${encodeURIComponent(game.region_id)}/game/log?after=${encodeURIComponent(newestId)}&limit=50`
                    : `/api/chat/history?after=${newestId}&limit=50${bidParam}`;
                const res = await fetch(pollUrl);
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
                        setMoveTrigger(prev => prev + 1);
                    }
                }
            } catch (err) {
                console.error("[Polling] Failed to check for new messages", err);
            }
        }, 5000); // Poll every 5 seconds

        return () => clearInterval(pollInterval);
    }, [isHistoryLoaded]);

    // Server-driven user movement sync (game_move_party / end_game 控室帰還など):
    // サーバー側の実在地 (serverCurrentBuildingIdRef との差分) が変わった時だけ
    // 画面を追従させる。閲覧モード (currentBuildingIdRef ≠ 実在地) で他の建物を
    // 見ている間は、サーバー位置が動かない限り何もしない。
    useEffect(() => {
        if (!isHistoryLoaded) return;

        const syncInterval = setInterval(async () => {
            if (isProcessingRef.current) return; // ストリーミング中は画面を奪わない
            try {
                const res = await fetch('/api/user/status');
                if (!res.ok) return;
                const data = await res.json();
                const serverBid: string | null = data.current_building_id ?? null;
                const oldServerBid = serverCurrentBuildingIdRef.current;
                const game = (data.active_game as ActiveGame | undefined) ?? null;
                const prevGame = activeGameRef.current;

                const serverMoved = !!serverBid && !!oldServerBid && serverBid !== oldServerBid;
                // 「いまセッションログを表示しているか」:
                // Region 内 = 閲覧先 = 実在地のとき / Region 外 = トグル ON のとき
                const wasShowingLog = !!prevGame && (prevGame.inside
                    ? !!oldServerBid && currentBuildingIdRef.current === oldServerBid
                    : sessionLogPeekRef.current);

                if (serverBid) updateServerBuildingId(serverBid);
                applyActiveGame(game);

                // ログ閲覧トグルは「ゲーム外に居る」状態にのみ意味がある。
                // ゲーム終了 / Region 内への移動でその状態が消えたらリセットする
                if ((!game || game.inside) && sessionLogPeekRef.current) {
                    updateSessionLogPeek(false);
                }

                if (serverMoved) {
                    // サーバー発の移動 (= ゲームの物語が動いた)。画面を追従させる
                    console.log(`[LocationSync] Server moved user: ${oldServerBid} -> ${serverBid}`);
                    setCurrentBuildingId(serverBid);
                    currentBuildingIdRef.current = serverBid;
                    fetchBuildingInfo(serverBid);
                    setMoveTrigger(prev => prev + 1);
                }

                const nowShowingLog = !!game && (game.inside
                    ? currentBuildingIdRef.current === serverCurrentBuildingIdRef.current
                    : sessionLogPeekRef.current);
                // セッションログ → 同一セッションのログ なら表示は連続している
                const logContinues = wasShowingLog && nowShowingLog
                    && prevGame!.region_id === game!.region_id;
                const viewSourceChanged = serverMoved || (wasShowingLog !== nowShowingLog);
                if (viewSourceChanged && !logContinues) {
                    setMessages([]);
                    setIsHistoryLoaded(false);
                    fetchHistory(undefined, currentBuildingIdRef.current ?? undefined);
                }
            } catch {
                // backend 不達は再接続ポーリング側が面倒を見る
            }
        }, 5000);

        return () => clearInterval(syncInterval);
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

    const handleSpellConfirmResponse = useCallback(async (requestId: string, decision: string, editedText?: string) => {
        setSpellConfirm(null);
        try {
            await fetch('/api/chat/spell-confirmation-response', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ request_id: requestId, decision, edited_text: editedText }),
            });
        } catch (e) {
            console.error('Failed to send spell confirmation response', e);
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

    // ------------------------------------------------------------------
    // 応答ストリームの読み手
    //
    // 送信・続きの生成・やり直しの三つの入口が同じ読み手を共有する。入口ごとに
    // 書き分けると、片方だけ直った分岐がいずれ必ず生まれる。
    // 設計: docs/issues/user_utterance_path_failure_inventory.md
    // ------------------------------------------------------------------

    // 画面に出る案内 (エラー・お知らせ) は、いまの状態の表示であって履歴では
    // ない。サーバーには保存されず、別の建物を見て戻ったりページを開き直すと
    // 消える — その揮発性に合わせて、**次の操作を始めたら消す** (2026-08-26
    // まはー裁定)。時間では消さない: 読んでいない可能性が普通にあるので、
    // 消える引き金はユーザーの操作だけにする。
    //
    // isError / isWarning / isInfo が立つのは画面側で作った案内だけで、履歴から
    // 復元した発言には付かない (履歴はサーバーの行をそのまま並べる)。だから
    // この条件で本物の発言を巻き込むことはない。印を持たない system の
    // お知らせ行はサーバー由来なので残す。
    const clearTransientNotices = () => {
        setMessages(prev => prev.filter(
            m => !m.isError && !m.isWarning && !m.isInfo,
        ));
    };

    // やり直しても結果が変わらない終わり方。ここに載る札のときは「再送」を
    // 出さない — 押しても同じ結果しか返らない操作を勧めることになるから。
    const RETRY_CHANGES_NOTHING = new Set(['no_responder']);

    // 返事が来なかった発言に印を立てる。出口 3 / 7。
    //
    // ``useless`` は「やり直しても結果が変わらない」— 相手が居ない部屋など。
    // その回も **needsRetry は立てる**: 返事が来ていないのは事実だし、誰にも
    // 読まれていない以上「取り消す」は使えるべきだから。落とすのは「再送」だけ。
    const markRetryable = (messageId: string, useless = false) => {
        setMessages(prev => prev.map(m => (
            m.id === messageId && m.role === 'user'
                ? { ...m, needsRetry: true, retryUseless: useless }
                : m
        )));
    };

    // ストリームが閉じた後の後片付け。読み終わっても、途中で切れても、fetch が
    // 失敗しても必ず通す。
    const finishReplyCycle = async () => {
            // ストリームが閉じた後に metabolism の遅延タイマーが発火すると
            // スピナーが復活して二度と消えなくなるため、必ずここで潰す。
            if (metabolismStatusTimerRef.current) {
                clearTimeout(metabolismStatusTimerRef.current);
                metabolismStatusTimerRef.current = null;
            }
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
            setMoveTrigger(prev => prev + 1);
            // Refresh RPD usage after message sent
            if (selectedModelRateLimit && selectedModel) {
                fetch(`/api/usage/rpd?model_id=${encodeURIComponent(selectedModel)}`)
                    .then(res => res.ok ? res.json() : null)
                    .then((data: { used: number; limit: number }[] | null) => {
                        if (data && data.length > 0) setRpdUsage({ used: data[0].used, limit: data[0].limit });
                    })
                    .catch(() => {});
            }
    };

    const consumeReplyStream = async (
        res: Response,
        source: 'send' | 'continue' | 'retry',
    ) => {
        // 発言が届いたことをサーバーから聞けたか。通信が途中で切れたとき、
        // 「届いたか分からない」(出口 7) と「届いたが返事が無かった」(出口 3) を
        // 分けるのに使う。分からないときに分かった顔をしないための材料。
        let landedMessageId: string | null = null;
        // ペルソナが実際に言葉を出したか。呼び出し元が「走らせた」と「返事が
        // 生まれた」を取り違えないための報告。バックエンドの続き生成も同じ線で
        // 印を降ろすかを決めており、こちらだけ別の線 (ストリームが始まったか)
        // を引くと、片方だけ正しい状態になる。
        let replied = false;
        // 最後に届いたエラーの札。「やり直しても変わらない」かの判定に使う。
        let lastErrorCode: string | null = null;
        // 読めなかった行の数。1 行でも読めなければ、そこに何が載っていたかは
        // 分からない — 発言が届いた印かもしれないし、エラーの説明かもしれない。
        // 黙って捨てると、何も起きなかったのと同じ顔でストリームが正常終了する。
        let malformedLines = 0;
        try {
            if (!res.body) throw new Error("No response body");
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();

                let lines: string[];
                if (done) {
                    // 最後のチャンクが改行で終わっていない = 行が途中で切れている。
                    // decoder を flush して残りを取り出し、1 行として通す。捨てると、
                    // 最後のイベント (結果を運んでいることが多い) が消える。
                    buffer += decoder.decode();
                    lines = buffer ? [buffer] : [];
                    buffer = '';
                } else {
                    buffer += decoder.decode(value, { stream: true });
                    const parts = buffer.split('\n');
                    buffer = parts.pop() || ''; // Keep the last partial line
                    lines = parts;
                }

                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const event = JSON.parse(line);
                        if (event.type !== 'ping') {
                            console.log('[SSE][diag]', event.type, 'persona=', event.persona_id, 'pulse=', event.pulse_id);
                        }

                        if (event.type === 'status') {
                            setLoadingStatus(event.content === 'processing' ? 'Processing...' : event.content);
                        } else if (event.type === 'think') {
                            setLoadingStatus(`Thinking: ${event.content.substring(0, 50)}${event.content.length > 50 ? '...' : ''}`);
                        } else if (event.type === 'activity') {
                            // Activity trace: accumulate tool/memorize steps
                            const entry: ActivityEntry = { action: event.action, name: event.name, ...(event.playbook && { playbook: event.playbook }), status: event.status };
                            const evtPulseId: string | undefined = event.pulse_id || undefined;
                            const mergeActivity = (existing: ActivityEntry[] | undefined): ActivityEntry[] => {
                                const activities = [...(existing || [])];
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
                                return activities;
                            };
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        _activities: mergeActivity(last._activities),
                                        ...(evtPulseId && !last._pulse_id && { _pulse_id: evtPulseId }),
                                    }];
                                }
                                // Finalized message with the same pulse_id: append to its activity_trace
                                if (evtPulseId) {
                                    for (let i = prev.length - 1; i >= 0; i--) {
                                        const m = prev[i];
                                        if (m.role !== 'assistant') continue;
                                        if (m._pulse_id !== evtPulseId) continue;
                                        const merged = mergeActivity(m.activity_trace || m._activities);
                                        const updated = [...prev];
                                        updated[i] = { ...m, activity_trace: merged, _activities: undefined };
                                        return updated;
                                    }
                                }
                                const actAvatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
                                return [...prev, {
                                    role: 'assistant' as const, content: '', _streaming: true,
                                    sender: event.persona_name || undefined,
                                    avatar: actAvatarUrl,
                                    _activities: [entry], timestamp: new Date().toISOString(),
                                    ...(evtPulseId && { _pulse_id: evtPulseId }),
                                }];
                            });
                            setLoadingStatus(event.status === 'started' ? `Running ${event.name}...` : event.name);
                        } else if (event.type === 'auto_recall') {
                            // 自動想起 (記憶アーキv2 §4.5): 末尾注入された「ふと浮かんだ記憶」を
                            // スペル結果と同じ折りたたみで表示する。<system> タグは剥がす。
                            const rawContent: string = typeof event.content === 'string' ? event.content : '';
                            const recallBody = rawContent
                                .replace(/^\s*<system>/, '')
                                .replace(/<\/system>\s*$/, '')
                                .trim();
                            if (recallBody) {
                                const arAvatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
                                setMessages(prev => {
                                    const last = prev[prev.length - 1];
                                    if (last && last.role === 'assistant' && last._streaming) {
                                        return [...prev.slice(0, -1), { ...last, auto_recall: recallBody }];
                                    }
                                    return [...prev, {
                                        role: 'assistant' as const, content: '', _streaming: true,
                                        sender: event.persona_name || undefined,
                                        avatar: arAvatarUrl,
                                        auto_recall: recallBody, timestamp: new Date().toISOString(),
                                    }];
                                });
                            }
                        } else if (event.type === 'streaming_thinking') {
                            // Streaming thinking: accumulate into _streamingThinking
                            const avatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
                            const evtPulseId: string | undefined = event.pulse_id || undefined;
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        _streamingThinking: (last._streamingThinking || '') + event.content,
                                        ...(evtPulseId && !last._pulse_id && { _pulse_id: evtPulseId }),
                                    }];
                                } else {
                                    return [...prev, {
                                        role: 'assistant',
                                        content: '',
                                        sender: event.persona_name || 'Assistant',
                                        avatar: avatarUrl,
                                        timestamp: new Date().toISOString(),
                                        _streaming: true,
                                        _streamingThinking: event.content,
                                        ...(evtPulseId && { _pulse_id: evtPulseId }),
                                    }];
                                }
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'streaming_chunk') {
                            if (String(event.content || '').trim()) replied = true;
                            // Streaming: append chunk to last message or create new one
                            const avatarUrl = event.persona_avatar || (event.persona_id ? `/api/chat/persona/${event.persona_id}/avatar` : undefined);
                            const evtPulseId: string | undefined = event.pulse_id || undefined;
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last.role === 'assistant' && last._streaming) {
                                    return [...prev.slice(0, -1), {
                                        ...last,
                                        content: last.content + event.content,
                                        ...(evtPulseId && !last._pulse_id && { _pulse_id: evtPulseId }),
                                    }];
                                } else {
                                    return [...prev, {
                                        role: 'assistant',
                                        content: event.content,
                                        sender: event.persona_name || 'Assistant',
                                        avatar: avatarUrl,
                                        timestamp: new Date().toISOString(),
                                        _streaming: true,
                                        ...(evtPulseId && { _pulse_id: evtPulseId })
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
                                        // 途中で切れた発言。再読込を待たずに印を立て、
                                        // その場で「続きの生成」を出せるようにする。
                                        ...(event.interrupted && { interrupted: true }),
                                    }];
                                }
                                return prev;
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'say') {
                            if (String(event.content || '').trim()) replied = true;
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
                            const sayPulseId: string | undefined = event.pulse_id || undefined;
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
                                        ...(sayPulseId && { _pulse_id: sayPulseId }),
                                    }];
                                }
                                return [...prev, {
                                    role: 'assistant',
                                    id: event.message_id || undefined,
                                    content: event.content,
                                    sender: event.persona_name || 'Assistant',
                                    avatar: avatarUrl,
                                    timestamp: new Date().toISOString(),
                                    ...(sayImages && { images: sayImages }),
                                    ...(sayUsageTotal && { llm_usage_total: sayUsageTotal }),
                                    ...(sayReasoning && { reasoning: sayReasoning }),
                                    ...(sayActivityTrace && { activity_trace: sayActivityTrace }),
                                    ...(sayPulseId && { _pulse_id: sayPulseId }),
                                }];
                            });
                            setLoadingStatus('Thinking...');
                        } else if (event.type === 'error') {
                            // W7: 位置競合エラー (not_in_building 等) はサーバの
                            // 確定現在地を同期する — 放置すると次回発言の
                            // expected_from が古いままで余分な CAS 競合になる
                            if (event.current_building_id) {
                                updateServerBuildingId(event.current_building_id);
                            }
                            setMessages(prev => [...prev, {
                                role: 'assistant',
                                content: event.content || 'An error occurred',
                                isError: true,
                                errorCode: event.error_code || 'unknown',
                                errorDetail: event.technical_detail,
                                timestamp: new Date().toISOString()
                            }]);
                            const errorCode: string = event.error_code || 'unknown';
                            lastErrorCode = errorCode;
                            // 発言は届いているのに返事が生まれなかった (出口 3)。
                            // 送り直しではなく「もう一度応答を得る」を出す。
                            // 応答できる相手が居ない回は「再送」だけを落とす —
                            // 発言は誰にも読まれていないので「取り消す」は残る。
                            if (landedMessageId) {
                                markRetryable(
                                    landedMessageId,
                                    RETRY_CHANGES_NOTHING.has(errorCode),
                                );
                            }
                        } else if (event.type === 'metabolism') {
                            if (event.status === 'completed') {
                                // Show completion message briefly, then transition
                                if (event.content) {
                                    setLoadingStatus(event.content);
                                    if (metabolismStatusTimerRef.current) clearTimeout(metabolismStatusTimerRef.current);
                                    metabolismStatusTimerRef.current = setTimeout(() => {
                                        metabolismStatusTimerRef.current = null;
                                        setLoadingStatus('Thinking...');
                                    }, 2000);
                                } else {
                                    setLoadingStatus('Thinking...');
                                }
                            } else {
                                // started, running, etc. — show content as loading status
                                setLoadingStatus(event.content || '記憶を整理しています...');
                            }
                        } else if (event.type === 'user_message_id') {
                            // Update the optimistic user message (temp id) with server-assigned id
                            if (event.message_id) {
                                // サーバーが発言を受け取った証拠。通信が後で切れても
                                // 「届いたかどうか分からない」ではなくなる。
                                landedMessageId = event.message_id;
                                setMessages(prev => {
                                    // Find the last user message with a temp id and update it
                                    for (let i = prev.length - 1; i >= 0; i--) {
                                        if (prev[i].role === 'user' && prev[i].id?.startsWith('temp-')) {
                                            const updated = [...prev];
                                            updated[i] = { ...prev[i], id: event.message_id };
                                            return updated;
                                        }
                                    }
                                    return prev;
                                });
                            }
                        } else if (event.type === 'permission_request') {
                            setPermissionRequest({
                                requestId: event.request_id,
                                playbookName: event.playbook_name,
                                playbookDisplayName: event.playbook_display_name || event.playbook_name,
                                playbookDescription: event.playbook_description || '',
                                personaName: event.persona_name || '',
                            });
                        } else if (event.type === 'spell_confirmation') {
                            setSpellConfirm({
                                requestId: event.request_id,
                                title: event.title || '確認',
                                body: event.body || '',
                                editable: !!event.editable,
                                text: event.text,
                                addon: event.addon || '',
                                confirmText: event.confirm_text,
                                maxChars: event.max_chars,
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
                        } else if (event.type === 'info') {
                            // Info notification (e.g. 504 stream interruption)
                            setMessages(prev => [...prev, {
                                role: 'system',
                                content: event.content || '',
                                isInfo: true,
                                timestamp: new Date().toISOString()
                            }]);
                        } else if (event.type === 'cancelled') {
                            // Server-side cancellation: finalize streaming message
                            setMessages(prev => {
                                const last = prev[prev.length - 1];
                                if (last && last._streaming) {
                                    // 本文が一文字も出る前に停止された回。思考や活動の
                                    // 表示だけのバブルは、サーバー履歴には存在しない
                                    // (下書き行は空文字で確定され、履歴 API は content 空を
                                    // 出さない) ので、再読込で黙って消える。見かけと実態を
                                    // 揃えるため、その場で畳む (2026-08-27 まはー裁定)。
                                    // 本文が一文字でも出ていれば従来どおり確定して残す。
                                    if (!last.content) {
                                        return prev.slice(0, -1);
                                    }
                                    const { _streaming, _streamingThinking, _activities, ...rest } = last;
                                    return [...prev.slice(0, -1), {
                                        ...rest,
                                        ...((_streamingThinking) && { reasoning: _streamingThinking }),
                                        ...((_activities && _activities.length > 0) && { activity_trace: _activities }),
                                    }];
                                }
                                return prev;
                            });
                            if (metabolismStatusTimerRef.current) {
                                clearTimeout(metabolismStatusTimerRef.current);
                                metabolismStatusTimerRef.current = null;
                            }
                            setLoadingStatus(null);
                        } else if (event.response) {
                            setMessages(prev => [...prev, { role: 'assistant', content: event.response }]);
                        }

                    } catch (e) {
                        malformedLines += 1;
                        console.error("Error parsing NDJSON line", e, line);
                    }
                }

                if (done) break;
            }

            if (malformedLines > 0) {
                // 下の catch へ落とす。そこには既に「発言が届いた印を持っているか」で
                // 復旧導線を分ける判断があるので、同じ道を通す。
                throw new Error(
                    `${malformedLines} NDJSON line(s) could not be parsed`,
                );
            }
        } catch (error) {
            console.error(error);
            if (source === 'send' && !landedMessageId) {
                // 出口 7: サーバーに届いたかどうか、こちら側からは原理的に
                // 分からない。分かった顔をせず、そのまま伝える。
                setMessages(prev => [...prev, {
                    role: 'system',
                    content: '通信が途中で切れました。発言が届いたかどうかは分かりません。履歴を確認してください。',
                    isError: true,
                    errorCode: 'unknown_outcome',
                    timestamp: new Date().toISOString(),
                }]);
            } else {
                setMessages(prev => [...prev, {
                    role: 'system',
                    content: source === 'continue'
                        ? '続きの生成が途中で切れました。'
                        : '通信が途中で切れました。',
                    isError: true,
                    errorCode: 'stream_broken',
                    timestamp: new Date().toISOString(),
                }]);
                if (landedMessageId) markRetryable(landedMessageId);
            }
        } finally {
            await finishReplyCycle();
        }
        return { replied, errorCode: lastErrorCode };
    };

    const handleSendMessage = async () => {
        if ((!inputValue.trim() && attachments.length === 0) || loadingStatus) return;
        clearTransientNotices();
        // Block send while a video is still uploading, or if any attachment errored.
        const pendingUpload = attachments.find(a => a.uploading);
        if (pendingUpload) {
            alert(`動画「${pendingUpload.name}」のアップロード中です。完了まで少し待って。`);
            return;
        }
        const errored = attachments.find(a => a.error);
        if (errored) {
            alert(`添付「${errored.name}」のアップロードに失敗してる: ${errored.error}\n削除してから送って。`);
            return;
        }
        isProcessingRef.current = true;

        // Optimistic update
        // Temporary ID for key prop until refreshed
        const tempId = `temp-${Date.now()}`;
        const userMsg: Message = {
            id: tempId, role: 'user', content: inputValue,
            sender: userDisplayNameRef.current || undefined,
            avatar: userAvatarRef.current || undefined,
            images: attachments
                .filter(a => a.type === 'image' && a.base64)
                .map(a => ({ url: `data:${a.mimeType};base64,${a.base64}`, mime_type: a.mimeType })),
            audios: attachments
                .filter(a => a.type === 'audio' && a.base64)
                .map(a => ({ url: `data:${a.mimeType};base64,${a.base64}`, mime_type: a.mimeType })),
            // Video preview uses the blob URL (no base64 copy); it's only valid for
            // this session, but the next history fetch replaces it with a real
            // /api/media/video/<name> URL from the server.
            videos: attachments
                .filter(a => a.type === 'video' && a.previewUrl)
                .map(a => ({ url: a.previewUrl as string, mime_type: a.mimeType })),
        };
        setMessages(prev => [...prev, userMsg]);
        setInputValue('');
        setLoadingStatus('Thinking...');

        const currentAttachments = attachments;
        const currentPlaybook = selectedPlaybook;
        const currentPlaybookArgs = playbookArgs;

        setAttachments([]);
        // Reset playbook args after sending
        setPlaybookArgs({});

        // ツール指定モードの場合は UI 状態 (Playbook 1 つ + Spell 複数) を
        // pre_spells エントリ列に変換して送る。meta_playbook と args は送らない
        // (UI センチネルはサーバー側に存在しない Playbook 名なので、そのまま渡すと
        // "playbook not found" になる)。pre_spells のフォーマットは
        // バックエンドの _SPELL_PATTERN / _SPELL_PATTERN_NO_ARGS と互換
        // (sea/runtime_llm.py)。
        const isToolSelectedMode = currentPlaybook === TOOL_MODE_SELECTED;
        const selectedToolName = isToolSelectedMode
            ? (currentPlaybookArgs?.selected_playbook || null)
            : null;
        const selectedSpellNames: string[] = isToolSelectedMode && Array.isArray(currentPlaybookArgs?.selected_spells)
            ? (currentPlaybookArgs.selected_spells as string[])
            : [];
        const preSpellsBuilt = isToolSelectedMode
            ? buildPreSpellsFromUI(selectedToolName, selectedSpellNames)
            : [];
        const preSpells = preSpellsBuilt.length > 0 ? preSpellsBuilt : undefined;
        const sendMetaPlaybook = isToolSelectedMode ? undefined : (currentPlaybook || undefined);
        const sendArgs = !isToolSelectedMode && Object.keys(currentPlaybookArgs).length > 0
            ? currentPlaybookArgs
            : undefined;

        // B-2 idempotency: 送信操作 1 回につき 1 つの UUID を生成。
        // fetch retry 等で同じ送信が複数回 backend に届いても、 backend は
        // UNIQUE(client_message_id) で既存行を返すだけで二重 INSERT しない。
        // See: docs/intent/building_memory_unified.md §B-2
        //
        // 注: crypto.randomUUID() は Secure Context 限定なので、 Tailscale
        // 経由スマホ等 (http://100.x.x.x:3000) では throw する。 そのケース
        // では非 secure context でも使える getRandomValues で UUID v4 を
        // 手組みするフォールバックに落ちる。
        const clientMessageId = (() => {
            if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
                try { return crypto.randomUUID(); } catch { /* fall through */ }
            }
            const bytes = new Uint8Array(16);
            crypto.getRandomValues(bytes);
            bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
            bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant
            const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
            return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
        })();

        try {
            // C-2: /chat/utter は発言契機入室。 target_building_id (= UI 上で
            // 表示中の建物) がサーバの真の現在地と異なれば、 backend が atomic
            // に move を実行してから発言処理に入る。 同建物発言なら move skip。
            const res = await fetch('/api/chat/utter', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: userMsg.content,
                    target_building_id: currentBuildingIdRef.current,
                    // B-1 CAS: クライアントが認識しているサーバの真の現在地。
                    // サーバ側と一致しなければ 409 で他クライアントの先行 move
                    // を検出できる。
                    expected_from_building_id: serverCurrentBuildingIdRef.current,
                    attachments: currentAttachments.length > 0 ? currentAttachments.map(a => (
                        a.type === 'video' && a.uri
                            ? { uri: a.uri, filename: a.name, type: a.type, mime_type: a.mimeType }
                            : { data: a.base64, filename: a.name, type: a.type, mime_type: a.mimeType }
                    )) : undefined,
                    meta_playbook: sendMetaPlaybook,
                    args: sendArgs,
                    pre_spells: preSpells,
                    client_message_id: clientMessageId,
                })
            });

            if (res.status === 409) {
                // CAS conflict (= B-1): 他クライアントが先に動いていた。
                // ユーザーに通知し、 status を再取得して serverCurrentBuildingId
                // を真の現在地に同期する。 メッセージ自体は再送が必要。
                let conflictMsg = '他のクライアントが先に移動したため、 発言は受け付けられませんでした。 最新状態に同期します。';
                try {
                    const data = await res.json();
                    if (data?.detail?.message) conflictMsg = data.detail.message;
                    if (data?.detail?.current_building_id) {
                        updateServerBuildingId(data.detail.current_building_id);
                    }
                } catch { /* ignore JSON parse */ }
                // この発言はサーバーに保存されていない。alert だけで済ませると、
                // 閉じた瞬間に失敗の説明が消え、入力欄も空のままなので、本文を
                // 手で打ち直すしか手が残らない。③ の「取り消す」と同じ形で
                // 手元へ返す — 吹き出しを引っ込めて、本文を入力欄に戻す。
                setMessages(prev => prev.filter(m => m.id !== tempId));
                setInputValue(prev => (
                    prev.trim() ? `${prev}\n${userMsg.content}` : userMsg.content
                ));
                // 添付も一緒に戻す。本文だけ返しても、画像や音声や動画は消えた
                // ままなので「同じものをもう一度送る」ができない。動画はサーバー側に
                // 再送用のファイルが残るので、URI を手放すと参照できない置き土産に
                // なる。
                // 送っている間にも添付は足せる (ボタンは閉じていない)。丸ごと
                // 置き換えると、その間に足したものとアップロード中の状態まで消える。
                // 手が入っていないときだけ戻す。
                if (currentAttachments.length > 0) {
                    setAttachments(prev => (prev.length > 0 ? prev : currentAttachments));
                }
                requestAnimationFrame(() => adjustTextareaHeight());
                setMessages(prev => [...prev, {
                    role: 'system',
                    content: conflictMsg,
                    isError: true,
                    errorCode: 'location_conflict',
                    timestamp: new Date().toISOString(),
                }]);
                // status を再取得して UI と整合させる
                try {
                    const statusRes = await fetch('/api/user/status');
                    if (statusRes.ok) {
                        const statusData = await statusRes.json();
                        if (statusData?.current_building_id) {
                            updateServerBuildingId(statusData.current_building_id);
                        }
                    }
                } catch (statusErr) {
                    console.error('Failed to refetch status after CAS conflict', statusErr);
                }
                // 後片付けは必ず通す。読み手を切り出したことで、この早期 return は
                // もう外側の finally に拾われない (isProcessingRef が立ったままだと
                // 履歴の追従が止まる)。
                await finishReplyCycle();
                return;
            }

            if (!res.ok) {
                let errorDetails = `Status: ${res.status} ${res.statusText}`;
                try {
                    const errorText = await res.text();
                    errorDetails += ` - Body: ${errorText}`;
                } catch (e) { console.error('Failed to read error response body:', e); }
                throw new Error(`Failed to send message. ${errorDetails}`);
            }

            // 楽観的更新: utter が成功した時点で、 サーバ側の真の現在地は
            // target_building_id に移動済 (= utter が atomic に auto-move
            // した)。 次回発言時の expected_from が古い値だと 409 になるので
            // ここで先に同期しておく。
            updateServerBuildingId(currentBuildingIdRef.current);
            // Sidebar / RightSidebar の status / details を再 fetch させて
            // D-1 マーカーや滞在ユーザー表示をサーバの新しい現在地に追従させる。
            setMoveTrigger(prev => prev + 1);

            await consumeReplyStream(res, 'send');
        } catch (error) {
            console.error(error);
            setMessages(prev => [...prev, {
                role: 'system',
                content: '送信できませんでした。接続を確認してもう一度お試しください。',
                isError: true,
                errorCode: 'send_failed',
                timestamp: new Date().toISOString(),
            }]);
            await finishReplyCycle();
        }
    };

    // 追加の推論は必ずこの二つのボタンの後ろにある (2026-08-25 まはー裁定)。
    // どちらも発言を送り直さない — 起こすのは応答だけ。
    const runMessageAction = async (
        endpoint: 'continue' | 'retry',
        messageId: string,
    ) => {
        if (loadingStatus) return;
        clearTransientNotices();
        isProcessingRef.current = true;
        setLoadingStatus('Thinking...');
        // 押した瞬間にボタンを下ろす。二度押しで二重に走らせない。
        setMessages(prev => prev.map(m => (
            m.id === messageId
                ? { ...m, ...(endpoint === 'continue' ? { interrupted: false } : { needsRetry: false }) }
                : m
        )));
        // 下ろしたボタンを戻す。**線は「応答が生まれたか」** — 「ストリームが
        // 始まったか」ではない。始まっても中で「応答できる相手がいません」が
        // 返るだけの回があり、そこで戻さないとボタンが消えたまま残る。
        // ただし、やり直しても結果が変わらない終わり方 (相手が居ない) では
        // 戻さない — 押しても同じ結果しか返らない操作を出し続けることになる。
        const restoreAffordance = (retryUseless = false) => {
            setMessages(prev => prev.map(m => (
                m.id === messageId
                    ? {
                        ...m,
                        ...(endpoint === 'continue'
                            ? { interrupted: true }
                            : { needsRetry: true, retryUseless }),
                    }
                    : m
            )));
        };
        try {
            const res = await fetch(`/api/chat/${endpoint}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message_id: messageId }),
            });
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            const outcome = await consumeReplyStream(res, endpoint);
            if (!outcome.replied) {
                restoreAffordance(
                    RETRY_CHANGES_NOTHING.has(outcome.errorCode || ''),
                );
            }
        } catch (error) {
            console.error(error);
            restoreAffordance();
            setMessages(prev => [...prev, {
                role: 'system',
                content: endpoint === 'continue'
                    ? '続きを起こせませんでした。'
                    : 'やり直せませんでした。',
                isError: true,
                errorCode: 'action_failed',
                timestamp: new Date().toISOString(),
            }]);
            await finishReplyCycle();
        }
    };

    // 発言を「なかったことにする」。取り消せるかどうかは好みでは決まらず、
    // ペルソナがもう読んだかで決まる (読まれた後は取り消せない)。消すのではなく
    // 入力欄へ返す — ユーザーの発言はユーザーのものなので、手元に戻す形にする。
    const handleWithdrawMessage = async (messageId: string) => {
        if (loadingStatus) return;
        // 押した瞬間に閉じる。取り消しは元に戻せない操作なので、2 発目が飛ぶと
        // 「1 発目で消えた発言」を相手に、2 発目が別の結果 (not_found など) を
        // 返して画面に出る。
        if (withdrawingRef.current) return;
        withdrawingRef.current = messageId;
        setWithdrawingId(messageId);
        clearTransientNotices();
        try {
            const res = await fetch('/api/chat/withdraw', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message_id: messageId }),
            });
            if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
            const data = await res.json();
            if (data.withdrawn) {
                setMessages(prev => prev.filter(m => m.id !== messageId));
                setInputValue(prev => (
                    prev.trim() ? `${prev}\n${data.content || ''}` : (data.content || '')
                ));
                requestAnimationFrame(() => adjustTextareaHeight());
            } else {
                // 取り消せなかった = 何も起きていない。ここで needsRetry を
                // 下ろすと「再送」まで消え、**まさに再送が要る場面**
                // (読まれたが返事が生まれなかった発言) で復旧手段が無くなる。
                // 二度と成功しない「取り消す」だけを引っ込める。
                setMessages(prev => prev.map(m => (
                    m.id === messageId ? { ...m, withdrawBlocked: true } : m
                )));
                setMessages(prev => [...prev, {
                    role: 'system',
                    content: data.message || '取り消せませんでした。',
                    isInfo: true,
                    timestamp: new Date().toISOString(),
                }]);
            }
        } catch (error) {
            console.error(error);
            setMessages(prev => [...prev, {
                role: 'system',
                content: '取り消しをサーバーに届けられませんでした。',
                isError: true,
                errorCode: 'action_failed',
                timestamp: new Date().toISOString(),
            }]);
        } finally {
            withdrawingRef.current = null;
            setWithdrawingId(null);
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

    // Video files are uploaded via multipart so we never base64-encode the
    // whole file in the browser. The endpoint normalizes via ffmpeg
    // (1FPS 480p, 90s cap) and returns a saiverse://video/<filename> reference
    // we can hand off to /api/chat/send by URI instead of by inline data.
    const uploadVideoToServer = async (file: File): Promise<{ uri: string }> => {
        const fd = new FormData();
        fd.append('file', file);
        const res = await fetch('/api/media/upload-video', { method: 'POST', body: fd });
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try {
                const body = await res.json();
                if (body?.detail) detail = body.detail;
            } catch { /* response wasn't JSON */ }
            throw new Error(detail);
        }
        const data = await res.json();
        if (!data?.filename) throw new Error('Server did not return a video filename');
        return { uri: `saiverse://video/${data.filename}` };
    };

    const addFile = (file: File) => {
        const mimeType = file.type || 'application/octet-stream';
        const fileType = getFileType(file.name, mimeType);
        const id = `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

        if (fileType === 'video') {
            // Inline preview uses a blob URL — keeps the file referenced by handle
            // instead of duplicating bytes as base64 in memory.
            const previewUrl = URL.createObjectURL(file);
            setAttachments(prev => [...prev, {
                id, name: file.name, type: fileType, mimeType,
                previewUrl, uploading: true,
            }]);
            uploadVideoToServer(file).then(({ uri }) => {
                setAttachments(prev => prev.map(a =>
                    a.id === id ? { ...a, uri, uploading: false } : a
                ));
            }).catch(err => {
                console.error('Video upload failed:', err);
                setAttachments(prev => prev.map(a =>
                    a.id === id ? { ...a, uploading: false, error: String(err?.message || err) } : a
                ));
            });
            return;
        }

        // image / audio / document: keep the existing base64 path (small payloads)
        const reader = new FileReader();
        reader.onloadend = () => {
            const base64 = reader.result as string;
            setAttachments(prev => [...prev, {
                id, name: file.name, type: fileType, mimeType, base64,
            }]);
        };
        reader.readAsDataURL(file);
    };

    const handleFileUpload = (e: ChangeEvent<HTMLInputElement>) => {
        if (e.target.files && e.target.files.length > 0) {
            const files = Array.from(e.target.files);
            files.forEach(addFile);
            // Reset input to allow selecting the same files again
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };

    const removeAttachment = (index: number) => {
        setAttachments(prev => {
            const target = prev[index];
            if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
            return prev.filter((_, i) => i !== index);
        });
    };

    const clearAllAttachments = () => {
        setAttachments(prev => {
            prev.forEach(a => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
            return [];
        });
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
            const attachmentTypes = attachments.map(a => {
                if (a.type === 'image') return 'image';
                if (a.type === 'audio') return 'audio';
                if (a.type === 'video') return 'video';
                return 'document';
            });
            // ツール指定モードのセンチネルは meta_playbook として送らない
            // (サーバー側に該当 Playbook はなく、preview のコンテキスト計算は
            // どちらのモードでも default Playbook 基準で問題ない)。
            const previewMetaPlaybook = selectedPlaybook === TOOL_MODE_SELECTED
                ? undefined
                : (selectedPlaybook || undefined);
            const res = await fetch('/api/chat/preview', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message: inputValue || '(empty)',
                    building_id: currentBuildingIdRef.current,
                    meta_playbook: previewMetaPlaybook,
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
        files.forEach(addFile);
    };

    // ゲーム外からセッションログを閲覧中か (= 入力を read-only にする条件)
    const sessionLogReadOnly = !!activeGame && !activeGame.inside && sessionLogPeek;

    return (
        <div
            className={styles.container}
            onTouchStart={handleTouchStart}
            onTouchMove={handleTouchMove}
        >
            <SystemAlertBanner />
            <Sidebar
                refreshTrigger={backendConnected}
                viewingBuildingId={currentBuildingId}
                serverMoveTrigger={moveTrigger}
                onMove={(buildingId?: string) => {
                    if (!buildingId) return;
                    // C-1 閲覧モード: サーバ側の CURRENT_BUILDINGID は変えず、
                    // UI 上の表示建物だけ切り替える。 サーバへの move は
                    // 発言時に /chat/utter が atomic に行う (= C-2)。
                    setCurrentBuildingId(buildingId);
                    currentBuildingIdRef.current = buildingId;
                    // 建物を選んだ = その建物のログを見たい。セッションログ閲覧は解除
                    updateSessionLogPeek(false);
                    setMessages([]);
                    setIsHistoryLoaded(false);
                    fetchHistory(undefined, buildingId);
                    fetchBuildingInfo(buildingId);
                    setMoveTrigger(prev => prev + 1);
                    // Sidebar からの遷移はチャット閲覧目的なのでマップは閉じる
                    setIsMapModalOpen(false);
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
                        {activeGame && (activeGame.inside ? (
                            currentBuildingId === serverBuildingId ? (
                                <span
                                    title={`セッションログ表示中 (${activeGame.region_name ?? activeGame.region_id})${activeGame.scene ? ` / scene: ${activeGame.scene}` : ''}`}
                                    style={{ fontSize: '0.8rem', opacity: 0.75, whiteSpace: 'nowrap' }}
                                >
                                    🎲 {activeGame.region_name ?? 'ゲーム'}{activeGame.phase === 'paused' ? ' (中断中)' : ''}
                                </span>
                            ) : (
                                <span
                                    title={`${activeGame.region_name ?? activeGame.region_id} でゲーム進行中。この画面は閲覧中の建物のログです (実在地に戻るとセッションログ表示)`}
                                    style={{ fontSize: '0.8rem', opacity: 0.45, whiteSpace: 'nowrap' }}
                                >
                                    🎲 {activeGame.region_name ?? 'ゲーム'}で進行中
                                </span>
                            )
                        ) : (
                            // ゲーム外 (入口含む): どこに居てもログ閲覧 + 復帰を出す。
                            // 復帰の認可は参加者資格 (場所要件なし、docs/intent/region.md §7)
                            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: '0.8rem', whiteSpace: 'nowrap' }}>
                                <span
                                    title={`『${activeGame.region_name ?? activeGame.region_id}』${activeGame.at_entrance ? 'の入口に居ます' : 'から離脱中です'}${activeGame.phase === 'paused' ? ' (ゲームは中断中)' : ''}`}
                                    style={{ opacity: 0.75 }}
                                >
                                    🎲 {activeGame.region_name ?? 'ゲーム'}{activeGame.phase === 'paused' ? ' (中断中)' : ''}
                                </span>
                                <button
                                    onClick={toggleSessionLogPeek}
                                    title={sessionLogPeek ? '通常のチャットに戻る' : 'セッションログを閲覧する (発言はできません)'}
                                    style={{
                                        background: 'rgba(120,180,255,0.12)',
                                        border: '1px solid rgba(120,180,255,0.4)',
                                        borderRadius: 6, color: 'inherit', cursor: 'pointer',
                                        padding: '2px 8px', fontSize: '0.75rem', whiteSpace: 'nowrap',
                                    }}
                                >
                                    {sessionLogPeek ? '💬 チャットに戻る' : '📜 セッションログ'}
                                </button>
                                <button
                                    onClick={handleRejoinGame}
                                    title="パーティーの現在地へ移動してゲームに戻る"
                                    style={{
                                        background: 'rgba(130,220,160,0.15)',
                                        border: '1px solid rgba(130,220,160,0.5)',
                                        borderRadius: 6, color: 'inherit', cursor: 'pointer',
                                        padding: '2px 8px', fontSize: '0.75rem', whiteSpace: 'nowrap',
                                    }}
                                >
                                    ▶ 復帰
                                </button>
                            </span>
                        ))}
                    </div>
                    <div className={styles.headerRight}>
                        <ActiveClientIndicator isActive={isActiveClientTab} />
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
                            className={`${styles.iconBtn} ${isMapModalOpen ? styles.active : ''}`}
                            onClick={() => setIsMapModalOpen(v => !v)}
                            title={isMapModalOpen ? '街マップを閉じる' : '街マップを開く'}
                        >
                            <MapIcon size={20} />
                        </button>
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
                    {messages.map((msg, idx) => {
                        // System notices (world events / warnings / info) are NOT AI utterances:
                        // render them author-less and compact, distinct from user/assistant bubbles.
                        // Errors stay as assistant cards (role 'assistant', has retry/detail affordances).
                        const isSystemNotice = (msg.role === 'host' || msg.role === 'system') && !msg.isError;
                        if (isSystemNotice) {
                            return (
                                <div key={msg.id || idx} className={styles.systemNotice}>
                                    {msg.isWarning ? (
                                        <div className={`${styles.systemNoticeInner} ${styles.systemNoticeWarning}`}>
                                            <span className={styles.systemNoticeIcon}>⚠️</span>
                                            <span>{msg.content}</span>
                                        </div>
                                    ) : msg.isInfo ? (
                                        <div className={`${styles.systemNoticeInner} ${styles.systemNoticeInfo}`}>
                                            <span className={styles.systemNoticeIcon}>ℹ️</span>
                                            <span>{msg.content}</span>
                                        </div>
                                    ) : (
                                        <div className={styles.systemNoticeInner}>
                                            <ReactMarkdown
                                                remarkPlugins={MARKDOWN_REMARK_PLUGINS}
                                                rehypePlugins={MARKDOWN_REHYPE_PLUGINS}
                                                urlTransform={markdownUrlTransform}
                                                components={markdownComponents}
                                            >{prepareMessageMarkdown(msg.content)}</ReactMarkdown>
                                        </div>
                                    )}
                                    {msg.timestamp && (
                                        <span className={styles.systemNoticeTime}>{new Date(msg.timestamp).toLocaleString()}</span>
                                    )}
                                </div>
                            );
                        }
                        return (
                        <div key={msg.id || idx} className={`${styles.message} ${styles[msg.role]}`}>
                            <div className={`${styles.card} ${msg.isError ? styles.errorCard : ''} ${msg.isError && msg.errorCode ? styles[`error_${msg.errorCode}`] : ''}`}>
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
                                    {msg.audios && msg.audios.length > 0 && (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', margin: '0.4rem 0' }}>
                                            {msg.audios.map((a, idx) => (
                                                <audio
                                                    key={`a-${idx}`}
                                                    controls
                                                    preload="metadata"
                                                    src={a.url}
                                                    style={{ width: '100%', maxWidth: '420px' }}
                                                >
                                                    お使いのブラウザは audio タグをサポートしていません。
                                                </audio>
                                            ))}
                                        </div>
                                    )}
                                    {msg.videos && msg.videos.length > 0 && (
                                        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem', margin: '0.4rem 0' }}>
                                            {msg.videos.map((v, idx) => (
                                                <video
                                                    key={`v-${idx}`}
                                                    controls
                                                    preload="metadata"
                                                    src={v.url}
                                                    style={{ width: '100%', maxWidth: '480px', borderRadius: '6px' }}
                                                >
                                                    お使いのブラウザは video タグをサポートしていません。
                                                </video>
                                            ))}
                                        </div>
                                    )}
                                    {msg.isError ? (
                                        <div className={styles.errorContent}>
                                            <div className={styles.errorHeader}>
                                                <span className={styles.errorIcon}>
                                                    {({rate_limit: '⏱️', timeout: '⏰', safety_filter: '🛡️', server_error: '🔧', empty_response: '📭', authentication: '🔑', payment: '💳', no_response: '💭', no_responder: '🚪', unknown_outcome: '❓', stream_broken: '🔌', send_failed: '🔌', message_not_found: '🔍', location_conflict: '📍'} as Record<string, string>)[msg.errorCode || ''] || '⚠️'}
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
                                                    // 発言は届いている。だから送り直しではなく、
                                                    // その発言の「再送」ボタンで応答だけを求める。
                                                    no_response: 'あなたの発言は記録に残っています。返事だけが生まれなかったので、発言の「再送」から応答をもう一度求められます。',
                                                    // 応答できる相手がいない回。ここで「再送」を勧めると、
                                                    // 何度押しても結果の変わらない操作を勧めることになる。
                                                    // できるのは場所を変えるか、誰かが来るのを待つこと。
                                                    no_responder: 'あなたの発言は記録に残っています。ただし、この場所には応答できる相手がいないので、やり直しても結果は変わりません。別の場所へ移るか、誰かが来るのを待ってください。',
                                                    // 出口 7: こちら側からは届いたかどうか分からない。
                                                    // 分かった顔をせず、確認の手立てだけを示す。
                                                    unknown_outcome: '発言が届いたかどうかは、この画面からは判断できません。同じ内容を送り直す前に、履歴に残っているかを確認してください。',
                                                    stream_broken: '接続が途中で切れました。ここまでの内容は残っています。',
                                                    send_failed: 'サーバーに接続できませんでした。SAIVerse が起動しているかを確認してください。',
                                                    message_not_found: 'この発言は記録に残っていません。入力欄からもう一度送ってください。',
                                                    // 他の画面が先に移動していた回。発言はサーバーに
                                                    // 届いていないので、本文は入力欄へ返してある。
                                                    location_conflict: '発言は保存されていません。本文は入力欄に戻したので、いまいる場所を確かめてから送り直してください。',
                                                    empty_message: '空のまま送信されました。内容を入れてから送ってください。',
                                                    no_current_building: 'いまいる場所が確定していません。画面を再読み込みするか、建物を選び直してください。',
                                                    action_failed: '操作をサーバーに届けられませんでした。接続を確認してもう一度お試しください。',
                                                } as Record<string, string>)[msg.errorCode || ''] || '予期しないエラーが発生しました。問題が続く場合は管理者に連絡してください。'}
                                            </div>
                                            {msg.errorDetail && (
                                                <details className={styles.errorDetails}>
                                                    <summary>Technical Details</summary>
                                                    <pre>{msg.errorDetail}</pre>
                                                </details>
                                            )}
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
                                            {msg.auto_recall && (
                                                <details className={styles.recallBlock}>
                                                    <summary className={styles.recallSummary}>
                                                        <span className={styles.recallIcon}>
                                                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>
                                                        </span>
                                                        <span>ふと浮かんだ記憶</span>
                                                    </summary>
                                                    <div className={styles.recallContent}>
                                                        {msg.auto_recall}
                                                    </div>
                                                </details>
                                            )}
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
                                                remarkPlugins={MARKDOWN_REMARK_PLUGINS}
                                                rehypePlugins={MARKDOWN_REHYPE_PLUGINS}
                                                urlTransform={markdownUrlTransform}
                                                components={markdownComponents}
                                            >{prepareMessageMarkdown(msg.content)}</ReactMarkdown>
                                        </>
                                    )}
                                </div>
                                {(msg.timestamp || msg.llm_usage || msg.llm_usage_total) && (
                                    <div className={styles.cardFooter}>
                                        {msg.timestamp && <span>{new Date(msg.timestamp).toLocaleString()}</span>}
                                        <CacheHitDot usage={msg.llm_usage} total={msg.llm_usage_total} />
                                        {msg.llm_usage_total && msg.llm_usage_total.call_count > 1 ? (
                                            // Show total usage when multiple LLM calls were made
                                            <span className={styles.llmUsageWrap}>
                                                <span className={styles.llmUsage} onClick={(e) => { e.stopPropagation(); setUsageTooltipId(prev => prev === (msg.id || `msg-${idx}`) ? null : (msg.id || `msg-${idx}`)); }}>
                                                    {msg.llm_usage_total.call_count} calls · {(msg.llm_usage_total.total_input_tokens + msg.llm_usage_total.total_output_tokens).toLocaleString()} tokens · {formatCost(msg.llm_usage_total.total_cost_usd, msg.llm_usage_total.currency)}
                                                </span>
                                                {usageTooltipId === (msg.id || `msg-${idx}`) && (
                                                    <div className={styles.usageTooltip}>
                                                        <div>Models: {msg.llm_usage_total.models_used.join(', ')}</div>
                                                        <div>LLM Calls: {msg.llm_usage_total.call_count}</div>
                                                        <div>Total Input: {msg.llm_usage_total.total_input_tokens.toLocaleString()} tokens{msg.llm_usage_total.total_cached_tokens ? ` (${msg.llm_usage_total.total_cached_tokens.toLocaleString()} cached)` : ''}</div>
                                                        <div>Total Output: {msg.llm_usage_total.total_output_tokens.toLocaleString()} tokens</div>
                                                        <div>Total Cost: {formatCost(msg.llm_usage_total.total_cost_usd, msg.llm_usage_total.currency)}</div>
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
                                                        <div>Cost: {formatCost(msg.llm_usage.cost_usd || 0, msg.llm_usage.currency)}</div>
                                                    </div>
                                                )}
                                            </span>
                                        )}
                                    </div>
                                )}
                                <div className={`${styles.cardActions} ${(msg.interrupted || msg.needsRetry) ? styles.cardActionsPinned : ''}`}>
                                    <button
                                        className={`${styles.actionBtn} ${copiedMessageId === (msg.id || `msg-${idx}`) ? styles.copied : ''}`}
                                        onClick={() => handleCopyMessage(msg.id || `msg-${idx}`, msg.content)}
                                        title="Copy message"
                                    >
                                        {copiedMessageId === (msg.id || `msg-${idx}`) ? <Check size={14} /> : <Copy size={14} />}
                                    </button>
                                    {/* 途中で終わった発言にだけ「続きの生成」を出す。
                                        追加の推論はユーザーの一押しの後ろに置く。 */}
                                    {msg.role === 'assistant' && msg.interrupted && msg.id && (
                                        <button
                                            className={`${styles.actionBtn} ${styles.continueBtn}`}
                                            onClick={() => runMessageAction('continue', msg.id as string)}
                                            disabled={!!loadingStatus}
                                            title="この発言は途中で終わっています。続きを話してもらう"
                                        >
                                            <CornerDownRight size={14} />
                                            <span className={styles.actionBtnLabel}>続きの生成</span>
                                        </button>
                                    )}
                                    {/* 返事が来なかった発言にだけ「再送」を出す。発言は
                                        残っているので、押しても送り直しにはならない。 */}
                                    {msg.role === 'user' && msg.needsRetry
                                        && !msg.retryUseless && msg.id && (
                                        <button
                                            className={`${styles.actionBtn} ${styles.retryBtn}`}
                                            onClick={() => runMessageAction('retry', msg.id as string)}
                                            disabled={!!loadingStatus}
                                            title="この発言に返事が来ていません。もう一度応答を求める"
                                        >
                                            <RotateCcw size={14} />
                                            <span className={styles.actionBtnLabel}>再送</span>
                                        </button>
                                    )}
                                    {/* 返事が来なかった発言は、なかったことにもできる。
                                        ただしペルソナがもう読んでいたら断られる。 */}
                                    {msg.role === 'user' && msg.needsRetry
                                        && !msg.withdrawBlocked && msg.id && (
                                        <button
                                            className={`${styles.actionBtn} ${styles.withdrawBtn}`}
                                            onClick={() => handleWithdrawMessage(msg.id as string)}
                                            disabled={!!loadingStatus || withdrawingId === msg.id}
                                            title="この発言を取り消して、入力欄に戻す（まだ誰も読んでいない場合のみ）"
                                        >
                                            <Undo2 size={14} />
                                            <span className={styles.actionBtnLabel}>取り消す</span>
                                        </button>
                                    )}
                                    {/* アドオンバブルボタン（assistantメッセージにのみ表示） */}
                                    {msg.role === 'assistant' && addonBubbleButtons.length > 0 && (
                                        <AddonBubbleButtons
                                            messageId={msg.id || `msg-${idx}`}
                                            messageText={msg.content}
                                            personaId={msg.persona_id}
                                            addonMetadata={addonMetadata[msg.id || `msg-${idx}`] ?? {}}
                                            buttons={addonBubbleButtons}
                                        />
                                    )}
                                </div>
                            </div>
                        </div>
                        );
                    })}
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
                        {rpdUsage && (
                            <span
                                className={`${styles.rpdBadge} ${rpdUsage.used >= rpdUsage.limit ? styles.rpdExhausted : rpdUsage.used >= rpdUsage.limit * 0.8 ? styles.rpdWarning : ''}`}
                                title={`RPD: ${rpdUsage.used}/${rpdUsage.limit} (リセット: 太平洋時間 0:00)`}
                            >
                                {rpdUsage.used}/{rpdUsage.limit}
                            </span>
                        )}
                        <ToolModeSelector
                            selectedPlaybook={selectedPlaybook}
                            onPlaybookChange={setSelectedPlaybook}
                            playbookArgs={playbookArgs}
                            onPlaybookArgsChange={setPlaybookArgs}
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
                                <div key={att.id} style={{
                                    padding: '0.25rem 0.5rem',
                                    background: att.error ? '#fde2e2' : '#eee',
                                    borderRadius: '4px',
                                    display: 'inline-flex',
                                    alignItems: 'center',
                                    gap: '0.5rem',
                                    color: att.error ? '#b91c1c' : '#333'
                                }}>
                                    <span>{
                                        att.type === 'image' ? '🖼'
                                        : att.type === 'audio' ? '🎵'
                                        : att.type === 'video' ? '🎬'
                                        : '📄'
                                    } {att.name}{
                                        att.uploading ? ' (アップロード中…)'
                                        : att.error ? ` (失敗: ${att.error})`
                                        : ''
                                    }</span>
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
                            accept="image/*,audio/*,video/*,.txt,.md,.py,.js,.ts,.tsx,.json,.yaml,.yml,.csv,.html,.css,.xml,.log,.sh,.bat,.sql,.java,.c,.cpp,.h,.hpp,.go,.rs,.rb,.swift,.kt,.scala,.r,.lua,.pl,.pdf"
                        />
                        <textarea
                            ref={textareaRef}
                            value={inputValue}
                            onChange={(e) => setInputValue(e.target.value)}
                            onKeyDown={handleKeyDown}
                            // ゲーム外でのセッションログ閲覧は read-only (発言は通常
                            // チャットか、復帰してゲーム内で行う)
                            disabled={sessionLogReadOnly}
                            placeholder={sessionLogReadOnly
                                ? 'セッションログは閲覧専用です。発言するには「復帰」するかチャットに戻ってください。'
                                : 'メッセージを入力...'}
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
                                disabled={(!inputValue.trim() && attachments.length === 0) || sessionLogReadOnly}
                            >
                                <Send size={20} />
                            </button>
                        )}
                    </div>
                </div>
            </main>

            {/* チュートリアル表示中は街マップを開かない。初回起動では map (デフォルト ON) と
                チュートリアルの表示条件が同時に成立するため、判定完了 (tutorialChecked) かつ
                チュートリアル非表示のときだけ開く。これで初回のマップちらつきと、チュートリアル
                裏でのマップ mount/ポーリングを防ぐ。チュートリアル完了時は reload が走るので、
                現在地が設定された状態で改めてマップが現在地中心で開く。 */}
            {isMapModalOpen && tutorialChecked && !showTutorial && (
                <ModalOverlay onClose={() => setIsMapModalOpen(false)}>
                    <div className={cityMapStyles.modalShell}>
                        <CityMap
                            currentBuildingId={currentBuildingId}
                            onSelectBuilding={handleSelectBuildingFromMap}
                            refreshTrigger={moveTrigger}
                            onClose={() => setIsMapModalOpen(false)}
                        />
                    </div>
                </ModalOverlay>
            )}

            <RightSidebar
                isOpen={isInfoOpen}
                onClose={() => setIsInfoOpen(false)}
                refreshTrigger={moveTrigger}
                currentBuildingId={currentBuildingId}
                onPersonaChanged={() => setMoveTrigger(prev => prev + 1)}
            />

            <ChatOptions
                isOpen={isOptionsOpen}
                onClose={() => setIsOptionsOpen(false)}
                currentModel={selectedModel}
                buildingId={currentBuildingId}
                onModelChange={(id, displayName, rateLimit) => {
                    setSelectedModel(id);
                    setSelectedModelDisplayName(displayName);
                    setSelectedModelRateLimit(rateLimit || null);
                    setRpdUsage(null); // Reset RPD on model change
                }}
            />

            <PeopleModal
                isOpen={isPeopleModalOpen}
                onClose={() => setIsPeopleModalOpen(false)}
                currentBuildingId={currentBuildingId}
                onChanged={() => setMoveTrigger(prev => prev + 1)}
            />

            <ItemModal
                isOpen={!!linkItemModalItem}
                onClose={() => setLinkItemModalItem(null)}
                item={linkItemModalItem}
                currentBuildingId={currentBuildingId}
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

            {spellConfirm && (
                <SpellConfirmDialog
                    request={spellConfirm}
                    onRespond={handleSpellConfirmResponse}
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
