#!/usr/bin/env python3
"""Programmatic orchestrator for Letter Analyst document sweep and LifeOS push."""

import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import mcp_lifeos


def main():
    print("==== Letter Analyst: Document Sweep & LifeOS Push ====")
    
    db_path = "/home/node/.openclaw/analyst_registry.db"
    if not os.path.exists(db_path):
        print(f"Warning: Registry database not found at {db_path}. Ingesting empty list.")
        letters_data = {"document_processing_outputs": []}
    else:
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT item_id, task_id, inputs, metadata, created_at, updated_at "
                "FROM registry ORDER BY updated_at DESC LIMIT 10"
            )
            rows = cursor.fetchall()
            conn.close()
            
            recent_letters = []
            for row in rows:
                inputs_dict = {}
                metadata_dict = {}
                try:
                    if row["inputs"]:
                        inputs_dict = json.loads(row["inputs"])
                except Exception:
                    pass
                try:
                    if row["metadata"]:
                        metadata_dict = json.loads(row["metadata"])
                except Exception:
                    pass
                    
                # Extract clean filename, OCR classification, and urgency info
                # Inputs typically has 'source' or 'filename' or 'file_name'
                # Metadata typically has 'decision', 'classification', 'actions'
                filename = inputs_dict.get("filename") or inputs_dict.get("file_name") or row["item_id"]
                classification = metadata_dict.get("classification") or metadata_dict.get("decision", {}).get("classification") or "Unknown"
                urgency = metadata_dict.get("urgency") or metadata_dict.get("decision", {}).get("urgency") or "Medium"
                summary = metadata_dict.get("summary") or metadata_dict.get("decision", {}).get("summary") or "Processed document."
                
                recent_letters.append({
                    "filename": filename,
                    "classification": classification,
                    "urgency": urgency,
                    "summary": summary,
                    "processed_at": row["updated_at"]
                })
                
            letters_data = {
                "document_processing_outputs": recent_letters
            }
        except Exception as exc:
            print(f"ERROR: Failed to read registry database: {exc}")
            sys.exit(1)
            
    # Push to LifeOS
    print("Pushing section to LifeOS...")
    try:
        mcp_lifeos.call('tools/lifeos_section_push', {
            'agent_id': 'LETTER_ANALYST',
            'content': json.dumps(letters_data)
        })
        print("Section pushed successfully.")
    except Exception as exc:
        print(f"ERROR: Failed to push to LifeOS: {exc}")
        sys.exit(1)
        
    print("==== Letter Analyst Pipeline Finished ====")

if __name__ == "__main__":
    main()
