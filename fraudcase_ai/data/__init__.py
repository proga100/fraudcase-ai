"""Data slice: JSON loading, embedding, and MongoDB upsert pipeline."""

from fraudcase_ai.data.load import load_json_dir, build_documents, upsert_collections

__all__ = ["load_json_dir", "build_documents", "upsert_collections"]
