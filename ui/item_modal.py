"""Item modal display with tooltips and click events."""

ITEM_MODAL_CSS = """
/* Item link styling */
.item-link {
    cursor: pointer;
    color: #2563eb;
    text-decoration: underline;
    text-decoration-style: dotted;
    transition: color 0.2s;
}

.item-link:hover {
    color: #1d4ed8;
    text-decoration-style: solid;
}

/* Modal overlay */
#item-modal-overlay {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0, 0, 0, 0.5);
    z-index: 9998;
    justify-content: center;
    align-items: center;
}

#item-modal-overlay.show {
    display: flex;
}

/* Modal content */
#item-modal-content {
    background: white;
    border-radius: 8px;
    padding: 24px;
    max-width: 800px;
    max-height: 80vh;
    width: 90%;
    overflow-y: auto;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    position: relative;
    z-index: 9999;
}

/* Dark mode support */
.dark #item-modal-content {
    background: #1f2937;
    color: #f3f4f6;
}

/* Modal header */
#item-modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 16px;
    border-bottom: 1px solid #e5e7eb;
    padding-bottom: 12px;
}

.dark #item-modal-header {
    border-bottom-color: #374151;
}

#item-modal-title {
    font-size: 1.5rem;
    font-weight: bold;
    margin: 0;
}

#item-modal-close {
    background: none;
    border: none;
    font-size: 1.5rem;
    cursor: pointer;
    padding: 4px 8px;
    line-height: 1;
    color: #6b7280;
}

#item-modal-close:hover {
    color: #111827;
}

.dark #item-modal-close:hover {
    color: #f3f4f6;
}

/* Modal body */
#item-modal-body {
    line-height: 1.6;
}

#item-modal-body pre {
    background: #f3f4f6;
    padding: 12px;
    border-radius: 4px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-wrap: break-word;
}

.dark #item-modal-body pre {
    background: #111827;
}

#item-modal-body img {
    max-width: 100%;
    height: auto;
    border-radius: 4px;
}

.item-meta {
    font-size: 0.875rem;
    color: #6b7280;
    margin-bottom: 12px;
}

.dark .item-meta {
    color: #9ca3af;
}

.item-meta code {
    background: #f3f4f6;
    padding: 2px 6px;
    border-radius: 3px;
    font-family: monospace;
}

.dark .item-meta code {
    background: #374151;
}
"""

ITEM_MODAL_JS = """
() => {
    // ===== SCRIPT LOADED CHECK =====
    console.log('[ItemModal] ===== SCRIPT FILE LOADED AND PARSING STARTED =====');
    console.log('[ItemModal] Current time:', new Date().toISOString());
    console.log('[ItemModal] Document readyState:', document.readyState);
    console.log('[ItemModal] Document body exists:', !!document.body);

    function initItemModal() {
        console.log('[ItemModal] initItemModal called');

        // Create modal elements if they don't exist
        if (!document.getElementById('item-modal-overlay')) {
            console.log('[ItemModal] Creating modal overlay');
            const overlay = document.createElement('div');
            overlay.id = 'item-modal-overlay';
            overlay.innerHTML = `
                <div id="item-modal-content">
                    <div id="item-modal-header">
                        <h2 id="item-modal-title"></h2>
                        <button id="item-modal-close">&times;</button>
                    </div>
                    <div id="item-modal-body"></div>
                </div>
            `;
            document.body.appendChild(overlay);
            console.log('[ItemModal] Modal overlay created');

            // Close modal on overlay click
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) {
                    closeItemModal();
                }
            });

            // Close button
            document.getElementById('item-modal-close').addEventListener('click', closeItemModal);
        } else {
            console.log('[ItemModal] Modal overlay already exists');
        }

        // Add click event listeners to all item links
        const itemLinks = document.querySelectorAll('.item-link');
        console.log('[ItemModal] Found', itemLinks.length, 'item links');

        itemLinks.forEach(function(link, index) {
            console.log('[ItemModal] Processing link', index, ':', link.getAttribute('data-item-name'), 'type:', link.getAttribute('data-item-type'));
            link.removeEventListener('click', handleItemClick); // Remove existing listeners
            link.addEventListener('click', handleItemClick);
        });
    }

    function handleItemClick(e) {
        console.log('[ItemModal] Item clicked:', e.target);

        const link = e.target;
        const itemId = link.getAttribute('data-item-id');
        const itemName = link.getAttribute('data-item-name');
        const itemDesc = link.getAttribute('data-item-desc');
        const itemType = link.getAttribute('data-item-type');
        const filePath = link.getAttribute('data-file-path');

        console.log('[ItemModal] Item details:', {itemId, itemName, itemDesc, itemType, filePath});

        if (itemType === 'picture' || itemType === 'document') {
            console.log('[ItemModal] Showing modal for', itemType);
            showItemModal(itemId, itemName, itemDesc, itemType, filePath);
        } else {
            console.log('[ItemModal] Item type not supported:', itemType);
        }
    }

    function showItemModal(itemId, itemName, itemDesc, itemType, filePath) {
        console.log('[ItemModal] showItemModal called with:', {itemId, itemName, itemType});

        const overlay = document.getElementById('item-modal-overlay');
        const title = document.getElementById('item-modal-title');
        const body = document.getElementById('item-modal-body');

        if (!overlay || !title || !body) {
            console.error('[ItemModal] Modal elements not found!', {overlay, title, body});
            return;
        }

        title.textContent = itemName;

        // Show meta information
        let metaHtml = `<div class="item-meta">`;
        metaHtml += `<p><strong>ID:</strong> <code>${itemId}</code></p>`;
        if (itemDesc) {
            metaHtml += `<p><strong>説明:</strong> ${itemDesc}</p>`;
        }
        metaHtml += `<p><strong>タイプ:</strong> ${itemType}</p>`;
        metaHtml += `</div>`;

        body.innerHTML = metaHtml + '<p><em>読み込み中...</em></p>';
        overlay.classList.add('show');
        console.log('[ItemModal] Modal displayed, fetching content from API');

        // Fetch item content from API
        const apiUrl = `/api/item/view?item_id=${encodeURIComponent(itemId)}`;
        console.log('[ItemModal] Fetching:', apiUrl);
        console.log('[ItemModal] Full URL:', window.location.origin + apiUrl);
        console.log('[ItemModal] Item ID:', itemId);

        fetch(apiUrl)
            .then(response => {
                console.log('[ItemModal] API response status:', response.status);
                console.log('[ItemModal] API response ok:', response.ok);
                console.log('[ItemModal] API response headers:', response.headers);
                return response.json();
            })
            .then(data => {
                console.log('[ItemModal] API response data:', data);
                console.log('[ItemModal] data.success:', data.success);
                console.log('[ItemModal] data.content:', data.content ? `${data.content.substring(0, 100)}...` : 'undefined');
                console.log('[ItemModal] data.error:', data.error);

                if (data.success) {
                    let contentHtml = metaHtml;
                    if (itemType === 'picture') {
                        // Display image
                        contentHtml += `<img src="${data.file_path}" alt="${itemName}" />`;
                        console.log('[ItemModal] Displaying image:', data.file_path);
                    } else if (itemType === 'document') {
                        // Display document content
                        if (data.content) {
                            contentHtml += `<pre>${data.content}</pre>`;
                            console.log('[ItemModal] Displaying document, content length:', data.content.length);
                        } else {
                            contentHtml += `<p style="color: red;">コンテンツが空です</p>`;
                            console.error('[ItemModal] Document content is undefined or empty');
                        }
                    }
                    body.innerHTML = contentHtml;
                } else {
                    console.error('[ItemModal] API returned error:', data.error);
                    const errorMsg = data.error || 'Unknown error';
                    body.innerHTML = metaHtml + `<p style="color: red;">エラー: ${errorMsg}</p>`;
                }
            })
            .catch(error => {
                console.error('[ItemModal] Fetch error:', error);
                body.innerHTML = metaHtml + `<p style="color: red;">ファイルの読み込みに失敗しました: ${error}</p>`;
            });
    }

    function closeItemModal() {
        const overlay = document.getElementById('item-modal-overlay');
        overlay.classList.remove('show');
    }

    // Initialize on page load and after updates
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initItemModal);
    } else {
        initItemModal();
    }

    // Re-initialize when detail panels are updated
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            if (mutation.type === 'childList' && mutation.target.querySelector('.item-link')) {
                initItemModal();
            }
        });
    });

    observer.observe(document.body, {
        childList: true,
        subtree: true
    });
}
"""
