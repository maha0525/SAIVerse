
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useMemo, useRef } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import SaiverseLink from '../SaiverseLink';
import { Book, ChevronRight, ChevronDown, ChevronLeft, History, Clock, GitCommit, Tag, Edit2, Trash2, Save, X, Plus, FolderTree, Sparkles, Star } from 'lucide-react';
import styles from './MemopediaViewer.module.css';

interface MemopediaPage {
    id: string;
    title: string;
    summary: string;
    keywords: string[];
    // P4-c: vividness は廃止。フィールドを除去した。
    is_trunk: boolean;
    is_important: boolean;
    updated_at?: number;
    last_referenced_at?: number;
    children: MemopediaPage[];
}

interface CategoryMeta {
    key: string;
    label: string;
    label_en: string;
    hide_when_empty: boolean;
    can_generate: boolean;
    writable: boolean;
}

type TreeStructure = Record<string, MemopediaPage[]>;

interface EditHistoryEntry {
    id: string;
    page_id: string;
    edited_at: number;
    diff_text: string;
    ref_start_message_id: string | null;
    ref_end_message_id: string | null;
    edit_type: string;
    edit_source: string | null;
}

interface MemopediaFragment {
    id: string;
    content: string;
    source_date: string | null;
    chronicle_entry_id: string | null;
    created_at: number;
}

interface MemopediaViewerProps {
    personaId: string;
}

// Collect all page IDs that have children (for default expansion)
function collectExpandableIds(pages: MemopediaPage[]): Set<string> {
    const ids = new Set<string>();
    const traverse = (page: MemopediaPage) => {
        if (page.children && page.children.length > 0) {
            ids.add(page.id);
            page.children.forEach(traverse);
        }
    };
    pages.forEach(traverse);
    return ids;
}

// Default categories before API response arrives (matches CATEGORY_DEFS order)
const DEFAULT_CATEGORIES: CategoryMeta[] = [
    { key: "people", get label() { return uiText("components.memory.MemopediaViewer.text001"); }, label_en: "People", hide_when_empty: false, can_generate: true, writable: true },
    { key: "terms", get label() { return uiText("components.memory.MemopediaViewer.text002"); }, label_en: "Terms", hide_when_empty: false, can_generate: true, writable: true },
    { key: "plans", get label() { return uiText("components.memory.MemopediaViewer.text003"); }, label_en: "Plans", hide_when_empty: false, can_generate: true, writable: true },
    { key: "events", get label() { return uiText("components.memory.MemopediaViewer.text004"); }, label_en: "Events", hide_when_empty: false, can_generate: true, writable: true },
    { key: "theme", get label() { return uiText("components.memory.MemopediaViewer.text005"); }, label_en: "Themes", hide_when_empty: true, can_generate: false, writable: false },
];

export default function MemopediaViewer({ personaId }: MemopediaViewerProps) {
    useLocale();
    const [tree, setTree] = useState<TreeStructure | null>(null);
    const [categories, setCategories] = useState<CategoryMeta[]>(DEFAULT_CATEGORIES);
    const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
    const [pageContent, setPageContent] = useState<string>("");
    const [pageFragments, setPageFragments] = useState<MemopediaFragment[]>([]);
    const [isLoadingPage, setIsLoadingPage] = useState(false);
    const [showList, setShowList] = useState(true);

    // Expansion state: managed at parent level for persistence
    const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

    // Sort state
    type SortMode = 'tree' | 'updated';
    const [sortMode, setSortMode] = useState<SortMode>('updated');

    // History state
    const [showHistory, setShowHistory] = useState(false);
    const [editHistory, setEditHistory] = useState<EditHistoryEntry[]>([]);
    const [isLoadingHistory, setIsLoadingHistory] = useState(false);
    const [selectedHistoryEntry, setSelectedHistoryEntry] = useState<EditHistoryEntry | null>(null);

    // Edit mode state
    const [isEditing, setIsEditing] = useState(false);
    const [editTitle, setEditTitle] = useState("");
    const [editSummary, setEditSummary] = useState("");
    const [editContent, setEditContent] = useState("");
    const [editKeywords, setEditKeywords] = useState("");
    // P4-c: editVividness は廃止。
    const [isSaving, setIsSaving] = useState(false);

    // Delete confirmation state
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [isDeleting, setIsDeleting] = useState(false);

    // Create page modal state
    const [showCreateModal, setShowCreateModal] = useState(false);
    const [createParentId, setCreateParentId] = useState<string>("");
    const [createTitle, setCreateTitle] = useState("");
    const [createSummary, setCreateSummary] = useState("");
    const [createContent, setCreateContent] = useState("");
    const [createKeywords, setCreateKeywords] = useState("");
    // P4-c: createVividness は廃止。
    const [createIsTrunk, setCreateIsTrunk] = useState(false);
    const [isCreating, setIsCreating] = useState(false);

    // Generation state
    const [showGenerateModal, setShowGenerateModal] = useState(false);
    const [generateKeyword, setGenerateKeyword] = useState("");
    const [generateDirections, setGenerateDirections] = useState("");
    const [generateCategory, setGenerateCategory] = useState<string | null>(null);
    const [isGenerating, setIsGenerating] = useState(false);
    const [generateJobId, setGenerateJobId] = useState<string | null>(null);
    const [generateStatus, setGenerateStatus] = useState<string>("");
    const [generateProgress, setGenerateProgress] = useState<{ current: number, total: number } | null>(null);
    const [generateError, setGenerateError] = useState<string | null>(null);
    const [generateResult, setGenerateResult] = useState<any>(null);
    const pollIntervalRef = useRef<NodeJS.Timeout | null>(null);

    useEffect(() => {
        loadTree();
    }, [personaId]);

    // Set default expansion when tree loads
    useEffect(() => {
        if (tree) {
            const allExpandable = new Set<string>();
            Object.values(tree).forEach(pages => {
                if (Array.isArray(pages)) {
                    collectExpandableIds(pages).forEach(id => allExpandable.add(id));
                }
            });
            setExpandedIds(allExpandable);
        }
    }, [tree]);

    useEffect(() => {
        if (selectedPageId) {
            loadPage(selectedPageId);
            setShowHistory(false);
            setSelectedHistoryEntry(null);
            setIsEditing(false);
        } else {
            setPageContent("");
        }
    }, [selectedPageId]);

    const loadTree = async () => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/tree`);
            if (res.ok) {
                const data = await res.json();
                // Extract categories meta if present, else keep defaults
                if (data.categories && Array.isArray(data.categories)) {
                    setCategories(data.categories);
                }
                // Ensure all category arrays exist (backward compat for old personas)
                const treeData: TreeStructure = {};
                const cats: CategoryMeta[] = data.categories || DEFAULT_CATEGORIES;
                for (const cat of cats) {
                    treeData[cat.key] = data[cat.key] || [];
                }
                setTree(treeData);
            }
        } catch (error) {
            console.error("Failed to load memopedia tree", error);
        }
    };

    const loadPage = async (pageId: string) => {
        setIsLoadingPage(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${pageId}`);
            if (res.ok) {
                const data = await res.json();
                setPageContent(data.content);
                setPageFragments(data.fragments || []);
            }
        } catch (error) {
            console.error("Failed to load page content", error);
            setPageContent("*Failed to load content*");
            setPageFragments([]);
        } finally {
            setIsLoadingPage(false);
        }
    };

    const loadHistory = async (pageId: string) => {
        setIsLoadingHistory(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${pageId}/history`);
            if (res.ok) {
                const data = await res.json();
                setEditHistory(data.history);
            }
        } catch (error) {
            console.error("Failed to load edit history", error);
            setEditHistory([]);
        } finally {
            setIsLoadingHistory(false);
        }
    };

    const handleShowHistory = () => {
        if (selectedPageId) {
            setShowHistory(true);
            setIsEditing(false);
            loadHistory(selectedPageId);
        }
    };

    // Edit mode handlers
    const startEditing = () => {
        if (!selectedPageId || !tree) return;
        const allPages = Object.values(tree).flat() as MemopediaPage[];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        if (!page) return;

        // Parse the markdown content to extract title, summary, content
        // The pageContent from API is markdown: "# Title\n\n*summary*\n\ncontent"
        const lines = pageContent.split('\n');
        let title = page.title;
        let summary = page.summary;
        let content = "";

        // Try to extract from markdown
        let contentStartIdx = 0;

        // Extract title
        if (lines[0]?.startsWith('# ')) {
            title = lines[0].substring(2);
            contentStartIdx = 1;
        }

        // Skip empty lines after title
        while (contentStartIdx < lines.length && lines[contentStartIdx] === '') {
            contentStartIdx++;
        }

        // Extract summary
        if (contentStartIdx < lines.length &&
            lines[contentStartIdx]?.startsWith('*') &&
            lines[contentStartIdx]?.endsWith('*')) {
            summary = lines[contentStartIdx].slice(1, -1);
            contentStartIdx++;
        }

        // Skip empty lines after summary
        while (contentStartIdx < lines.length && lines[contentStartIdx] === '') {
            contentStartIdx++;
        }

        // Extract content (remaining lines)
        // Use trimEnd() to preserve leading whitespace (important for code blocks and indentation)
        content = lines.slice(contentStartIdx).join('\n').trimEnd();

        setEditTitle(title);
        setEditSummary(summary);
        setEditContent(content);
        setEditKeywords(page.keywords?.join(', ') || '');
        setIsEditing(true);
        setShowHistory(false);
    };

    const cancelEditing = () => {
        setIsEditing(false);
    };

    // P4-c: handleVividnessChange は廃止。代わりに机の開閉を管理する。
    const handleDeskToggle = async (open: boolean) => {
        if (!selectedPageId) return;
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}/desk`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ open }),
            });

            if (res.ok) {
                const data = await res.json();
                alert(data.message || (open ? uiText("components.memory.MemopediaViewer.text006") : uiText("components.memory.MemopediaViewer.text007")));
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text008", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to toggle desk', error);
            alert(uiText("components.memory.MemopediaViewer.text009"));
        }
    };

    const saveEdit = async () => {
        if (!selectedPageId) return;
        setIsSaving(true);
        try {
            const keywords = editKeywords
                .split(',')
                .map(k => k.trim())
                .filter(k => k.length > 0);

            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title: editTitle,
                    summary: editSummary,
                    content: editContent,
                    keywords,
                    // P4-c: vividness は廃止。送信しない。
                }),
            });

            if (res.ok) {
                setIsEditing(false);
                await loadTree();
                await loadPage(selectedPageId);
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text010", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to save page', error);
            alert(uiText("components.memory.MemopediaViewer.text011"));
        } finally {
            setIsSaving(false);
        }
    };

    const deletePage = async () => {
        if (!selectedPageId) return;
        setIsDeleting(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${selectedPageId}`, {
                method: 'DELETE',
            });

            if (res.ok) {
                setShowDeleteConfirm(false);
                setSelectedPageId(null);
                await loadTree();
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text012", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to delete page', error);
            alert(uiText("components.memory.MemopediaViewer.text013"));
        } finally {
            setIsDeleting(false);
        }
    };

    // Open create modal with parent set
    const openCreateModal = (parentId: string) => {
        setCreateParentId(parentId);
        setCreateTitle("");
        setCreateSummary("");
        setCreateContent("");
        setCreateKeywords("");
        // P4-c: createVividness は廃止。
        setCreateIsTrunk(false);
        setShowCreateModal(true);
    };

    // Create new page
    const createPage = async () => {
        if (!createTitle.trim()) {
            alert(uiText("components.memory.MemopediaViewer.text014"));
            return;
        }
        setIsCreating(true);
        try {
            const keywords = createKeywords
                .split(',')
                .map(k => k.trim())
                .filter(k => k.length > 0);

            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    parent_id: createParentId,
                    title: createTitle,
                    summary: createSummary,
                    content: createContent,
                    keywords,
                    // P4-c: vividness は廃止。送信しない。
                    is_trunk: createIsTrunk,
                }),
            });

            if (res.ok) {
                const data = await res.json();
                setShowCreateModal(false);
                await loadTree();
                // Select the newly created page
                setSelectedPageId(data.page.id);
                setShowList(false);
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text015", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to create page', error);
            alert(uiText("components.memory.MemopediaViewer.text016"));
        } finally {
            setIsCreating(false);
        }
    };

    // Toggle trunk flag
    const handleTrunkToggle = async (pageId: string, isTrunk: boolean) => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${pageId}/trunk`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_trunk: isTrunk }),
            });

            if (res.ok) {
                await loadTree();
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text017", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to toggle trunk', error);
            alert(uiText("components.memory.MemopediaViewer.text018"));
        }
    };

    // Toggle important flag
    const handleImportantToggle = async (pageId: string, isImportant: boolean) => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/pages/${pageId}/important`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ is_important: isImportant }),
            });

            if (res.ok) {
                await loadTree();
            } else {
                const err = await res.json();
                alert(uiText("components.memory.MemopediaViewer.text019", { p1: err.detail || 'Unknown error' }));
            }
        } catch (error) {
            console.error('Failed to toggle important', error);
            alert(uiText("components.memory.MemopediaViewer.text020"));
        }
    };

    // Generation handlers
    const startGeneration = async () => {
        if (!generateKeyword.trim()) return;

        setIsGenerating(true);
        setGenerateError(null);
        setGenerateStatus(uiText("components.memory.MemopediaViewer.text021"));

        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    keyword: generateKeyword,
                    directions: generateDirections || null,
                    category: generateCategory,
                    max_loops: 5,
                    context_window: 5,
                    with_chronicle: true,
                }),
            });

            if (!res.ok) {
                const err = await res.json();
                throw new Error(err.detail || 'Failed to start generation');
            }

            const data = await res.json();
            setGenerateJobId(data.job_id);

            // Start polling
            pollIntervalRef.current = setInterval(() => pollGenerationStatus(data.job_id), 2000);

        } catch (error: any) {
            console.error('Failed to start generation', error);
            setGenerateError(error.message || 'Failed to start generation');
            setIsGenerating(false);
        }
    };

    const pollGenerationStatus = async (jobId: string) => {
        try {
            const res = await apiFetch(`/api/people/${personaId}/memopedia/generate/${jobId}`);
            if (!res.ok) {
                throw new Error('Failed to get job status');
            }

            const data = await res.json();
            setGenerateStatus(data.message || uiText("components.memory.MemopediaViewer.text022"));

            if (data.progress !== undefined && data.total) {
                setGenerateProgress({ current: data.progress, total: data.total });
            }

            if (data.status === 'completed') {
                if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                }
                setIsGenerating(false);
                if (data.result) {
                    setGenerateResult(data.result);
                } else {
                    setGenerateError(data.message || 'No result generated');
                }
            } else if (data.status === 'failed') {
                if (pollIntervalRef.current) {
                    clearInterval(pollIntervalRef.current);
                    pollIntervalRef.current = null;
                }
                setIsGenerating(false);
                setGenerateError(data.error || 'Generation failed');
            }
        } catch (error: any) {
            console.error('Failed to poll status', error);
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
                pollIntervalRef.current = null;
            }
            setIsGenerating(false);
            setGenerateError(error.message || 'Failed to poll status');
        }
    };

    // Cleanup polling on unmount
    useEffect(() => {
        return () => {
            if (pollIntervalRef.current) {
                clearInterval(pollIntervalRef.current);
            }
        };
    }, []);

    const toggleExpand = (pageId: string) => {
        setExpandedIds(prev => {
            const next = new Set(prev);
            if (next.has(pageId)) {
                next.delete(pageId);
            } else {
                next.add(pageId);
            }
            return next;
        });
    };

    const formatDate = (timestamp: number) => {
        return new Date(timestamp * 1000).toLocaleString(getFormatLocale(), {
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit'
        });
    };

    const getEditTypeLabel = (editType: string) => {
        switch (editType) {
            case 'create': return uiText("components.memory.MemopediaViewer.text023");
            case 'update': return uiText("components.memory.MemopediaViewer.text024");
            case 'append': return uiText("components.memory.MemopediaViewer.text025");
            case 'delete': return uiText("components.memory.MemopediaViewer.text026");
            default: return editType;
        }
    };

    const TreeItem = ({ page }: { page: MemopediaPage }) => {
    useLocale();
        const hasChildren = page.children && page.children.length > 0;
        const isExpanded = expandedIds.has(page.id);
        const isRoot = page.id.startsWith('root_');

        const handleChevronClick = (e: React.MouseEvent) => {
            e.stopPropagation();
            toggleExpand(page.id);
        };

        const handlePageClick = () => {
            setSelectedPageId(page.id);
            if (!hasChildren && !isRoot) setShowList(false); // Mobile: go to content if leaf
        };

        const handleAddClick = (e: React.MouseEvent) => {
            e.stopPropagation();
            openCreateModal(page.id);
        };

        // P4-c: getVividnessClass は廃止。

        return (
            <div>
                <div
                    className={`${styles.pageItem} ${selectedPageId === page.id ? styles.active : ''} ${page.is_trunk ? styles.trunkItem : ''}`}
                    onClick={handlePageClick}
                >
                    {hasChildren || page.is_trunk || isRoot ? (
                        <span
                            className={styles.chevron}
                            onClick={handleChevronClick}
                        >
                            {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                        </span>
                    ) : (
                        <span style={{ display: 'inline-block', width: 16 }} />
                    )}
                    {page.is_trunk && <FolderTree size={14} className={styles.trunkIcon} />}
                    {page.is_important && <Star size={12} style={{ color: '#e6a817', flexShrink: 0 }} />}
                    <span className={page.is_trunk ? styles.trunkTitle : ''}>{page.title}</span>
                    {(page.is_trunk || isRoot) && (
                        <button data-i18n="components.memory.MemopediaViewer.text027"
                            className={styles.addChildBtn}
                            onClick={handleAddClick}
                            title={uiText("components.memory.MemopediaViewer.text027")}
                        >
                            <Plus size={12} />
                        </button>
                    )}
                </div>
                {isExpanded && hasChildren && (
                    <div className={styles.pageChildren}>
                        {page.children.map(child => <TreeItem key={child.id} page={child} />)}
                    </div>
                )}
            </div>
        );
    };

    // Helper to find selected page and get its keywords
    const getSelectedPageKeywords = (): string[] => {
        if (!tree || !selectedPageId) return [];
        const allPages = Object.values(tree).flat() as MemopediaPage[];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        const keywords = page?.keywords;
        // Handle case where keywords might be a JSON string instead of array
        if (typeof keywords === 'string') {
            try {
                const parsed = JSON.parse(keywords);
                return Array.isArray(parsed) ? parsed : [];
            } catch {
                return [];
            }
        }
        return Array.isArray(keywords) ? keywords : [];
    };

    // P4-c: getSelectedPageVividness は廃止。

    // Helper to find selected page and get its is_trunk
    const getSelectedPageIsTrunk = (): boolean => {
        if (!tree || !selectedPageId) return false;
        const allPages = Object.values(tree).flat() as MemopediaPage[];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        return page?.is_trunk || false;
    };

    // Helper to find selected page and get its is_important
    const getSelectedPageIsImportant = (): boolean => {
        if (!tree || !selectedPageId) return false;
        const allPages = Object.values(tree).flat() as MemopediaPage[];
        const findPage = (pages: MemopediaPage[]): MemopediaPage | null => {
            for (const p of pages) {
                if (p.id === selectedPageId) return p;
                const found = findPage(p.children);
                if (found) return found;
            }
            return null;
        };
        const page = findPage(allPages);
        return page?.is_important || false;
    };

    const selectedKeywords = getSelectedPageKeywords();
    // P4-c: selectedVividness は廃止。
    const selectedIsTrunk = getSelectedPageIsTrunk();
    const selectedIsImportant = getSelectedPageIsImportant();
    // P4-c: getVividnessLabel は廃止。

    // Sort helper: most recently referenced/updated first
    const pageFreshness = (p: MemopediaPage) => Math.max(p.last_referenced_at || 0, p.updated_at || 0);

    // Sort pages within each category by freshness (for tree mode)
    const sortedTree = useMemo((): TreeStructure | null => {
        if (!tree) return null;
        const sortPages = (pages: MemopediaPage[]) =>
            [...pages].sort((a, b) => pageFreshness(b) - pageFreshness(a));
        const result: TreeStructure = {};
        for (const key of Object.keys(tree)) {
            result[key] = sortPages(tree[key] || []);
        }
        return result;
    }, [tree]);

    // Flatten all pages for "updated" sort mode
    const flatPages = useMemo(() => {
        if (!tree || sortMode !== 'updated') return [];
        const pages: MemopediaPage[] = [];
        const collect = (page: MemopediaPage) => {
            if (!page.id.startsWith('root_')) {
                pages.push(page);
            }
            page.children?.forEach(collect);
        };
        Object.values(tree).forEach(catPages => catPages.forEach(collect));
        // Sort by most recently referenced/updated (newest first)
        const freshness = (p: MemopediaPage) => Math.max(p.last_referenced_at || 0, p.updated_at || 0);
        pages.sort((a, b) => freshness(b) - freshness(a));
        return pages;
    }, [tree, sortMode]);

    if (!tree) return <div data-i18n="components.memory.MemopediaViewer.text028" className={styles.emptyState}>{uiText("components.memory.MemopediaViewer.text028")}</div>;

    return (
        <div className={styles.container}>
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>
                    <span data-i18n="components.memory.MemopediaViewer.text029">{uiText("components.memory.MemopediaViewer.text029")}</span>
                    <div className={styles.sidebarActions}>
                        <button data-i18n="components.memory.MemopediaViewer.text030 components.memory.MemopediaViewer.text031"
                            className={`${styles.sortButton} ${sortMode === 'updated' ? styles.active : ''}`}
                            onClick={() => setSortMode(sortMode === 'tree' ? 'updated' : 'tree')}
                            title={sortMode === 'tree' ? uiText("components.memory.MemopediaViewer.text030") : uiText("components.memory.MemopediaViewer.text031")}
                        >
                            <Clock size={14} />
                        </button>
                        <button data-i18n="components.memory.MemopediaViewer.text032"
                            className={styles.generateButton}
                            onClick={() => {
                                setShowGenerateModal(true);
                                setGenerateKeyword("");
                                setGenerateDirections("");
                                setGenerateCategory(null);
                                setGenerateError(null);
                                setGenerateResult(null);
                            }}
                            title={uiText("components.memory.MemopediaViewer.text032")}
                        >
                            <Sparkles size={14} />
                            <span data-i18n="components.memory.MemopediaViewer.text033">{uiText("components.memory.MemopediaViewer.text033")}</span>
                        </button>
                    </div>
                </div>
                <div className={styles.treeContainer}>
                    {sortMode === 'tree' && sortedTree ? (
                        <>
                            {categories.map(cat => {
                                const pages = sortedTree[cat.key] || [];
                                if (cat.hide_when_empty && pages.length === 0) return null;
                                return (
                                    <React.Fragment key={cat.key}>
                                        <div className={styles.categoryTitle}>{cat.label} / {cat.label_en}</div>
                                        {pages.map(p => <TreeItem key={p.id} page={p} />)}
                                    </React.Fragment>
                                );
                            })}
                        </>
                    ) : (
                        <>
                            <div data-i18n="components.memory.MemopediaViewer.text034" className={styles.categoryTitle}>{uiText("components.memory.MemopediaViewer.text034")}</div>
                            {flatPages.map(p => (
                                <div
                                    key={p.id}
                                    className={`${styles.flatItem} ${selectedPageId === p.id ? styles.selected : ''}`}
                                    onClick={() => { setSelectedPageId(p.id); setShowList(false); }}
                                >
                                    <div className={styles.flatItemTitle}>{p.title}</div>
                                    <div className={styles.flatItemMeta}>
                                        {pageFreshness(p) ? new Date(pageFreshness(p) * 1000).toLocaleString(getFormatLocale(), {
                                            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                                        }) : ''}
                                    </div>
                                </div>
                            ))}
                        </>
                    )}
                </div>
            </div>

            <div className={`${styles.contentArea} ${showList ? styles.mobileHidden : ''}`}>
                <div className={styles.contentHeader}>
                    <button data-i18n="components.memory.MemopediaViewer.text035"
                        className={styles.backButton}
                        onClick={() => setShowList(true)}
                    >
                        <ChevronLeft size={20} />{uiText("components.memory.MemopediaViewer.text035")}</button>
                    {selectedPageId && !selectedPageId.startsWith('root_') && (
                        <div className={styles.headerButtons}>
                            {!isEditing && (
                                <>
                                    <button data-i18n="components.memory.MemopediaViewer.text036"
                                        className={styles.editButton}
                                        onClick={startEditing}
                                        title={uiText("components.memory.MemopediaViewer.text036")}
                                    >
                                        <Edit2 size={16} />
                                        <span data-i18n="components.memory.MemopediaViewer.text037">{uiText("components.memory.MemopediaViewer.text037")}</span>
                                    </button>
                                    <button data-i18n="components.memory.MemopediaViewer.text038"
                                        className={`${styles.historyButton} ${showHistory ? styles.active : ''}`}
                                        onClick={() => showHistory ? setShowHistory(false) : handleShowHistory()}
                                        title={uiText("components.memory.MemopediaViewer.text038")}
                                    >
                                        <History size={16} />
                                        <span data-i18n="components.memory.MemopediaViewer.text039">{uiText("components.memory.MemopediaViewer.text039")}</span>
                                    </button>
                                    <button data-i18n="components.memory.MemopediaViewer.text040"
                                        className={styles.deleteButton}
                                        onClick={() => setShowDeleteConfirm(true)}
                                        title={uiText("components.memory.MemopediaViewer.text040")}
                                    >
                                        <Trash2 size={16} />
                                    </button>
                                </>
                            )}
                        </div>
                    )}
                </div>

                {showHistory ? (
                    // History View
                    <div className={styles.historyContainer}>
                        <h3 data-i18n="components.memory.MemopediaViewer.text041" className={styles.historyTitle}>
                            <History size={20} />{uiText("components.memory.MemopediaViewer.text041")}</h3>
                        {isLoadingHistory ? (
                            <div data-i18n="components.memory.MemopediaViewer.text042" className={styles.emptyState}>{uiText("components.memory.MemopediaViewer.text042")}</div>
                        ) : editHistory.length === 0 ? (
                            <div className={styles.emptyState}>
                                <p data-i18n="components.memory.MemopediaViewer.text043">{uiText("components.memory.MemopediaViewer.text043")}</p>
                            </div>
                        ) : (
                            <div className={styles.historyList}>
                                {editHistory.map(entry => (
                                    <div
                                        key={entry.id}
                                        className={`${styles.historyEntry} ${selectedHistoryEntry?.id === entry.id ? styles.selected : ''}`}
                                        onClick={() => setSelectedHistoryEntry(
                                            selectedHistoryEntry?.id === entry.id ? null : entry
                                        )}
                                    >
                                        <div className={styles.historyEntryHeader}>
                                            <span className={styles.editType}>{getEditTypeLabel(entry.edit_type)}</span>
                                            <span className={styles.editDate}>
                                                <Clock size={12} /> {formatDate(entry.edited_at)}
                                            </span>
                                        </div>
                                        {entry.edit_source && (
                                            <div className={styles.editSource}>
                                                {uiText("components.memory.MemopediaViewer.label001")}{entry.edit_source}
                                            </div>
                                        )}
                                        {(entry.ref_start_message_id || entry.ref_end_message_id) && (
                                            <div className={styles.refRange}>
                                                <GitCommit size={12} />
                                                <span data-i18n="components.memory.MemopediaViewer.text044">{uiText("components.memory.MemopediaViewer.text044")}{entry.ref_start_message_id?.slice(0, 8) || '?'}
                                                    {' → '}
                                                    {entry.ref_end_message_id?.slice(0, 8) || '?'}
                                                </span>
                                            </div>
                                        )}
                                        {selectedHistoryEntry?.id === entry.id && (
                                            <div className={styles.diffView}>
                                                <div className={styles.diffHeader}>
                                                    <span>{uiText("components.memory.MemopediaViewer.label002")}</span>
                                                    <button data-i18n="components.memory.MemopediaViewer.text045 components.memory.MemopediaViewer.text046 components.memory.MemopediaViewer.text047 components.memory.MemopediaViewer.text048 components.memory.MemopediaViewer.text049"
                                                        className={styles.rollbackButton}
                                                        onClick={async (e) => {
                                                            e.stopPropagation();
                                                            if (!confirm(uiText("components.memory.MemopediaViewer.text045", { p1: getEditTypeLabel(entry.edit_type), p2: formatDate(entry.edited_at) }))) return;
                                                            try {
                                                                const url = `/api/people/${personaId}/memopedia/pages/${entry.page_id}/rollback/${entry.id}`;
                                                                console.log('[rollback] POST', url);
                                                                const res = await apiFetch(url, { method: 'POST' });
                                                                console.log('[rollback] response status:', res.status);
                                                                if (res.ok) {
                                                                    const data = await res.json();
                                                                    setPageContent(data.page.content);
                                                                    setShowHistory(false);
                                                                    setSelectedHistoryEntry(null);
                                                                    // Refresh tree (loadTree separates categories meta from page arrays)
                                                                    await loadTree();
                                                                } else {
                                                                    const err = await res.json();
                                                                    alert(uiText("components.memory.MemopediaViewer.text046", { p1: err.detail || uiText("common.extra007") }));
                                                                }
                                                            } catch (err) {
                                                                alert(uiText("components.memory.MemopediaViewer.text047", { p1: err }));
                                                            }
                                                        }}
                                                        title={uiText("components.memory.MemopediaViewer.text048")}
                                                    >{uiText("components.memory.MemopediaViewer.text049")}</button>
                                                </div>
                                                <pre data-i18n="components.memory.MemopediaViewer.text050" className={styles.diffContent}>{entry.diff_text || uiText("components.memory.MemopediaViewer.text050")}</pre>
                                            </div>
                                        )}
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                ) : isEditing ? (
                    // Edit Form
                    <div className={styles.editForm}>
                        <div className={styles.formGroup}>
                            <label data-i18n="components.memory.MemopediaViewer.text051">{uiText("components.memory.MemopediaViewer.text051")}</label>
                            <input
                                type="text"
                                value={editTitle}
                                onChange={e => setEditTitle(e.target.value)}
                                className={styles.formInput}
                            />
                        </div>
                        <div className={styles.formGroup}>
                            <label data-i18n="components.memory.MemopediaViewer.text052">{uiText("components.memory.MemopediaViewer.text052")}</label>
                            <input
                                type="text"
                                value={editSummary}
                                onChange={e => setEditSummary(e.target.value)}
                                className={styles.formInput}
                            />
                        </div>
                        <div className={styles.formGroup}>
                            <label data-i18n="components.memory.MemopediaViewer.text053">{uiText("components.memory.MemopediaViewer.text053")}</label>
                            <input data-i18n="components.memory.MemopediaViewer.text054"
                                type="text"
                                value={editKeywords}
                                onChange={e => setEditKeywords(e.target.value)}
                                className={styles.formInput}
                                placeholder={uiText("components.memory.MemopediaViewer.text054")}
                            />
                        </div>
                        {/* P4-c: 鮮明度 select 廃止 */}
                        <div className={styles.formGroup}>
                            <label data-i18n="components.memory.MemopediaViewer.text055">{uiText("components.memory.MemopediaViewer.text055")}</label>
                            <textarea
                                value={editContent}
                                onChange={e => setEditContent(e.target.value)}
                                className={styles.formTextarea}
                                rows={15}
                            />
                        </div>
                        <div className={styles.formActions}>
                            <button data-i18n="components.memory.MemopediaViewer.text056"
                                className={styles.cancelButton}
                                onClick={cancelEditing}
                                disabled={isSaving}
                            >
                                <X size={16} />{uiText("components.memory.MemopediaViewer.text056")}</button>
                            <button data-i18n="components.memory.MemopediaViewer.text057 components.memory.MemopediaViewer.text058"
                                className={styles.saveButton}
                                onClick={saveEdit}
                                disabled={isSaving}
                            >
                                <Save size={16} />
                                {isSaving ? uiText("components.memory.MemopediaViewer.text057") : uiText("components.memory.MemopediaViewer.text058")}
                            </button>
                        </div>
                    </div>
                ) : (
                    // Content View
                    selectedPageId ? (
                        isLoadingPage ? (
                            <div data-i18n="components.memory.MemopediaViewer.text059" className={styles.emptyState}>{uiText("components.memory.MemopediaViewer.text059")}</div>
                        ) : (
                            <div className={styles.contentBody}>
                                {selectedKeywords.length > 0 && (
                                    <div className={styles.contentKeywords}>
                                        <Tag size={14} className={styles.keywordIcon} />
                                        <div className={styles.keywords}>
                                            {selectedKeywords.map((kw, idx) => (
                                                <span key={idx} className={styles.keyword}>{kw}</span>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                {/* P4-c: 鮮明度 select の代わりに机ボタン */}
                                {selectedPageId && !selectedPageId.startsWith('root_') && (
                                    <div style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem', flexWrap: 'wrap' }}>
                                        <button data-i18n="components.memory.MemopediaViewer.text060 components.memory.MemopediaViewer.text061"
                                            className={styles.editButton}
                                            onClick={() => handleDeskToggle(true)}
                                            title={uiText("components.memory.MemopediaViewer.text060")}
                                        >{uiText("components.memory.MemopediaViewer.text061")}</button>
                                        <button data-i18n="components.memory.MemopediaViewer.text062 components.memory.MemopediaViewer.text063"
                                            className={styles.historyButton}
                                            onClick={() => handleDeskToggle(false)}
                                            title={uiText("components.memory.MemopediaViewer.text062")}
                                        >{uiText("components.memory.MemopediaViewer.text063")}</button>
                                        <small data-i18n="components.memory.MemopediaViewer.text064" style={{ color: '#888' }}>{uiText("components.memory.MemopediaViewer.text064")}</small>
                                    </div>
                                )}
                                {selectedPageId && !selectedPageId.startsWith('root_') && (
                                    <>
                                        <div style={{ marginBottom: '0.5rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                            <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                                <input
                                                    type="checkbox"
                                                    checked={selectedIsImportant}
                                                    onChange={e => handleImportantToggle(selectedPageId, e.target.checked)}
                                                    style={{ cursor: 'pointer' }}
                                                />
                                                <Star size={14} />
                                                <span data-i18n="components.memory.MemopediaViewer.text065" style={{ fontSize: '0.9em', fontWeight: 'bold', color: '#666' }}>{uiText("components.memory.MemopediaViewer.text065")}</span>
                                            </label>
                                            <small data-i18n="components.memory.MemopediaViewer.text066" style={{ color: '#888' }}>{uiText("components.memory.MemopediaViewer.text066")}</small>
                                        </div>
                                        <div style={{ marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                            <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                                <input
                                                    type="checkbox"
                                                    checked={selectedIsTrunk}
                                                    onChange={e => handleTrunkToggle(selectedPageId, e.target.checked)}
                                                    style={{ cursor: 'pointer' }}
                                                />
                                                <FolderTree size={14} />
                                                <span data-i18n="components.memory.MemopediaViewer.text067" style={{ fontSize: '0.9em', fontWeight: 'bold', color: '#666' }}>{uiText("components.memory.MemopediaViewer.text067")}</span>
                                            </label>
                                            <small data-i18n="components.memory.MemopediaViewer.text068" style={{ color: '#888' }}>{uiText("components.memory.MemopediaViewer.text068")}</small>
                                        </div>
                                    </>
                                )}
                                <div className={styles.markdown}>
                                    <ReactMarkdown
                                        urlTransform={(url) => url.startsWith('saiverse://') ? url : defaultUrlTransform(url)}
                                        components={{
                                            a: ({ href, children }) => <SaiverseLink href={href} personaId={personaId}>{children}</SaiverseLink>,
                                        }}
                                    >{pageContent}</ReactMarkdown>
                                </div>
                                {pageFragments.length > 0 && (
                                    <div className={styles.fragmentsSection}>
                                        <h3 className={styles.fragmentsTitle}>{uiText("components.memory.MemopediaViewer.label003")}{pageFragments.length})</h3>
                                        {(() => {
                                            const grouped: Record<string, MemopediaFragment[]> = {};
                                            for (const f of pageFragments) {
                                                const key = f.source_date || "unknown";
                                                if (!grouped[key]) grouped[key] = [];
                                                grouped[key].push(f);
                                            }
                                            return Object.entries(grouped).map(([date, frags]) => (
                                                <div key={date} className={styles.fragmentDateGroup}>
                                                    <div className={styles.fragmentDate}>{date}</div>
                                                    <ul className={styles.fragmentList}>
                                                        {frags.map(f => (
                                                            <li key={f.id} className={styles.fragmentItem}>
                                                                {f.content}
                                                            </li>
                                                        ))}
                                                    </ul>
                                                </div>
                                            ));
                                        })()}
                                    </div>
                                )}
                            </div>
                        )
                    ) : (
                        <div className={styles.emptyState}>
                            <div style={{ textAlign: 'center' }}>
                                <Book size={48} style={{ marginBottom: '1rem', opacity: 0.5 }} />
                                <p data-i18n="components.memory.MemopediaViewer.text069">{uiText("components.memory.MemopediaViewer.text069")}</p>
                            </div>
                        </div>
                    )
                )}

                {/* Delete Confirmation Dialog */}
                {showDeleteConfirm && (
                    <div className={styles.overlay}>
                        <div className={styles.confirmDialog}>
                            <h3 data-i18n="components.memory.MemopediaViewer.text070">{uiText("components.memory.MemopediaViewer.text070")}</h3>
                            <p data-i18n="components.memory.MemopediaViewer.text071">{uiText("components.memory.MemopediaViewer.text071")}</p>
                            <div className={styles.confirmActions}>
                                <button data-i18n="components.memory.MemopediaViewer.text072"
                                    className={styles.cancelButton}
                                    onClick={() => setShowDeleteConfirm(false)}
                                    disabled={isDeleting}
                                >{uiText("components.memory.MemopediaViewer.text072")}</button>
                                <button data-i18n="components.memory.MemopediaViewer.text073 components.memory.MemopediaViewer.text074"
                                    className={styles.confirmDeleteButton}
                                    onClick={deletePage}
                                    disabled={isDeleting}
                                >
                                    {isDeleting ? uiText("components.memory.MemopediaViewer.text073") : uiText("components.memory.MemopediaViewer.text074")}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Create Page Modal */}
                {showCreateModal && (
                    <div className={styles.overlay}>
                        <div className={styles.createModal}>
                            <h3 data-i18n="components.memory.MemopediaViewer.text075">{uiText("components.memory.MemopediaViewer.text075")}</h3>
                            <div className={styles.formGroup}>
                                <label data-i18n="components.memory.MemopediaViewer.text076">{uiText("components.memory.MemopediaViewer.text076")}</label>
                                <input data-i18n="components.memory.MemopediaViewer.text077"
                                    type="text"
                                    value={createTitle}
                                    onChange={e => setCreateTitle(e.target.value)}
                                    className={styles.formInput}
                                    placeholder={uiText("components.memory.MemopediaViewer.text077")}
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label data-i18n="components.memory.MemopediaViewer.text078">{uiText("components.memory.MemopediaViewer.text078")}</label>
                                <input data-i18n="components.memory.MemopediaViewer.text079"
                                    type="text"
                                    value={createSummary}
                                    onChange={e => setCreateSummary(e.target.value)}
                                    className={styles.formInput}
                                    placeholder={uiText("components.memory.MemopediaViewer.text079")}
                                />
                            </div>
                            <div className={styles.formGroup}>
                                <label data-i18n="components.memory.MemopediaViewer.text080">{uiText("components.memory.MemopediaViewer.text080")}</label>
                                <input data-i18n="components.memory.MemopediaViewer.text081"
                                    type="text"
                                    value={createKeywords}
                                    onChange={e => setCreateKeywords(e.target.value)}
                                    className={styles.formInput}
                                    placeholder={uiText("components.memory.MemopediaViewer.text081")}
                                />
                            </div>
                            {/* P4-c: 鮮明度 select 廃止 */}
                            <div className={styles.formGroup}>
                                <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                    <input
                                        type="checkbox"
                                        checked={createIsTrunk}
                                        onChange={e => setCreateIsTrunk(e.target.checked)}
                                        style={{ cursor: 'pointer' }}
                                    />
                                    <FolderTree size={14} />
                                    <span data-i18n="components.memory.MemopediaViewer.text082">{uiText("components.memory.MemopediaViewer.text082")}</span>
                                </label>
                                <small data-i18n="components.memory.MemopediaViewer.text083" style={{ color: '#888', display: 'block', marginTop: '4px' }}>{uiText("components.memory.MemopediaViewer.text083")}</small>
                            </div>
                            <div className={styles.formGroup}>
                                <label data-i18n="components.memory.MemopediaViewer.text084">{uiText("components.memory.MemopediaViewer.text084")}</label>
                                <textarea data-i18n="components.memory.MemopediaViewer.text085"
                                    value={createContent}
                                    onChange={e => setCreateContent(e.target.value)}
                                    className={styles.formTextarea}
                                    rows={8}
                                    placeholder={uiText("components.memory.MemopediaViewer.text085")}
                                />
                            </div>
                            <div className={styles.formActions}>
                                <button data-i18n="components.memory.MemopediaViewer.text086"
                                    className={styles.cancelButton}
                                    onClick={() => setShowCreateModal(false)}
                                    disabled={isCreating}
                                >
                                    <X size={16} />{uiText("components.memory.MemopediaViewer.text086")}</button>
                                <button data-i18n="components.memory.MemopediaViewer.text087 components.memory.MemopediaViewer.text088"
                                    className={styles.saveButton}
                                    onClick={createPage}
                                    disabled={isCreating || !createTitle.trim()}
                                >
                                    <Plus size={16} />
                                    {isCreating ? uiText("components.memory.MemopediaViewer.text087") : uiText("components.memory.MemopediaViewer.text088")}
                                </button>
                            </div>
                        </div>
                    </div>
                )}

                {/* Generate Page Modal */}
                {showGenerateModal && (
                    <div className={styles.overlay}>
                        <div className={styles.createModal}>
                            <h3 data-i18n="components.memory.MemopediaViewer.text089"><Sparkles size={20} />{uiText("components.memory.MemopediaViewer.text089")}</h3>
                            {!isGenerating && !generateResult ? (
                                <>
                                    <div className={styles.formGroup}>
                                        <label data-i18n="components.memory.MemopediaViewer.text090">{uiText("components.memory.MemopediaViewer.text090")}</label>
                                        <input data-i18n="components.memory.MemopediaViewer.text091"
                                            type="text"
                                            value={generateKeyword}
                                            onChange={e => setGenerateKeyword(e.target.value)}
                                            className={styles.formInput}
                                            placeholder={uiText("components.memory.MemopediaViewer.text091")}
                                        />
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label data-i18n="components.memory.MemopediaViewer.text092">{uiText("components.memory.MemopediaViewer.text092")}</label>
                                        <textarea data-i18n="components.memory.MemopediaViewer.text093"
                                            value={generateDirections}
                                            onChange={e => setGenerateDirections(e.target.value)}
                                            className={styles.formTextarea}
                                            rows={3}
                                            placeholder={uiText("components.memory.MemopediaViewer.text093")}
                                        />
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label data-i18n="components.memory.MemopediaViewer.text094">{uiText("components.memory.MemopediaViewer.text094")}</label>
                                        <select
                                            value={generateCategory || ""}
                                            onChange={e => setGenerateCategory(e.target.value || null)}
                                            className={styles.formInput}
                                        >
                                            <option data-i18n="components.memory.MemopediaViewer.text095" value="">{uiText("components.memory.MemopediaViewer.text095")}</option>
                                            {categories.filter(c => c.can_generate).map(c => (
                                                <option key={c.key} value={c.key}>{c.label_en}</option>
                                            ))}
                                        </select>
                                    </div>
                                    {generateError && (
                                        <div className={styles.errorText}>{generateError}</div>
                                    )}
                                    <div className={styles.formActions}>
                                        <button data-i18n="components.memory.MemopediaViewer.text096"
                                            className={styles.cancelButton}
                                            onClick={() => setShowGenerateModal(false)}
                                        >
                                            <X size={16} />{uiText("components.memory.MemopediaViewer.text096")}</button>
                                        <button data-i18n="components.memory.MemopediaViewer.text097"
                                            className={styles.saveButton}
                                            onClick={startGeneration}
                                            disabled={!generateKeyword.trim()}
                                        >
                                            <Sparkles size={16} />{uiText("components.memory.MemopediaViewer.text097")}</button>
                                    </div>
                                </>
                            ) : isGenerating ? (
                                <div className={styles.generatingState}>
                                    <div className={styles.spinner} />
                                    <p>{generateStatus}</p>
                                    {generateProgress && (
                                        <div className={styles.progressBar}>
                                            <div
                                                className={styles.progressFill}
                                                style={{ width: `${(generateProgress.current / generateProgress.total) * 100}%` }}
                                            />
                                        </div>
                                    )}
                                </div>
                            ) : generateResult ? (
                                <div className={styles.resultState}>
                                    <p data-i18n="components.memory.MemopediaViewer.text098 components.memory.MemopediaViewer.text099 components.memory.MemopediaViewer.text100 components.memory.MemopediaViewer.text101">{uiText("components.memory.MemopediaViewer.text098")}{generateResult.action === 'created' ? uiText("components.memory.MemopediaViewer.text099") : uiText("components.memory.MemopediaViewer.text100")}{uiText("components.memory.MemopediaViewer.text101")}</p>
                                    <p><strong>{generateResult.title}</strong></p>
                                    <div className={styles.formActions}>
                                        <button data-i18n="components.memory.MemopediaViewer.text102"
                                            className={styles.saveButton}
                                            onClick={() => {
                                                setShowGenerateModal(false);
                                                if (generateResult.page_id) {
                                                    setSelectedPageId(generateResult.page_id);
                                                    setShowList(false);
                                                }
                                                loadTree();
                                            }}
                                        >{uiText("components.memory.MemopediaViewer.text102")}</button>
                                    </div>
                                </div>
                            ) : null}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
