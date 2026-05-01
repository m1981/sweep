import json
import multiprocessing
import os
from typing import Generator

import backoff
from diskcache import Cache
import numpy as np
import openai
import requests
from loguru import logger
from scipy.spatial.distance import cdist

from tqdm import tqdm
import voyageai
import boto3
from botocore.exceptions import ClientError
from voyageai import error as voyageai_error

from sweepai.utils.timer import Timer
from sweepai.config.server import BATCH_SIZE, CACHE_DIRECTORY, VOYAGE_API_AWS_ENDPOINT_NAME, VOYAGE_API_KEY, VOYAGE_API_USE_AWS
from sweepai.utils.hash import hash_sha256
from sweepai.utils.openai_proxy import get_embeddings_client
from sweepai.utils.tiktoken_utils import Tiktoken

# Now uses Voyage AI if available, with asymmetric embedding
# CACHE_VERSION = "v2.0.04" + "-voyage" if VOYAGE_API_KEY else ""
suffix = "-voyage-aws" if VOYAGE_API_USE_AWS else "-voyage" if VOYAGE_API_KEY else ""
CACHE_VERSION = "v2.1.1" + suffix 
tiktoken_client = Tiktoken()
vector_cache = Cache(f'{CACHE_DIRECTORY}/vector_cache') # we instantiate a singleton, diskcache will handle concurrency


def cosine_similarity(a, B):
    # use scipy
    return 1 - cdist(a, B, metric='cosine')


def chunk(texts: list[str], batch_size: int) -> Generator[list[str], None, None]:
    logger.info(f"Truncating {len(texts)} texts")
    texts = [text[:25000] if len(text) > 25000 else text for text in texts]
    # remove empty string
    texts = [text if text else " " for text in texts]
    logger.info(f"Finished truncating {len(texts)} texts")
    for i in range(0, len(texts), batch_size):
        yield texts[i : i + batch_size] if i + batch_size < len(texts) else texts[i:]


# @file_cache(ignore_params=["texts"])
def multi_get_query_texts_similarity(queries: list[str], documents: list[str]) -> list[float]:
    if not documents:
        return []
    embeddings = embed_text_array(documents)
    embeddings = np.concatenate(embeddings)
    with Timer() as timer:
        query_embedding = np.array(openai_call_embedding(queries, input_type="query"))
    logger.info(f"Embedding query took {timer.time_elapsed:.2f} seconds")
    with Timer() as timer:
        similarity = cosine_similarity(query_embedding, embeddings)
    logger.info(f"Similarity took {timer.time_elapsed:.2f} seconds")
    similarity = similarity.tolist()
    return similarity


def normalize_l2(x):
    x = np.array(x)
    if x.ndim == 1:
        norm = np.linalg.norm(x)
        if norm == 0:
            return x
        return x / norm
    else:
        norm = np.linalg.norm(x, 2, axis=1, keepdims=True)
        return np.where(norm == 0, x, x / norm)

OPENAI_MAX_TOKENS_PER_REQUEST = 300_000
OPENAI_MAX_BATCH_SIZE = 2048
VOYAGE_MAX_TOKENS_PER_REQUEST = 120_000
VOYAGE_MAX_BATCH_SIZE = 128


def batch_by_token_count(
    texts: list[str],
    max_tokens: int,
    max_batch_size: int,
) -> list[list[str]]:
    """
    Split texts into batches respecting both total token limit and max batch size.
    Replaces naive fixed-size batching in embed_text_array.
    """
    batches: list[list[str]] = []
    batch: list[str] = []
    token_count = 0

    for text in texts:
        text_token_count = tiktoken_client.count(text)
        if batch and (
            token_count + text_token_count > max_tokens * 0.90  # 10% safety margin
            or len(batch) >= max_batch_size
        ):
            batches.append(batch)
            batch = [text]
            token_count = text_token_count
        else:
            batch.append(text)
            token_count += text_token_count

    if batch:
        batches.append(batch)

    logger.debug(
        f"batch_by_token_count: {len(texts)} texts → {len(batches)} batches "
        f"(max_tokens={max_tokens}, max_batch_size={max_batch_size})"
    )
    return batches


def embed_text_array(texts: list[str]) -> list[np.ndarray]:
    texts = [text if text else " " for text in texts]

    # Pick limits based on active provider — mirrors logic in openai_call_embedding_router
    if VOYAGE_API_USE_AWS or VOYAGE_API_KEY:
        batches = batch_by_token_count(
            texts,
            max_tokens=VOYAGE_MAX_TOKENS_PER_REQUEST,
            max_batch_size=VOYAGE_MAX_BATCH_SIZE,
        )
        provider = "voyage-aws" if VOYAGE_API_USE_AWS else "voyage"
    else:
        batches = batch_by_token_count(
            texts,
            max_tokens=OPENAI_MAX_TOKENS_PER_REQUEST,
            max_batch_size=OPENAI_MAX_BATCH_SIZE,
        )
        provider = "openai"

    logger.debug(
        f"embed_text_array: {len(texts)} texts → {len(batches)} token-aware batches "
        f"using {provider}"
    )

    workers = min(max(1, multiprocessing.cpu_count() // 4), 1)
    with Timer() as timer:
        if workers > 1 and len(batches) > 1:
            with multiprocessing.Pool(processes=workers) as pool:
                embeddings = list(
                    tqdm(
                        pool.imap(openai_with_expo_backoff, batches),
                        total=len(batches),
                        desc="openai embedding",
                    )
                )
        else:
            embeddings = [
                openai_with_expo_backoff(batch)
                for batch in tqdm(batches, desc="openai embedding")
            ]
    logger.info(f"Embedding docs took {timer.time_elapsed:.2f} seconds")
    return embeddings



# @redis_cache()
def openai_call_embedding_router(batch: list[str], input_type: str="document"): # input_type can be query or document
    VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", None)
    VOYAGE_API_AWS_ACCESS_KEY = os.environ.get("VOYAGE_API_AWS_ACCESS_KEY", None)
    VOYAGE_API_AWS_SECRET_KEY = os.environ.get("VOYAGE_API_AWS_SECRET_KEY", None)
    VOYAGE_API_AWS_REGION = os.environ.get("VOYAGE_API_AWS_REGION", None)
    VOYAGE_API_USE_AWS = VOYAGE_API_AWS_ACCESS_KEY and VOYAGE_API_AWS_SECRET_KEY and VOYAGE_API_AWS_REGION
    if len(batch) == 0:
        return np.array([])
    if VOYAGE_API_USE_AWS:
        sm_runtime = boto3.client(
            "sagemaker-runtime",
            aws_access_key_id=VOYAGE_API_AWS_ACCESS_KEY,
            aws_secret_access_key=VOYAGE_API_AWS_SECRET_KEY,
            region_name=VOYAGE_API_AWS_REGION
        )
        input_json = json.dumps({
            "input": batch,
            "input_type": input_type, 
            "truncation": "true"
        })
        response = sm_runtime.invoke_endpoint(
            EndpointName=VOYAGE_API_AWS_ENDPOINT_NAME,
            ContentType="application/json",
            Accept="application/json",
            Body=input_json,
        )
        body = response["Body"]
        obj = json.load(body)
        data = obj["data"]
        return np.array([vector["embedding"] for vector in data])
    elif VOYAGE_API_KEY:
        client = voyageai.Client(api_key=VOYAGE_API_KEY)
        result = client.embed(batch, model="voyage-code-2", input_type=input_type, truncation=True)
        cut_dim = np.array([data for data in result.embeddings])
        normalized_dim = normalize_l2(cut_dim)
        del client
        return normalized_dim
    else:
        client = get_embeddings_client()
        response = client.embeddings.create(
            input=batch, model="text-embedding-3-small", encoding_format="float"
        )
        cut_dim = np.array([data.embedding for data in response.data])[:, :512]
        normalized_dim = normalize_l2(cut_dim)
        # save results to redis
        return normalized_dim

def openai_call_embedding(batch: list[str], input_type: str = "document"):
    logger.debug(f"openai_call_embedding called: batch_size={len(batch)}, input_type={input_type}")
    try:
        result = openai_call_embedding_router(batch, input_type)
        if result is None:
            logger.error("openai_call_embedding_router returned None unexpectedly")
            raise ValueError("openai_call_embedding_router returned None")
        logger.debug(f"openai_call_embedding_router success: shape={np.array(result).shape}")
        return result
    except (voyageai_error.InvalidRequestError, ClientError) as e:
        if len(batch) > 1 and "Please lower the number of tokens in the batch." in str(e):
            logger.error(
                f"Token batch too large, max_tokens={max([tiktoken_client.count(t) for t in batch])}, "
                f"splitting batch of {len(batch)} in half"
            )
            mid = len(batch) // 2
            left = openai_call_embedding(batch[:mid], input_type)
            right = openai_call_embedding(batch[mid:], input_type)
            return np.concatenate((left, right))
        else:
            logger.error(f"Unrecoverable voyage/aws embedding error: {e}")
            raise
    except openai.BadRequestError as e:
        error_str = str(e)

        # Phrases that indicate the batch is too large and should be split
        BATCH_TOO_LARGE_PHRASES = (
            "maximum context length",       # older openai message
            "maximum input length",         # e.g. "Invalid 'input[81]': maximum input length is 8192 tokens."
            "max 300000 tokens per request",# e.g. "Requested 346763 tokens, max 300000 tokens per request"
            "max_tokens_per_request",       # error code variant
            "tokens per request",           # catch-all for similar future variants
        )

        if len(batch) > 1 and any(phrase in error_str for phrase in BATCH_TOO_LARGE_PHRASES):
            logger.warning(
                f"Batch token limit exceeded for batch of {len(batch)} texts "
                f"(total_tokens~={sum([tiktoken_client.count(t) for t in batch])}), "
                f"splitting in half and retrying"
            )
            mid = len(batch) // 2
            left = openai_call_embedding(batch[:mid], input_type)
            right = openai_call_embedding(batch[mid:], input_type)
            return np.concatenate((left, right))
        elif len(batch) == 1 and any(phrase in error_str for phrase in BATCH_TOO_LARGE_PHRASES):
            # Single item exceeds limit — truncate it
            logger.warning(
                f"Single item token limit exceeded "
                f"(tokens={tiktoken_client.count(batch[0])}), truncating to 8192"
            )
            batch = [tiktoken_client.truncate_string(batch[0])]
            return openai_call_embedding(batch, input_type)
        else:
            logger.error(f"BadRequestError not related to token length: {e}")
            raise





@backoff.on_exception(
    backoff.expo,
    requests.exceptions.Timeout,
    max_tries=5,
)
def openai_with_expo_backoff(batch: tuple[str]):
    logger.debug(f"openai_with_expo_backoff called with batch size: {len(batch)}")

    # check cache first
    embeddings: list[np.ndarray | None] = [None] * len(batch)
    cache_keys = [hash_sha256(text) + CACHE_VERSION for text in batch]

    try:
        for i, cache_key in enumerate(cache_keys):
            cache_value = vector_cache.get(cache_key)
            if cache_value is not None:
                embeddings[i] = cache_value
    except Exception as e:
        logger.warning(f"Error reading embeddings from cache: {e}")

    cached_count = sum(1 for e in embeddings if e is not None)
    logger.debug(f"Cache hits: {cached_count}/{len(batch)}")

    # not stored in cache, call openai
    uncached_batch = [
        text for i, text in enumerate(batch) if embeddings[i] is None
    ]
    if len(uncached_batch) == 0:
        logger.debug("All embeddings served from cache, skipping API call")
        return np.array(embeddings)

    logger.debug(f"Calling openai_call_embedding for {len(uncached_batch)} uncached texts")

    new_embeddings = None  # explicit init so we can detect failure
    try:
        new_embeddings = openai_call_embedding(uncached_batch)
        logger.debug(
            f"openai_call_embedding returned type={type(new_embeddings)}, "
            f"value={'None' if new_embeddings is None else f'shape={np.array(new_embeddings).shape}'}"
        )
    except requests.exceptions.Timeout as e:
        # BUG WAS HERE: exception was swallowed, new_embeddings left undefined
        logger.exception(f"Timeout error occurred while embedding (will raise): {e}")
        raise  # ← re-raise so backoff decorator can retry
    except Exception as e:
        logger.exception(f"Unexpected error during embedding: {e}")
        if any(tiktoken_client.count(text) > 8192 for text in uncached_batch):
            logger.warning(
                f"Token count exceeded, max={max([tiktoken_client.count(text) for text in uncached_batch])}, "
                f"truncating to 8192 tokens and retrying"
            )
            uncached_batch = [tiktoken_client.truncate_string(text) for text in uncached_batch]
            new_embeddings = openai_call_embedding(uncached_batch)
            logger.debug(f"Retry after truncation returned type={type(new_embeddings)}")
        else:
            raise

    # Guard: new_embeddings must not be None at this point
    if new_embeddings is None:
        logger.error(
            f"new_embeddings is None after API call — "
            f"batch size={len(uncached_batch)}, "
            f"sample text[:100]={uncached_batch[0][:100] if uncached_batch else 'empty'}"
        )
        raise ValueError(
            f"openai_call_embedding returned None for batch of size {len(uncached_batch)}"
        )

    indices = [i for i, emb in enumerate(embeddings) if emb is None]
    logger.debug(f"Indices to fill: {len(indices)}, new_embeddings length: {len(new_embeddings)}")

    if len(indices) != len(new_embeddings):
        logger.error(
            f"Length mismatch: indices={len(indices)}, new_embeddings={len(new_embeddings)}, "
            f"total batch={len(batch)}, uncached={len(uncached_batch)}"
        )
        raise ValueError(
            f"Embedding count mismatch: expected {len(indices)}, got {len(new_embeddings)}"
        )

    for i, index in enumerate(indices):
        embeddings[index] = new_embeddings[i]

    # store in cache
    try:
        for cache_key, embedding in zip(cache_keys, embeddings):
            vector_cache.set(cache_key, embedding)
        embeddings = np.array(embeddings)
        logger.debug(f"Stored {len(cache_keys)} embeddings in cache, final shape: {embeddings.shape}")
    except Exception as e:
        logger.warning(f"Error storing embeddings in cache: {e}")

    return embeddings


if __name__ == "__main__":
    texts = ["sasxtt " * 10000 for i in range(10)] + ["abb " * 1 for i in range(10)]
    embeddings = embed_text_array(texts)
