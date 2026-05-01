from __future__ import annotations

from collections.abc import Iterable
import hashlib
import multiprocessing
import os
import re
import shutil
import subprocess
import time
from typing import Iterator

import tantivy
from diskcache import Cache
from loguru import logger
from redis import Redis
from tqdm import tqdm

from sweepai.utils.streamable_functions import streamable
from sweepai.utils.timer import Timer
from sweepai.config.server import CACHE_DIRECTORY, FILE_CACHE_DISABLED, REDIS_URL
from sweepai.core.entities import Snippet
from sweepai.core.repo_parsing_utils import directory_to_chunks
from sweepai.core.vector_db import multi_get_query_texts_similarity
from sweepai.dataclasses.files import Document
from sweepai.config.client import SweepConfig

# ── Constants ────────────────────────────────────────────────────────────────

CACHE_VERSION = "v1.0.16"
SNIPPET_FORMAT = "File path: {file_path}\n\n{contents}"

# ── Cache bootstrap ──────────────────────────────────────────────────────────


def _safe_cache(path: str) -> Cache | None:
    """
    Attempt to open a diskcache.Cache, rebuilding it if corrupted.
    Returns None only if rebuild also fails.
    """
    for attempt in ("open", "rebuild"):
        try:
            cache = Cache(path)
            cache.check()
            return cache
        except Exception as exc:
            if attempt == "open":
                logger.warning(
                    f"Cache corrupted at '{path}', rebuilding. Reason: {exc}"
                )
                shutil.rmtree(path, ignore_errors=True)
            else:
                logger.error(
                    f"Cache unrecoverable at '{path}', "
                    f"running without cache. Reason: {exc}"
                )
                return None


token_cache = _safe_cache(f"{CACHE_DIRECTORY}/token_cache")
snippets_cache = _safe_cache(f"{CACHE_DIRECTORY}/snippets_cache")


# ── Redis ────────────────────────────────────────────────────────────────────

redis_client: Redis | None = (
    Redis.from_url(REDIS_URL) if (REDIS_URL and not FILE_CACHE_DISABLED) else None
)


# ── Tantivy schema ───────────────────────────────────────────────────────────

# pylint: disable=no-member
_schema_builder = tantivy.SchemaBuilder()
_schema_builder.add_text_field("title", stored=True)
_schema_builder.add_text_field("body", stored=True)
_schema_builder.add_integer_field("doc_id", stored=True)
schema = _schema_builder.build()
# pylint: enable=no-member


# ── Helpers ──────────────────────────────────────────────────────────────────

variable_pattern = re.compile(r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))")


def _token_cache_key(content: str) -> str:
    return hashlib.sha256(f"{content}{CACHE_VERSION}".encode()).hexdigest()


def _cache_get(key: str) -> str | None:
    if token_cache is None:
        return None
    try:
        return token_cache.get(key)
    except Exception as exc:
        logger.warning(f"token_cache read failed: {exc}")
        return None


def _cache_set(key: str, value: str) -> None:
    if token_cache is None:
        return
    try:
        token_cache[key] = value
    except Exception as exc:
        logger.warning(f"token_cache write failed: {exc}")


# ── Tokenizer ────────────────────────────────────────────────────────────────


def tokenize_code(code: str) -> str:
    """
    Splits code into lowercase tokens by:
      - word boundaries (≥2 chars)
      - snake_case  →  individual parts
      - camelCase   →  individual parts
    Filters tokens that are too short, non-alphanumeric-heavy,
    or have very low character entropy.
    """
    tokens: list[str] = []
    for m in re.finditer(r"\b\w{2,}\b", code):
        text = m.group()
        for section in text.split("_"):
            for part in variable_pattern.findall(section):
                if len(part) < 2:
                    continue
                alpha_count = sum(1 for c in part if c.isalnum())
                entropy_ratio = len(part) / len(set(part))
                if alpha_count > len(part) // 2 and entropy_ratio < 4:
                    tokens.append(part.lower())
    return " ".join(tokens)


# ── Tantivy index wrapper ────────────────────────────────────────────────────


class IndexEmptyError(RuntimeError):
    """Raised when a tantivy searcher finds no documents after commit."""


class CustomIndex:
    """
    Thin wrapper around a tantivy index.
    Persists to *cache_path* so the index survives process restarts.
    """

    _SEARCHER_POLL_INTERVAL = 0.01
    _SEARCHER_MAX_POLLS = 100

    def __init__(self, cache_path: str) -> None:
        os.makedirs(cache_path, exist_ok=True)
        self.index = tantivy.Index(schema, path=cache_path)  # pylint: disable=no-member

    def add_documents(self, documents: Iterable[tuple[str, str]]) -> None:
        writer = self.index.writer()
        for doc_id, (title, text) in enumerate(documents):
            writer.add_document(
                tantivy.Document(  # pylint: disable=no-member
                    title=title,
                    body=text,
                    doc_id=doc_id,
                )
            )
        writer.commit()

    def search_index(self, query: str) -> list[tuple[str, float, tantivy.Document]]:
        """
        Tokenize query, parse it, poll until searcher is ready,
        then return (title, score, doc) triples.
        """
        tokenized_query = tokenize_code(query)
        parsed_query = self.index.parse_query(tokenized_query)
        searcher = self._wait_for_searcher()
        hits = searcher.search(parsed_query, limit=200).hits
        return [
            (searcher.doc(doc_id)["title"][0], score, searcher.doc(doc_id))
            for score, doc_id in hits
        ]

    def _wait_for_searcher(self) -> tantivy.Searcher:
        """
        Poll until the searcher sees committed documents.
        Tantivy searchers opened immediately after commit
        may briefly report 0 docs due to internal refresh lag.
        """
        for i in range(self._SEARCHER_MAX_POLLS):
            searcher = self.index.searcher()
            if searcher.num_docs > 0:
                return searcher
            wait = self._SEARCHER_POLL_INTERVAL * i
            logger.debug(
                f"Tantivy searcher not ready, "
                f"retry {i}/{self._SEARCHER_MAX_POLLS} "
                f"(sleeping {wait:.3f}s)"
            )
            time.sleep(wait)

        raise IndexEmptyError(
            f"Tantivy index still empty after "
            f"{self._SEARCHER_MAX_POLLS} polls — "
            f"writer.commit() may have failed silently."
        )


# ── Document conversion ───────────────────────────────────────────────────────


def snippets_to_docs(
    snippets: list[Snippet],
    len_repo_cache_dir: int,
) -> list[Document]:
    return [
        Document(
            title=f"{snippet.file_path[len_repo_cache_dir:]}:"
            f"{snippet.start}-{snippet.end}",
            content=snippet.get_snippet(add_ellipsis=False, add_lines=False),
        )
        for snippet in snippets
    ]


# ── Index preparation ─────────────────────────────────────────────────────────


def _tokenize_with_cache(
    docs: list[Document],
) -> list[str]:
    """
    Return tokenized strings for every doc, using token_cache for hits
    and multiprocessing for misses.
    """
    keys = [_token_cache_key(doc.content) for doc in docs]
    all_tokens = [_cache_get(k) for k in keys]
    miss_indices = [i for i, t in enumerate(all_tokens) if t is None]

    if not miss_indices:
        logger.debug("All tokens served from cache.")
        return all_tokens  # type: ignore[return-value]

    miss_contents = [docs[i].content for i in miss_indices]
    workers = max(1, multiprocessing.cpu_count() // 2)

    if workers > 1:
        chunksize = max(1, len(miss_indices) // (workers * 4))
        with multiprocessing.Pool(processes=workers) as pool:
            missed_tokens = list(
                tqdm(
                    pool.imap(tokenize_code, miss_contents, chunksize=chunksize),
                    total=len(miss_indices),
                    desc="Tokenizing documents",
                )
            )
    else:
        missed_tokens = [
            tokenize_code(c) for c in tqdm(miss_contents, desc="Tokenizing documents")
        ]

    for list_pos, doc_index in enumerate(miss_indices):
        token = missed_tokens[list_pos]
        all_tokens[doc_index] = token
        _cache_set(keys[doc_index], token)

    return all_tokens  # type: ignore[return-value]


@streamable
def prepare_index_from_snippets(
    snippets: list[Snippet],
    len_repo_cache_dir: int = 0,
    cache_path: str | None = None,
) -> Iterator[tuple[str, CustomIndex | None]]:
    """
    Build a tantivy BM25 index from *snippets*.

    Yields progress messages as (message, index) tuples so the
    agentic caller can stream status to the UI.
    Finally returns the completed CustomIndex (or None if no docs).
    """
    all_docs = snippets_to_docs(snippets, len_repo_cache_dir)
    if not all_docs:
        yield "No documents to index.", None
        return None

    index = CustomIndex(cache_path=cache_path)
    yield "Tokenizing documents...", index

    try:
        with Timer() as timer:
            all_tokens = _tokenize_with_cache(all_docs)
        logger.debug(f"Tokenizing took {timer.time_elapsed:.2f}s")

        yield "Building lexical index...", index

        all_titles = [doc.title for doc in all_docs]
        with Timer() as timer:
            index.add_documents(
                tqdm(
                    zip(all_titles, all_tokens),
                    total=len(all_docs),
                    desc="Indexing",
                )
            )
        logger.debug(f"Indexing took {timer.time_elapsed:.2f}s")

    except FileNotFoundError as exc:
        logger.exception(f"File not found during indexing: {exc}")
        yield "Indexing failed.", None
        return None

    yield "Index built.", index
    return index


# ── Lexical search ────────────────────────────────────────────────────────────


def search_index(
    query: str,
    index: CustomIndex,
) -> dict[str, float]:
    """
    Search the BM25 index and return min-max normalised scores.

    Returns
    -------
    dict[title -> normalised_score]   scores in [0.0, 1.0]
    """
    results = index.search_index(query)
    if not results:
        return {}

    # Deduplicate: keep highest score per title
    res: dict[str, float] = {}
    for title, score, _ in results:
        if score > res.get(title, float("-inf")):
            res[title] = score

    max_score = max(res.values())
    min_score = min(res.values())
    score_range = max_score - min_score

    if score_range == 0:
        return {k: 1.0 for k in res}

    return {k: (v - min_score) / score_range for k, v in res.items()}


# ── Vector search ─────────────────────────────────────────────────────────────


def compute_vector_search_scores(
    queries: list[str],
    snippets: list[Snippet],
) -> list[dict[str, float]]:
    """
    Compute cosine similarity scores between *queries* and *snippets*
    using dense vector embeddings.

    Returns
    -------
    list of dicts, one per query: dict[snippet.denotation -> score]
    """
    with Timer() as timer:
        snippet_str_to_contents = {
            snippet.denotation: SNIPPET_FORMAT.format(
                file_path=snippet.file_path,
                contents=snippet.get_snippet(add_ellipsis=False, add_lines=False),
            )
            for snippet in snippets
        }
    logger.info(f"Snippet formatting took {timer.time_elapsed:.2f}s")

    snippet_contents_array = list(snippet_str_to_contents.values())
    snippet_denotations = [snippet.denotation for snippet in snippets]

    multi_query_similarities = multi_get_query_texts_similarity(
        queries, snippet_contents_array
    )

    return [
        {snippet_denotations[i]: score for i, score in enumerate(query_similarities)}
        for query_similarities in multi_query_similarities
    ]


# ── Cache key helpers ─────────────────────────────────────────────────────────


def get_lexical_cache_key(
    repo_directory: str,
    commit_hash: str | None = None,
    seed: str = "",
) -> str:
    """
    Stable cache key based on:
      - repo name
      - current git HEAD (or provided commit hash)
      - CACHE_VERSION
      - optional seed (for variant indexes)
    """
    if commit_hash is None:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_directory,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning(
                f"git rev-parse failed in '{repo_directory}': "
                f"{result.stderr.strip()} — using 'unknown' as commit hash"
            )
            commit_hash = "unknown"
        else:
            commit_hash = result.stdout.strip()

    repo_name = os.path.basename(repo_directory)
    return f"{repo_name}_{commit_hash}_{CACHE_VERSION}_{seed}"


# ── Top-level pipeline ────────────────────────────────────────────────────────


@streamable
def prepare_lexical_search_index(
    repo_directory: str,
    sweep_config: SweepConfig,
    do_not_use_file_cache: bool = False,
    seed: str = "",
) -> Iterator[tuple[str, list[Snippet], CustomIndex | None]]:
    """
    Full pipeline: repo directory → tantivy BM25 index.

    Yields (message, snippets, index) triples for streaming progress.
    Finally returns (snippets, index).

    Stages
    ------
    1. Collect snippets  (cached by commit hash)
    2. Tokenize          (cached by content hash)
    3. Build tantivy index (persisted to disk via cache_path)
    """
    lexical_cache_key = get_lexical_cache_key(repo_directory, seed=seed)
    index_cache_path = f"{CACHE_DIRECTORY}/lexical_index_cache/{lexical_cache_key}"

    # ── Stage 1: snippet collection ───────────────────────────────────────────
    yield "Collecting snippets...", [], None

    snippets_result = (
        None
        if do_not_use_file_cache
        else (snippets_cache.get(lexical_cache_key) if snippets_cache else None)
    )

    if snippets_result is None:
        logger.info(f"Snippet cache miss for key: {lexical_cache_key}")
        snippets, file_list = directory_to_chunks(
            repo_directory,
            sweep_config,
            do_not_use_file_cache=do_not_use_file_cache,
        )
        if snippets_cache is not None and not do_not_use_file_cache:
            try:
                snippets_cache[lexical_cache_key] = (snippets, file_list)
            except Exception as exc:
                logger.warning(f"snippets_cache write failed: {exc}")
    else:
        logger.info(f"Snippet cache hit for key: {lexical_cache_key}")
        snippets, file_list = snippets_result

    # ── Stage 2 + 3: tokenize → index (streamed) ─────────────────────────────
    yield "Building index...", snippets, None

    index: CustomIndex | None = None
    for message, index in prepare_index_from_snippets.stream(
        snippets,
        len_repo_cache_dir=len(repo_directory) + 1,
        cache_path=index_cache_path,
    ):
        yield message, snippets, index

    # ── Done ──────────────────────────────────────────────────────────────────
    yield "Lexical index built.", snippets, index
    return snippets, index


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    repo_directory = os.getenv("REPO_DIRECTORY")
    if not repo_directory:
        raise EnvironmentError("REPO_DIRECTORY environment variable is not set.")

    sweep_config = SweepConfig()

    logger.info(f"Building lexical index for: {repo_directory}")
    start = time.perf_counter()

    snippets, index = prepare_lexical_search_index(
        repo_directory,
        sweep_config,
    )

    if index is None:
        logger.error("Index construction failed — no index returned.")
    else:
        result = search_index("logger export", index)
        elapsed = time.perf_counter() - start

        logger.info(f"Time taken: {elapsed:.2f}s")
        logger.info(f"Sample keys: {list(result.keys())[:5]}")
        logger.info(
            "Top 5 results: "
            + str(sorted(result.items(), key=lambda x: x[1], reverse=True)[:5])
        )
