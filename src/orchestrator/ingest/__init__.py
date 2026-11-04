"""Deterministic ingestion of planning documents (Requirement A)."""

from orchestrator.ingest.parser import parse_text
from orchestrator.ingest.readers import SUPPORTED_SUFFIXES, read_document
from orchestrator.ingest.service import IngestResult, ingest_file, persist_tree

__all__ = [
    "SUPPORTED_SUFFIXES",
    "IngestResult",
    "ingest_file",
    "parse_text",
    "persist_tree",
    "read_document",
]
