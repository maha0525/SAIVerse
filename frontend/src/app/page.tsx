"use client";

import { useState, useEffect, useRef } from 'react';
import { Send, Upload, MapPin, User, Settings } from 'lucide-react';
import styles from './page.module.css';

interface Message {
    role: 'user' | 'assistant';
    content: string;
}

export default function ChatPage() {
    const [messages, setMessages] = useState<Message[]>([]);
    const [inputValue, setInputValue] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Scroll to bottom
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    // Load history on mount
    useEffect(() => {
        const fetchHistory = async () => {
            try {
                const res = await fetch('http://localhost:7860/api/chat/history');
                if (res.ok) {
                    const data = await res.json();
                    setMessages(data.history || []);
                }
            } catch (err) {
                console.error("Failed to load history", err);
            }
        };
        fetchHistory();
    }, []);

    const handleSendMessage = async () => {
        if (!inputValue.trim()) return;

        const newMessage: Message = { role: 'user', content: inputValue };
        setMessages(prev => [...prev, newMessage]);
        setInputValue('');
        setIsLoading(true);

        try {
            // Optimistic update done, now send to API
            // Note: Actual implementation should handle streaming if possible, 
            // but for V1 we might just hit an endpoint that returns the response.
            // Or use a stream reader.

            const res = await fetch('http://localhost:7860/api/chat/send', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ message: newMessage.content })
            });

            if (!res.ok) {
                throw new Error('Failed to send message');
            }

            // If streaming is not implemented in V1 API yet, we might poll or wait for response.
            // Assuming naive response for PoC
            // const data = await res.json();
            // setMessages(prev => [...prev, { role: 'assistant', content: data.response }]);

        } catch (err) {
            console.error(err);
        } finally {
            setIsLoading(false);
        }
    };

    return (
        <div className={styles.container}>
            <header className={styles.header}>
                <div className={styles.headerLeft}>
                    <h1>SAIVerse City</h1>
                    <span className={styles.status}>Online</span>
                </div>
                <div className={styles.headerRight}>
                    <button className={styles.iconBtn}><MapPin size={20} /></button>
                    <button className={styles.iconBtn}><User size={20} /></button>
                </div>
            </header>

            <main className={styles.chatArea}>
                {messages.map((msg, idx) => (
                    <div key={idx} className={`${styles.message} ${msg.role === 'user' ? styles.user : styles.assistant}`}>
                        <div className={styles.bubble}>
                            {msg.content}
                        </div>
                    </div>
                ))}
                {isLoading && <div className={styles.loading}>Thinking...</div>}
                <div ref={messagesEndRef} />
            </main>

            <footer className={styles.inputArea}>
                <button className={styles.attachBtn}><Upload size={20} /></button>
                <div className={styles.inputWrapper}>
                    <textarea
                        value={inputValue}
                        onChange={(e) => setInputValue(e.target.value)}
                        onKeyDown={(e) => {
                            if (e.key === 'Enter' && !e.shiftKey) {
                                e.preventDefault();
                                handleSendMessage();
                            }
                        }}
                        placeholder="Message..."
                        rows={1}
                    />
                </div>
                <button className={styles.sendBtn} onClick={handleSendMessage} disabled={isLoading || !inputValue.trim()}>
                    <Send size={20} />
                </button>
            </footer>
        </div>
    );
}
