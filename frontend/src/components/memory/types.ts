export type ImportSubTab = 'official' | 'extension' | 'native';

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
}

export interface MemoryImportResult {
  success: boolean;
  message: string;
}
