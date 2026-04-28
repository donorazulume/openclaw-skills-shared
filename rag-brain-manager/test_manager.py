import hashlib
import json
import os
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, call, patch

sys.modules["chromadb"] = MagicMock()
sys.modules["google"] = MagicMock()
sys.modules["google.genai"] = MagicMock()
sys.modules["google.cloud"] = MagicMock()
sys.modules["google.cloud.storage"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manager


# ── Helpers ──────────────────────────────────────────────────────────

def _capture(fn, *args, **kwargs):
    """Run fn, capture stdout, return parsed JSON output."""
    captured = StringIO()
    sys.stdout = captured
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = sys.__stdout__
    return json.loads(captured.getvalue())


def _mock_collection(name="test_col", count=0, ids=None, docs=None, metas=None):
    col = MagicMock()
    col.name = name
    col.count.return_value = count
    col.get.return_value = {
        "ids": ids or [],
        "documents": docs or [],
        "metadatas": metas or [],
    }
    return col


# ── Collection name validation ────────────────────────────────────────

class TestValidateCollectionName(unittest.TestCase):
    def test_valid_names(self):
        for name in ["letters", "mortgage_docs", "my-collection", "ab"]:
            # ab is too short (min 3), rest should pass
            pass
        self.assertEqual(manager.validate_collection_name("letters"), "letters")
        self.assertEqual(manager.validate_collection_name("mortgage_docs"), "mortgage_docs")
        self.assertEqual(manager.validate_collection_name("my-collection"), "my-collection")

    def test_too_short(self):
        with self.assertRaises(SystemExit):
            manager.validate_collection_name("ab")

    def test_too_long(self):
        with self.assertRaises(SystemExit):
            manager.validate_collection_name("a" * 64)

    def test_empty(self):
        with self.assertRaises(SystemExit):
            manager.validate_collection_name("")

    def test_invalid_chars(self):
        with self.assertRaises(SystemExit):
            manager.validate_collection_name("my collection")  # space
        with self.assertRaises(SystemExit):
            manager.validate_collection_name("col.name")  # dot

    def test_resolve_uses_cli_arg_first(self):
        result = manager.resolve_collection_name("my_col")
        self.assertEqual(result, "my_col")

    def test_resolve_falls_back_to_default(self):
        result = manager.resolve_collection_name(None)
        self.assertEqual(result, manager.DEFAULT_COLLECTION)


# ── Chunking ─────────────────────────────────────────────────────────

class TestChunkMarkdown(unittest.TestCase):
    def test_short_text_single_chunk(self):
        text = "Hello world, this is a short document."
        chunks = manager.chunk_markdown(text, chunk_size=1000)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_heading_split(self):
        text = "# Section A\nContent A.\n\n# Section B\nContent B."
        chunks = manager.chunk_markdown(text, chunk_size=1000)
        self.assertEqual(len(chunks), 2)
        self.assertIn("Section A", chunks[0])
        self.assertIn("Section B", chunks[1])

    def test_long_section_paragraph_split(self):
        para = "Word " * 300
        text = f"# Big Section\n\n{para}\n\n{para}"
        chunks = manager.chunk_markdown(text, chunk_size=500, overlap=50)
        self.assertTrue(len(chunks) > 1)

    def test_empty_text(self):
        chunks = manager.chunk_markdown("")
        self.assertEqual(chunks, [])

    def test_whitespace_only(self):
        chunks = manager.chunk_markdown("   \n\n   ")
        self.assertEqual(chunks, [])

    def test_preserves_heading_in_chunk(self):
        text = "Preamble text.\n\n# My Heading\nSome content here."
        chunks = manager.chunk_markdown(text, chunk_size=5000)
        self.assertTrue(any("# My Heading" in c for c in chunks))

    def test_character_fallback_for_huge_paragraph(self):
        text = "x" * 3000
        chunks = manager.chunk_markdown(text, chunk_size=500, overlap=100)
        self.assertTrue(len(chunks) >= 6)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)


# ── Expert Judgment ───────────────────────────────────────────────────

class TestExpertJudgment(unittest.TestCase):
    def test_small_file(self):
        self.assertIn("too small", manager.expert_judgment({"file_size": 10}))

    def test_large_file(self):
        self.assertIn("large", manager.expert_judgment({"file_size": 10 * 1024 * 1024}).lower())

    def test_normal_file(self):
        self.assertIsNone(manager.expert_judgment({"file_size": 5000}))

    def test_empty_collection(self):
        self.assertIn("empty", manager.expert_judgment({"doc_count": 0}).lower())

    def test_healthy_collection(self):
        self.assertIsNone(manager.expert_judgment({"doc_count": 100}))

    def test_high_duplicate_ratio(self):
        self.assertIn("duplicate", manager.expert_judgment({"total": 100, "duplicates": 30}).lower())

    def test_low_duplicate_ratio(self):
        self.assertIsNone(manager.expert_judgment({"total": 100, "duplicates": 5}))

    def test_zero_total(self):
        self.assertIsNone(manager.expert_judgment({"total": 0, "duplicates": 0}))

    def test_unrelated_dict(self):
        self.assertIsNone(manager.expert_judgment({"foo": "bar"}))


# ── Collection management ─────────────────────────────────────────────

class TestCreateCollection(unittest.TestCase):
    def test_create_new(self):
        mock_client = MagicMock()
        mock_client.list_collections.return_value = []
        mock_gcs = MagicMock()

        with patch.object(manager, "sync_to_gcs"):
            out = _capture(manager.handle_create_collection, mock_client, "new_col", mock_gcs)

        self.assertEqual(out["status"], "created")
        self.assertEqual(out["collection"], "new_col")
        mock_client.create_collection.assert_called_once_with(name="new_col")

    def test_create_already_exists(self):
        existing = MagicMock()
        existing.name = "existing_col"
        mock_client = MagicMock()
        mock_client.list_collections.return_value = [existing]
        mock_gcs = MagicMock()

        out = _capture(manager.handle_create_collection, mock_client, "existing_col", mock_gcs)

        self.assertEqual(out["status"], "already_exists")
        mock_client.create_collection.assert_not_called()


class TestDeleteCollection(unittest.TestCase):
    def test_delete_existing(self):
        existing = MagicMock()
        existing.name = "to_delete"
        mock_col = MagicMock()
        mock_col.count.return_value = 10

        mock_client = MagicMock()
        mock_client.list_collections.side_effect = [
            [existing],              # existence check
            [],                      # remaining after delete
        ]
        mock_client.get_collection.return_value = mock_col
        mock_gcs = MagicMock()

        with patch.object(manager, "sync_to_gcs"):
            out = _capture(manager.handle_delete_collection, mock_client, "to_delete", mock_gcs)

        self.assertEqual(out["status"], "deleted")
        self.assertEqual(out["documents_removed"], 10)
        mock_client.delete_collection.assert_called_once_with(name="to_delete")

    def test_delete_nonexistent_exits(self):
        mock_client = MagicMock()
        mock_client.list_collections.return_value = []
        mock_gcs = MagicMock()

        with self.assertRaises(SystemExit):
            manager.handle_delete_collection(mock_client, "missing", mock_gcs)


class TestListCollections(unittest.TestCase):
    def test_lists_with_sources(self):
        col = _mock_collection(
            name="letters",
            count=2,
            ids=["a", "b"],
            docs=["doc1", "doc2"],
            metas=[{"source": "letter_A"}, {"source": "letter_B"}],
        )
        mock_client = MagicMock()
        mock_client.list_collections.return_value = [col]

        out = _capture(manager.handle_list_collections, mock_client)

        self.assertEqual(out["total_collections"], 1)
        entry = out["collections"][0]
        self.assertEqual(entry["name"], "letters")
        self.assertEqual(entry["source_count"], 2)
        self.assertIn("letter_A", entry["sources"])

    def test_empty_db(self):
        mock_client = MagicMock()
        mock_client.list_collections.return_value = []

        out = _capture(manager.handle_list_collections, mock_client)
        self.assertEqual(out["total_collections"], 0)


# ── Query ─────────────────────────────────────────────────────────────

class TestHandleQuery(unittest.TestCase):
    @patch.object(manager, "get_embedding", return_value=[0.1] * 3072)
    def test_single_collection_query(self, mock_embed):
        col = MagicMock()
        col.query.return_value = {
            "ids": [["id1"]],
            "documents": [["Content"]],
            "metadatas": [[{"source": "test"}]],
            "distances": [[0.12]],
        }
        mock_genai = MagicMock()

        out = _capture(manager.handle_query, col, mock_genai, "test query", 3, "my_col")

        self.assertEqual(out["action"], "query")
        self.assertEqual(out["collection"], "my_col")
        self.assertEqual(len(out["matches"]), 1)
        self.assertEqual(out["matches"][0]["collection"], "my_col")
        self.assertAlmostEqual(out["matches"][0]["distance"], 0.12)

    @patch.object(manager, "get_embedding", return_value=[0.1] * 3072)
    def test_all_collections_query_merges_and_sorts(self, mock_embed):
        col_a = _mock_collection("col_a", count=1)
        col_b = _mock_collection("col_b", count=1)

        col_a_obj = MagicMock()
        col_a_obj.name = "col_a"
        col_a_obj.count.return_value = 1
        col_a_obj.query.return_value = {
            "ids": [["a1"]], "documents": [["Doc A"]], "metadatas": [[{}]], "distances": [[0.5]],
        }
        col_b_obj = MagicMock()
        col_b_obj.name = "col_b"
        col_b_obj.count.return_value = 1
        col_b_obj.query.return_value = {
            "ids": [["b1"]], "documents": [["Doc B"]], "metadatas": [[{}]], "distances": [[0.2]],
        }

        mock_client = MagicMock()
        mock_client.list_collections.return_value = [col_a, col_b]
        mock_client.get_collection.side_effect = lambda name: (
            col_a_obj if name == "col_a" else col_b_obj
        )
        mock_genai = MagicMock()

        out = _capture(manager.handle_query_all, mock_client, mock_genai, "query", 2)

        self.assertEqual(out["action"], "query")
        self.assertEqual(len(out["collections_searched"]), 2)
        # col_b (distance 0.2) should rank first
        self.assertEqual(out["matches"][0]["id"], "b1")
        self.assertEqual(out["matches"][1]["id"], "a1")

    @patch.object(manager, "get_embedding", return_value=[0.1] * 3072)
    def test_all_collections_skips_empty(self, mock_embed):
        # build a proper mock that list_collections returns but count() == 0
        empty_meta = MagicMock()
        empty_meta.name = "empty"

        empty_obj = MagicMock()
        empty_obj.name = "empty"
        empty_obj.count.return_value = 0

        mock_client = MagicMock()
        mock_client.list_collections.return_value = [empty_meta]
        mock_client.get_collection.return_value = empty_obj
        mock_genai = MagicMock()

        out = _capture(manager.handle_query_all, mock_client, mock_genai, "query", 3)

        # Empty collection should be skipped — collections_searched is empty
        self.assertEqual(out["collections_searched"], [])
        self.assertEqual(out["matches"], [])


# ── Ingest ────────────────────────────────────────────────────────────

class TestHandleIngest(unittest.TestCase):
    @patch.object(manager, "sync_to_gcs")
    @patch.object(manager, "get_embeddings_batch", return_value=[[0.1] * 3072])
    @patch.object(manager, "chunk_markdown", return_value=["chunk one"])
    def test_ingest_uses_collection_in_chunk_id(self, mock_chunk, mock_embed, mock_sync):
        """Chunk IDs must be scoped to the collection to avoid cross-collection collisions."""
        col = MagicMock()
        mock_genai = MagicMock()
        mock_gcs = MagicMock()

        import tempfile, os as _os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test\nContent here.")
            tmp = f.name

        try:
            out = _capture(
                manager.handle_ingest,
                col, mock_genai, mock_gcs, tmp, "doc_A", "col_X",
            )
        finally:
            _os.unlink(tmp)

        self.assertEqual(out["status"], "success")
        self.assertEqual(out["collection"], "col_X")
        self.assertEqual(out["source"], "doc_A")

        # Verify chunk ID is scoped: md5("col_X:doc_A:0")
        expected_id = hashlib.md5("col_X:doc_A:0".encode()).hexdigest()
        upsert_call = col.upsert.call_args
        self.assertIn(expected_id, upsert_call[1]["ids"])

        # Metadata must include collection field
        meta = upsert_call[1]["metadatas"][0]
        self.assertEqual(meta["collection"], "col_X")

    def test_ingest_missing_file_exits(self):
        col = MagicMock()
        mock_genai = MagicMock()
        mock_gcs = MagicMock()

        with self.assertRaises(SystemExit):
            manager.handle_ingest(col, mock_genai, mock_gcs, "/no/such/file.md", "src", "col")


# ── Delete source ─────────────────────────────────────────────────────

class TestHandleDeleteSource(unittest.TestCase):
    @patch.object(manager, "sync_to_gcs")
    def test_deletes_matching_chunks(self, mock_sync):
        col = _mock_collection(
            ids=["a1", "a2", "b1"],
            metas=[
                {"source": "doc_A"},
                {"source": "doc_A"},
                {"source": "doc_B"},
            ],
        )
        mock_gcs = MagicMock()

        out = _capture(manager.handle_delete_source, col, mock_gcs, "doc_A", "my_col")

        self.assertEqual(out["status"], "deleted")
        self.assertEqual(out["chunks_removed"], 2)
        col.delete.assert_called_once_with(ids=["a1", "a2"])

    def test_source_not_found(self):
        col = _mock_collection(
            ids=["b1"],
            metas=[{"source": "doc_B"}],
        )
        mock_gcs = MagicMock()

        out = _capture(manager.handle_delete_source, col, mock_gcs, "doc_A", "my_col")

        self.assertEqual(out["status"], "not_found")
        self.assertEqual(out["chunks_removed"], 0)
        col.delete.assert_not_called()


# ── Optimize ──────────────────────────────────────────────────────────

class TestHandleOptimize(unittest.TestCase):
    def test_dry_run_finds_duplicates(self):
        col = _mock_collection(ids=["a", "b", "c"], docs=["hello", "world", "hello"])
        mock_gcs = MagicMock()

        out = _capture(manager.handle_optimize, col, mock_gcs, True, "my_col")

        self.assertEqual(out["status"], "dry_run")
        self.assertEqual(out["collection"], "my_col")
        self.assertEqual(out["duplicates_found"], 1)
        col.delete.assert_not_called()

    @patch.object(manager, "sync_to_gcs")
    def test_optimize_deletes_and_syncs(self, mock_sync):
        col = _mock_collection(ids=["a", "b", "c"], docs=["hello", "world", "hello"])
        mock_gcs = MagicMock()

        out = _capture(manager.handle_optimize, col, mock_gcs, False, "my_col")

        self.assertEqual(out["status"], "optimized")
        self.assertEqual(out["duplicates_removed"], 1)
        col.delete.assert_called_once()

    def test_no_duplicates_skips_sync(self):
        col = _mock_collection(ids=["a", "b"], docs=["hello", "world"])
        mock_gcs = MagicMock()

        out = _capture(manager.handle_optimize, col, mock_gcs, False, "my_col")

        self.assertEqual(out["duplicates_removed"], 0)
        col.delete.assert_not_called()


# ── Report ────────────────────────────────────────────────────────────

class TestHandleReport(unittest.TestCase):
    def test_report_multiple_collections(self):
        col_a = _mock_collection("letters", 42)
        col_b = _mock_collection("mortgages", 0)
        mock_client = MagicMock()
        mock_client.list_collections.return_value = [col_a, col_b]

        out = _capture(manager.handle_report, mock_client)

        self.assertEqual(out["health"], "OK")
        self.assertEqual(out["total_collections"], 2)
        names = [c["name"] for c in out["collections"]]
        self.assertIn("letters", names)
        self.assertIn("mortgages", names)
        # Empty collection should have a warning
        mortgages = next(c for c in out["collections"] if c["name"] == "mortgages")
        self.assertIn("warning", mortgages)


# ── Split helpers ─────────────────────────────────────────────────────

class TestSplitHelpers(unittest.TestCase):
    def test_split_on_headings_no_headings(self):
        result = manager._split_on_headings("Just plain text")
        self.assertEqual(result, ["Just plain text"])

    def test_split_on_headings_multiple(self):
        text = "# A\nfoo\n## B\nbar\n# C\nbaz"
        result = manager._split_on_headings(text)
        self.assertEqual(len(result), 3)

    def test_split_on_headings_with_preamble(self):
        text = "Preamble\n# First\nContent"
        result = manager._split_on_headings(text)
        self.assertEqual(len(result), 2)
        self.assertIn("Preamble", result[0])

    def test_split_by_chars(self):
        text = "a" * 100
        result = manager._split_by_chars(text, chunk_size=30, overlap=10)
        self.assertTrue(len(result) >= 4)
        self.assertEqual(len(result[0]), 30)


if __name__ == "__main__":
    unittest.main()
