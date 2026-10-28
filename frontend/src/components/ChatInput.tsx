"use client";
// Chat input textarea, split out of app/page.tsx.
// inputValue を page.tsx の state に置くと、一打ごとに page 全体が再レンダリングされ、
// 表示中の全メッセージの Markdown 解析まで走り直す (iPhone で入力が追いつかない原因)。
// 入力値はこのコンポーネントの中だけで持ち、親には ChatInputHandle と onEmptyChange で渡す。
// 経緯: docs/issues/chat_input_keystroke_rerenders_whole_page.md
import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState, KeyboardEvent } from 'react';

export interface ChatInputHandle {
    getValue(): string;
    clear(): void;
    restoreText(text: string): void;
}

interface ChatInputProps {
    disabled: boolean;
    placeholder: string;
    onSend: () => void;
    onEmptyChange: (isEmpty: boolean) => void;
}

const ChatInput = forwardRef<ChatInputHandle, ChatInputProps>(function ChatInput(
    { disabled, placeholder, onSend, onEmptyChange },
    ref,
) {
    const [value, setValue] = useState('');
    // getValue / restoreText が再レンダリングを待たずに最新値を読めるよう、state と同時に更新する。
    const valueRef = useRef('');
    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const lastEmptyRef = useRef(true);

    const updateValue = useCallback((next: string) => {
        valueRef.current = next;
        setValue(next);
    }, []);

    useImperativeHandle(ref, () => ({
        getValue: () => valueRef.current,
        clear: () => updateValue(''),
        restoreText: (text: string) => {
            const prev = valueRef.current;
            updateValue(prev.trim() ? `${prev}\n${text}` : text);
        },
    }), [updateValue]);

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

    // Adjust height when input value changes (typing, clear, restoreText)
    useEffect(() => {
        adjustTextareaHeight();
    }, [value, adjustTextareaHeight]);

    // Notify the parent only when empty <-> non-empty flips, so typing does not
    // touch parent state on every keystroke.
    useEffect(() => {
        const isEmpty = !value.trim();
        if (isEmpty === lastEmptyRef.current) return;
        lastEmptyRef.current = isEmpty;
        onEmptyChange(isEmpty);
    }, [value, onEmptyChange]);

    const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
        // Ctrl+Enter (or Cmd+Enter) sends the message on any device.
        // Regular Enter always inserts a newline.
        // No isMobile gate needed: actual mobile devices have no Ctrl key,
        // while touch-screen laptops do need Ctrl+Enter to work.
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            onSend();
        }
    };

    return (
        <textarea data-i18n="app.page.text083 app.page.text084 app.page.text097"
            ref={textareaRef}
            value={value}
            onChange={(e) => updateValue(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            placeholder={placeholder}
            rows={1}
        />
    );
});

export default ChatInput;
