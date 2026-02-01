import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.getcwd())

# Mock Embedder BEFORE importing adapter
sys.modules["sai_memory.memory.recall"] = MagicMock()

from saiverse_memory import SAIMemoryAdapter
# We need to ensure SAIMemoryAdapter doesn't fail on import or init
# The adapter imports Embedder from sai_memory.memory.recall

def debug_threads(persona_id):
    print(f"--- Debugging Persona: {persona_id} ---")
    try:
        adapter = SAIMemoryAdapter(persona_id)
        if not adapter.is_ready():
            print("Adapter not ready")
            return

        summaries = adapter.list_thread_summaries()
        print(f"Found {len(summaries)} threads.")
        
        # Analyze global duplication
        print("\nAnalyzing Global Duplication...")
        
        # We need to scan all threads (or a sample). 
        # 9 threads is small, let's scan all.
        
        content_counts = {} # (role, content_hash) -> [thread_suffixes]
        
        for t in summaries:
            suffix = t["suffix"]
            full_id = t["thread_id"]
            
            # Use limited messages to speed up
            try:
                # Fetch first 10 messages (user complained about start of thread)
                msgs = adapter.get_thread_messages(full_id, page=0, page_size=10)
                for m in msgs:
                    key = (m['role'], m['content'])
                    if key not in content_counts:
                        content_counts[key] = []
                    content_counts[key].append(suffix)
            except Exception:
                print(f"Failed to read {suffix}")

        # Report findings
        print("\nTop Duplicated Messages (First 10 of threads):")
        sorted_keys = sorted(content_counts.keys(), key=lambda k: len(content_counts[k]), reverse=True)
        
        for k in sorted_keys[:5]:
            count = len(content_counts[k])
            role, content = k
            preview = content[:50].replace('\n', ' ')
            if count > 1:
                print(f"[{count} threads] [{role}] {preview}...")
                if count < 10:
                    print(f"    In: {', '.join(content_counts[k])}")

    except Exception as e:


        print(f"Error: {e}")
    finally:
        if 'adapter' in locals():
            adapter.close()

if __name__ == "__main__":
    # Assuming the persona ID from the screenshot "eris_city_a"
    debug_threads("eris_city_a")
