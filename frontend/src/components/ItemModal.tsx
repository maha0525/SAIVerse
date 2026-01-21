import { useState, useEffect } from 'react';
import { X, FileText, Code2 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import styles from './ItemModal.module.css';
import ModalOverlay from './common/ModalOverlay';

interface Item {
    id: string;
    name: string;
    description?: string;
    type: string;
}

interface ItemModalProps {
    isOpen: boolean;
    onClose: () => void;
    item: Item | null;
}

export default function ItemModal({ isOpen, onClose, item }: ItemModalProps) {
    const [content, setContent] = useState<string | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [isMarkdown, setIsMarkdown] = useState(true);

    useEffect(() => {
        if (isOpen && item && item.type === 'document') {
            setIsLoading(true);
            setError(null);
            fetch(`/api/info/item/${item.id}`)
                .then(async res => {
                    if (!res.ok) throw new Error("Failed to load content");
                    const data = await res.json();
                    setContent(data.content);
                })
                .catch(err => {
                    console.error(err);
                    setError("コンテンツの読み込みに失敗しました");
                })
                .finally(() => setIsLoading(false));
        } else {
            setContent(null);
            setError(null);
        }
    }, [isOpen, item]);

    if (!isOpen || !item) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2>{item.name}</h2>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={24} />
                    </button>
                </div>

                <div className={styles.meta}>
                    <span className={styles.badge}>{item.type}</span>
                    <span className={styles.id}>ID: <code>{item.id}</code></span>
                </div>

                {item.description && (
                    <div className={styles.description}>
                        {item.description}
                    </div>
                )}

                <div className={styles.body}>
                    {item.type === 'picture' ? (
                        <div className={styles.imageContainer}>
                            <img
                                src={`/api/info/item/${item.id}`}
                                alt={item.name}
                                className={styles.image}
                            />
                        </div>
                    ) : item.type === 'document' ? (
                        <div className={styles.documentContainer}>
                            <div className={styles.documentHeader}>
                                <button
                                    className={`${styles.toggleBtn} ${isMarkdown ? styles.active : ''}`}
                                    onClick={() => setIsMarkdown(true)}
                                    title="マークダウン表示"
                                >
                                    <FileText size={16} />
                                    <span>Markdown</span>
                                </button>
                                <button
                                    className={`${styles.toggleBtn} ${!isMarkdown ? styles.active : ''}`}
                                    onClick={() => setIsMarkdown(false)}
                                    title="プレーンテキスト表示"
                                >
                                    <Code2 size={16} />
                                    <span>Plain</span>
                                </button>
                            </div>
                            {isLoading && <div className={styles.loading}>読み込み中...</div>}
                            {error && <div className={styles.error}>{error}</div>}
                            {content && (
                                isMarkdown ? (
                                    <div className={styles.markdownContent}>
                                        <ReactMarkdown>{content}</ReactMarkdown>
                                    </div>
                                ) : (
                                    <pre className={styles.documentContent}>
                                        {content}
                                    </pre>
                                )
                            )}
                        </div>
                    ) : (
                        <div className={styles.unsupported}>
                            このアイテムタイプ ({item.type}) の表示はサポートされていません。
                        </div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
