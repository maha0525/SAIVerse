import React, { useState, useRef } from 'react';
import { Upload, Loader2, Image as ImageIcon } from 'lucide-react';

interface ImageUploadProps {
    value: string | null;
    onChange: (url: string) => void;
    placeholder?: string;
    className?: string;
    width?: number;
    height?: number;
    circle?: boolean;
}

export default function ImageUpload({
    value,
    onChange,
    placeholder = "Select Image",
    className = "",
    width = 96,
    height = 96,
    circle = false
}: ImageUploadProps) {
    const [isUploading, setIsUploading] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const handleClick = () => {
        fileInputRef.current?.click();
    };

    const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setIsUploading(true);
        const formData = new FormData();
        formData.append('file', file);

        try {
            const res = await fetch('/api/media/upload', {
                method: 'POST',
                body: formData
            });
            if (res.ok) {
                const data = await res.json();
                onChange(data.url);
            } else {
                console.error("Upload failed");
                alert("Upload failed. Please try again.");
            }
        } catch (error) {
            console.error(error);
            alert("Network error.");
        } finally {
            setIsUploading(false);
            // Reset input so same file can be selected again if needed
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const isDark = typeof document !== 'undefined' && document.documentElement.dataset.theme === 'dark';
    const bgColor = isDark ? '#1f2937' : '#f1f5f9';
    const borderColor = isDark ? '#4b5563' : '#cbd5e1';
    const placeholderColor = isDark ? '#9ca3af' : '#64748b';

    return (
        <div
            className={className}
            style={{
                width: width,
                height: height,
                position: 'relative',
                cursor: 'pointer',
                borderRadius: circle ? '50%' : '8px',
                overflow: 'hidden',
                border: `2px dashed ${borderColor}`,
                backgroundColor: bgColor,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                transition: 'all 0.2s',
            }}
            onClick={handleClick}
            onMouseEnter={e => { e.currentTarget.style.borderColor = '#6366f1'; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = borderColor; }}
        >
            <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                accept="image/*"
                style={{ display: 'none' }}
            />

            {value ? (
                <img
                    src={value}
                    alt="Preview"
                    style={{
                        width: '100%',
                        height: '100%',
                        objectFit: 'cover',
                        opacity: isUploading ? 0.5 : 1
                    }}
                    onError={(e) => { e.currentTarget.style.display = 'none'; }}
                />
            ) : (
                <div style={{ color: placeholderColor, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px' }}>
                    <ImageIcon size={20} />
                    <span style={{ fontSize: '10px' }}>{placeholder}</span>
                </div>
            )}

            {/* Hover Overlay or Loading Overlay */}
            {(isUploading || !value) && (
                <div style={{
                    position: 'absolute',
                    top: 0, left: 0, right: 0, bottom: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    backgroundColor: isUploading ? 'rgba(0,0,0,0.5)' : 'transparent',
                }}>
                    {isUploading ? <Loader2 className="spin" size={24} color="white" /> : null}
                </div>
            )}

            {/* Edit Icon on Hover (CSS based or simple absolute) */}
            {value && !isUploading && (
                <div style={{
                    position: 'absolute',
                    bottom: 0,
                    left: 0,
                    right: 0,
                    background: 'rgba(0,0,0,0.6)',
                    color: 'white',
                    fontSize: '10px',
                    textAlign: 'center',
                    padding: '2px 0'
                }}>
                    Edit
                </div>
            )}
        </div>
    );
}
