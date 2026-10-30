export type ImportSubTab = 'official' | 'claude' | 'extension' | 'native';

export type MemoryImportStep = 'upload' | 'select' | 'embedding-dialog' | 'importing' | 'thread-select';

export interface MemoryImportProps {
  personaId: string;
  onImportComplete?: () => void;
}

export interface MemoryImportUiResult {
  type: 'success' | 'error';
  message: string;
}

export interface ThreadSummary {
  thread_id: string;
  suffix: string;
  preview: string;
  active: boolean;
  /** 人が読むスレッド名 (取り込み元の会話タイトル)。無ければ suffix を表示する */
  title?: string | null;
  message_count?: number;
  /** epoch 秒 */
  first_created_at?: number | null;
  last_created_at?: number | null;
}

export interface ConversationSummary {
  idx: number;
  id: string;
  conversation_id: string | null;
  title: string;
  create_time: string | null;
  update_time: string | null;
  message_count: number;
  preview: string | null;
  /** Claude projects のみ: プロジェクト指示 (prompt_template) を持つか */
  has_instructions?: boolean | null;
}

export interface PreviewData {
  conversations: ConversationSummary[];
  cache_key: string;
  total_count: number;
}

export interface NativePreviewThread {
  thread_id: string;
  message_count: number;
  has_stelis: boolean;
  preview: string;
}

export interface NativePreviewData {
  format: string;
  source_persona: string;
  exported_at: string | null;
  thread_count: number;
  total_messages: number;
  threads: NativePreviewThread[];
}

/** Claude 公式エクスポートのカテゴリ (インポート対象) */
export type ClaudeCategory = 'conversations' | 'claude_code' | 'projects' | 'memories';

export interface ClaudeMemoryItemSummary {
  idx: number;
  id: string;
  kind: 'conversations_memory' | 'project_memory' | 'memory_file' | string;
  title: string;
  preview: string | null;
  char_count: number;
}

export interface ClaudePreviewData {
  category: ClaudeCategory | string;
  conversations: ConversationSummary[];
  memory_items: ClaudeMemoryItemSummary[];
  cache_key: string;
  total_count: number;
}

/** preview が 400 で返す構造化エラー (カテゴリ違い・対象外カテゴリの案内) */
export interface ClaudePreviewErrorDetail {
  code?: string;
  detected_category?: string | null;
  expected_category?: string | null;
}

export interface ClaudeImportRequest {
  cache_key: string;
  conversation_ids: string[];
  skip_embedding: boolean;
  append_instructions_to_system_prompt?: boolean;
}

export interface ClaudeMemoryImportResult {
  success: boolean;
  created: number;
  skipped: number;
  trunk_title: string;
  message: string;
}

export interface MemoryImportRequest {
  cache_key: string;
  conversation_ids: string[];
  skip_embedding: boolean;
}

export interface MemoryImportProgress {
  running: boolean;
  success?: boolean;
  message?: string;
  progress?: number;
  total?: number;
  // Background embedding of the messages this import stored (chat-log imports only).
  embedding_running?: boolean | null;
  embedding_progress?: number | null;
  embedding_total?: number | null;
  embedding_failed?: number | null;
  embedding_error?: string | null;
}

export interface MemoryImportResult {
  success: boolean;
  message: string;
}
