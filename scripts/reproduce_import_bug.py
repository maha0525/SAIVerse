import sys
import os
from unittest.mock import MagicMock

# Add project root
sys.path.append(os.getcwd())

# Mock Embedder
sys.modules["sai_memory.memory.recall"] = MagicMock()

from saiverse_memory import SAIMemoryAdapter
from tools.utilities.chatgpt_importer import ConversationMessage, ConversationRecord

def simulate_import():
    persona_id = "test_persona_import_bug"
    adapter = SAIMemoryAdapter(persona_id)
    
    # Clean up previous run
    conn = adapter.conn
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM threads")
    conn.commit()

    # Create dummy records
    # Record A
    msg_a = ConversationMessage(
        node_id="node_a", role="user", content="Content A", 
        create_time=None, metadata={"tags": []}
    )
    rec_a = ConversationRecord(
        identifier="thread_A", title="Thread A", 
        create_time=None, update_time=None, messages=[msg_a], 
        conversation_id="thread_A", default_model_slug="gpt-4"
    )

    # Record B
    msg_b = ConversationMessage(
        node_id="node_b", role="user", content="Content B", 
        create_time=None, metadata={"tags": []}
    )
    rec_b = ConversationRecord(
        identifier="thread_B", title="Thread B", 
        create_time=None, update_time=None, messages=[msg_b], 
        conversation_id="thread_B", default_model_slug="gpt-4"
    )

    records = [rec_a, rec_b]
    
    print("Starting Import Simulation...")
    
    # Copied logic from api/routes/people.py import_official_chatgpt
    for record in records:
        payloads = list(record.iter_memory_payloads(include_roles=["user", "assistant"]))
        thread_suffix = record.conversation_id or record.identifier
        
        print(f"Importing Record: {record.identifier} -> Suffix: {thread_suffix}")
        
        for payload in payloads:
            # Fix tags logic
            meta = payload.get("metadata", {})
            tags = meta.get("tags", [])
            if "conversation" not in tags:
                tags.append("conversation")
            meta["tags"] = tags
            payload["metadata"] = meta
            
            adapter.append_persona_message(payload, thread_suffix=thread_suffix)

    # Verify
    print("\nVerifying...")
    
    # Check Thread A
    msgs_a = adapter.get_thread_messages(f"{persona_id}:thread_A")
    print(f"Thread A messages: {[m['content'] for m in msgs_a]}")
    
    # Check Thread B
    msgs_b = adapter.get_thread_messages(f"{persona_id}:thread_B")
    print(f"Thread B messages: {[m['content'] for m in msgs_b]}")

    if any("Content B" in m['content'] for m in msgs_a):
        print("BUG DETECTED: Content B leaked into Thread A!")
    elif any("Content A" in m['content'] for m in msgs_b):
        print("BUG DETECTED: Content A leaked into Thread B!")
    else:
        print("SUCCESS: No leakage detected.")

    adapter.close()

if __name__ == "__main__":
    simulate_import()
