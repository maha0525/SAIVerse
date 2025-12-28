import React, { useState, useRef } from 'react';
import { Upload, Loader2, Image as ImageIcon, FileText, X } from 'lucide-react';

interface FileUploadProps {
    value: string | null;
    onChange: (relativePath: string, type: 'image' | 'document') => void;
    onClear?: () => void;
    acceptImages?: boolean;
    acceptDocuments?: boolean;
    placeholder?: string;
    className?: string;
}

export default function FileUpload({
    value,
    onChange,
    onClear,
    acceptImages = true,
    acceptDocuments = true,
    placeholder = "Select File",
    className = "",
}: FileUploadProps) {
    const [isUploading, setIsUploading] = useState(false);
    const [uploadedType, setUploadedType] = useState<'image' | 'document' | null>(null);
    const [previewUrl, setPreviewUrl] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // Build accept string
    const acceptTypes: string[] = [];
    if (acceptImages) acceptTypes.push("image/*");
    if (acceptDocuments) acceptTypes.push("text/*", ".txt", ".md", ".json", ".xml");
    const acceptString = acceptTypes.join(",");

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
            const res = await fetch('/api/media/upload-file', {
                method: 'POST',
                body: formData
            });
            if (res.ok) {
                const data = await res.json();
                const fileType = data.type as 'image' | 'document';
                setUploadedType(fileType);

                // Set preview for images
                if (fileType === 'image') {
                    setPreviewUrl(data.url);
                } else {
                    setPreviewUrl(null);
                }

                onChange(data.relative_path, fileType);
            } else {
                console.error("Upload failed");
                alert("Upload failed. Please try again.");
            }
        } catch (error) {
            console.error(error);
            alert("Network error.");
        } finally {
            setIsUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = "";
        }
    };

    const handleClear = (e: React.MouseEvent) => {
        e.stopPropagation();
        setPreviewUrl(null);
        setUploadedType(null);
        if (onClear) onClear();
    };

    // Determine if value looks like an image path
    const isImagePath = value && (
        value.startsWith('image/') ||
        value.match(/\.(png|jpg|jpeg|gif|webp|svg)$/i)
    );

    return (
        <div
            className={className}
            style={{
                position: 'relative',
                cursor: 'pointer',
                borderRadius: '8px',
                overflow: 'hidden',
                border: '2px dashed #4b5563',
                backgroundColor: '#1f2937',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: value ? '0.5rem' : '1rem',
                minHeight: '80px',
                transition: 'all 0.2s',
            }}
            onClick={handleClick}
            onMouseEnter={e => { e.currentTarget.style.borderColor = '#6366f1'; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = '#4b5563'; }}
        >
            <input
                type="file"
                ref={fileInputRef}
                onChange={handleFileChange}
                accept={acceptString}
                style={{ display: 'none' }}
            />

            {value ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', width: '100%' }}>
                    {isImagePath || previewUrl ? (
                        <img
                            src={previewUrl || `/api/media/images/${value.split('/').pop()}`}
                            alt="Preview"
                            style={{
                                width: '60px',
                                height: '60px',
                                objectFit: 'cover',
                                borderRadius: '4px',
                                opacity: isUploading ? 0.5 : 1
                            }}
                            onError={(e) => { e.currentTarget.style.display = 'none'; }}
                        />
                    ) : (
                        <div style={{
                            width: '60px',
                            height: '60px',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center',
                            backgroundColor: '#374151',
                            borderRadius: '4px',
                        }}>
                            <FileText size={24} color="#9ca3af" />
                        </div>
                    )}
                    <div style={{ flex: 1, overflow: 'hidden' }}>
                        <div style={{
                            fontSize: '0.85rem',
                            color: '#e5e7eb',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis'
                        }}>
                            {value.split('/').pop()}
                        </div>
                        <div style={{ fontSize: '0.75rem', color: '#9ca3af' }}>
                            Click to replace
                        </div>
                    </div>
                    {onClear && (
                        <button
                            onClick={handleClear}
                            style={{
                                background: 'rgba(239, 68, 68, 0.2)',
                                border: 'none',
                                borderRadius: '4px',
                                padding: '4px',
                                cursor: 'pointer',
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                            }}
                        >
                            <X size={16} color="#ef4444" />
                        </button>
                    )}
                </div>
            ) : (
                <div style={{ color: '#9ca3af', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px' }}>
                    {acceptImages && acceptDocuments ? (
                        <Upload size={24} />
                    ) : acceptImages ? (
                        <ImageIcon size={24} />
                    ) : (
                        <FileText size={24} />
                    )}
                    <span style={{ fontSize: '12px' }}>{placeholder}</span>
                    <span style={{ fontSize: '10px', color: '#6b7280' }}>
                        {acceptImages && acceptDocuments ? 'Image or Text' : acceptImages ? 'Image' : 'Text'}
                    </span>
                </div>
            )}

            {isUploading && (
                <div style={{
                    position: 'absolute',
                    top: 0, left: 0, right: 0, bottom: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    backgroundColor: 'rgba(0,0,0,0.5)',
                }}>
                    <Loader2 className="spin" size={24} color="white" />
                </div>
            )}
        </div>
    );
}
