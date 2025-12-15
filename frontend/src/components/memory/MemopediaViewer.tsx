import React, { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Book, ChevronRight, ChevronDown, ChevronLeft } from 'lucide-react';
import styles from './MemopediaViewer.module.css';

interface MemopediaPage {
    id: string;
    title: string;
    summary: string;
    children: MemopediaPage[];
}

interface TreeStructure {
    people: MemopediaPage[];
    events: MemopediaPage[];
    plans: MemopediaPage[];
}

interface MemopediaViewerProps {
    personaId: string;
}

export default function MemopediaViewer({ personaId }: MemopediaViewerProps) {
    const [tree, setTree] = useState<TreeStructure | null>(null);
    const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
    const [pageContent, setPageContent] = useState<string>("");
    const [isLoadingPage, setIsLoadingPage] = useState(false);

    useEffect(() => {
        loadTree();
    }, [personaId]);

    useEffect(() => {
        if (selectedPageId) {
            loadPage(selectedPageId);
        } else {
            setPageContent("");
        }
    }, [selectedPageId]);

    const loadTree = async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/tree`);
            if (res.ok) {
                const data = await res.json();
                setTree(data);
            }
        } catch (error) {
            console.error("Failed to load memopedia tree", error);
        }
    };

    const loadPage = async (pageId: string) => {
        setIsLoadingPage(true);
        try {
            const res = await fetch(`/api/people/${personaId}/memopedia/pages/${pageId}`);
            if (res.ok) {
                const data = await res.json();
                setPageContent(data.content);
            }
        } catch (error) {
            console.error("Failed to load page content", error);
            setPageContent("*Failed to load content*");
        } finally {
            setIsLoadingPage(false);
        }
    };

    const [showList, setShowList] = useState(true);



    const TreeItem = ({ page }: { page: MemopediaPage }) => {
        const [isOpen, setIsOpen] = useState(false);
        const hasChildren = page.children && page.children.length > 0;

        return (
            <div>
                <div
                    className={`${styles.pageItem} ${selectedPageId === page.id ? styles.active : ''}`}
                    onClick={() => {
                        setSelectedPageId(page.id);
                        if (hasChildren) setIsOpen(!isOpen);
                        if (!hasChildren) setShowList(false); // Mobile: go to content if leaf
                    }}
                >
                    {hasChildren && (
                        isOpen ? <ChevronDown size={12} style={{ marginRight: 4 }} /> : <ChevronRight size={12} style={{ marginRight: 4 }} />
                    )}
                    {!hasChildren && <span style={{ display: 'inline-block', width: 16 }} />}
                    {page.title}
                </div>
                {isOpen && hasChildren && (
                    <div className={styles.pageChildren}>
                        {page.children.map(child => <TreeItem key={child.id} page={child} />)}
                    </div>
                )}
            </div>
        );
    };

    if (!tree) return <div className={styles.emptyState}>Loading knowledge base...</div>;

    return (
        <div className={styles.container}>
            <div className={`${styles.sidebar} ${!showList ? styles.mobileHidden : ''}`}>
                <div className={styles.sidebarHeader}>Knowledge Tree</div>
                <div className={styles.treeContainer}>
                    {/* ... */}
                    <div className={styles.categoryTitle}>People</div>
                    {tree.people.map(p => <TreeItem key={p.id} page={p} />)}

                    <div className={styles.categoryTitle}>Events</div>
                    {tree.events.map(p => <TreeItem key={p.id} page={p} />)}

                    <div className={styles.categoryTitle}>Plans</div>
                    {tree.plans.map(p => <TreeItem key={p.id} page={p} />)}
                </div>
                {/* ... closing sidebar ... */}
            </div>

            <div className={`${styles.contentArea} ${showList ? styles.mobileHidden : ''}`}>
                <button
                    className={styles.backButton}
                    onClick={() => setShowList(true)}
                >
                    <ChevronLeft size={20} /> Back
                </button>
                {selectedPageId ? (
                    isLoadingPage ? (
                        <div className={styles.emptyState}>Loading...</div>
                    ) : (
                        <div className={styles.markdown}>
                            <ReactMarkdown>{pageContent}</ReactMarkdown>
                        </div>
                    )
                ) : (
                    <div className={styles.emptyState}>
                        <div style={{ textAlign: 'center' }}>
                            <Book size={48} style={{ marginBottom: '1rem', opacity: 0.5 }} />
                            <p>Select a page to view contents</p>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
