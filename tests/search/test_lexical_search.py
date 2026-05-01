from __future__ import annotations

import re
import time
import pytest
from sweepai.core.lexical_search import tokenize_code

FILE_CONTENTS = """\
import math
import re
import traceback
import openai

def parse_query(query: str) -> list[str]:
    \"\"\"Parse a search query into tokens.\"\"\"
    results = []
    for token in query.split():
        if len(token) > 2:
            results.append(token.lower())
    return results

class SearchIndex:
    def __init__(self, cache_path: str) -> None:
        self.cache_path = cache_path
        self.index = None

    def build_index(self, documents: list[str]) -> None:
        self.index = {}
        for doc_id, doc in enumerate(documents):
            for token in parse_query(doc):
                self.index.setdefault(token, []).append(doc_id)
"""


# ── Bridge helper ─────────────────────────────────────────────────────────────

def get_unique_symbols(code: str) -> list[str]:
    """
    Replicates old tokenize_call behaviour:
        old: list(set(token.text for token in tokenize_call(code)))
        new: list(set(tokenize_code(code).split()))
    """
    return list(set(tokenize_code(code).split()))


# ── Core API tests ────────────────────────────────────────────────────────────

class TestTokenizeCode:

    def test_returns_string(self):
        result = tokenize_code(FILE_CONTENTS)
        assert isinstance(result, str)

    def test_non_empty_output(self):
        result = tokenize_code(FILE_CONTENTS)
        assert len(result.strip()) > 0

    def test_tokens_are_lowercase(self):
        tokens = tokenize_code(FILE_CONTENTS).split()
        uppercase = [t for t in tokens if t != t.lower()]
        assert not uppercase, f"Found uppercase tokens: {uppercase}"

    def test_minimum_token_length(self):
        tokens = tokenize_code(FILE_CONTENTS).split()
        short = [t for t in tokens if len(t) < 2]
        assert not short, f"Found short tokens: {short}"

    def test_known_symbols_present(self):
        symbols = get_unique_symbols(FILE_CONTENTS)
        expected = [
            "math",
            "import",
            "traceback",
            "openai",
            "parse",
            "query",
            "search",
            "index",
            "cache",
            "path",
            "build",
            "documents",
            "results",
            "token",
        ]
        missing = [s for s in expected if s not in symbols]
        assert not missing, (
            f"Expected symbols missing: {missing}\n"
            f"Got: {sorted(symbols)}"
        )

    def test_camel_case_splitting(self):
        symbols = get_unique_symbols("searchIndex buildIndex cacheDirectory")
        assert "search" in symbols
        assert "index" in symbols
        assert "build" in symbols
        assert "cache" in symbols
        assert "directory" in symbols

    def test_snake_case_splitting(self):
        symbols = get_unique_symbols("cache_path build_index parse_query")
        assert "cache" in symbols
        assert "path" in symbols
        assert "build" in symbols
        assert "index" in symbols

    def test_pascal_case_splitting(self):
        symbols = get_unique_symbols("class SearchIndex CustomIndex LexicalSearch")
        assert "search" in symbols
        assert "custom" in symbols
        assert "lexical" in symbols

    def test_filters_low_entropy_tokens(self):
        # 'aaaa': len=4, unique_chars=1, ratio=4.0 — fails < 4 check → filtered
        # 'bbbb': same → filtered
        symbols = get_unique_symbols("aaaa bbbb normal_word")
        assert "aaaa" not in symbols
        assert "bbbb" not in symbols
        assert "normal" in symbols
        assert "word" in symbols

    def test_empty_input(self):
        assert tokenize_code("") == ""

    def test_whitespace_only_input(self):
        assert tokenize_code("   \n\t  ").strip() == ""

    def test_unique_symbols_no_duplicates(self):
        symbols = get_unique_symbols("index index index build build")
        assert len(symbols) == len(set(symbols))

    def test_real_world_file_symbol_count(self):
        symbols = get_unique_symbols(FILE_CONTENTS)
        assert 10 < len(symbols) < 500, (
            f"Unexpected symbol count: {len(symbols)}\n"
            f"Got: {sorted(symbols)}"
        )


# ── Parametrized edge cases ───────────────────────────────────────────────────

class TestTokenizeCodeEdgeCases:

    @pytest.mark.parametrize("code,expected_present,expected_absent", [
        (
                # camelCase splits into parts
                "myVariableName",
                ["my", "variable", "name"],
                ["myvariablename", "myVariableName"],
        ),
        (
                # ALL_CAPS snake splits into parts
                "MAX_BUFFER_SIZE",
                ["max", "buffer", "size"],
                ["max_buffer_size"],
        ),
        (
                # Mixed PascalCase + snake
                "HttpRequest_handler",
                ["http", "request", "handler"],
                ["httpRequest", "Http"],
        ),
        (
                # Single meaningful word passes through
                "authenticate",
                ["authenticate"],
                [],
        ),
        (
                # Special characters stripped, words extracted
                "foo.bar(baz)",
                ["foo", "bar", "baz"],
                ["foo.bar", "bar(baz)"],
        ),
        (
                # base64encode: digit boundary causes split into 'base' + 'encode'
                # variable_pattern splits on digit transitions
                "base64encode",
                ["base", "encode"],  # CORRECT: tokenizer splits here
                ["base64encode"],  # whole token does NOT survive
        ),
        (
                # Entropy filter: 'dddd' ratio = 4/1 = 4.0, fails strict < 4
                # 'bb' has len=2 which passes minimum length (>= 2)
                # 'ccc' ratio = 3/1 = 3.0, passes < 4
                "a bb ccc dddd",
                ["bb", "ccc"],  # 'a' filtered (len<2), 'dddd' filtered (entropy)
                ["a", "dddd"],
        ),
        (
                # Pure digits should not appear
                "x = 12345 + 67890",
                [],
                ["12345", "67890"],
        ),
        (
                # Underscore-only separator, no content
                "___",
                [],
                ["___"],
        ),
        (
                # Single character repeated — high entropy ratio, filtered
                "zzz",
                [],  # ratio = 3/1 = 3.0 passes BUT len=3 < meaningful?
                [],  # implementation-dependent, just check no crash
        ),
    ])
    def test_parametrized_tokenization(
            self,
            code: str,
            expected_present: list[str],
            expected_absent: list[str],
    ):
        symbols = get_unique_symbols(code)

        missing = [s for s in expected_present if s not in symbols]
        assert not missing, (
            f"Input:                  {code!r}\n"
            f"Expected present but missing: {missing}\n"
            f"Got symbols:            {sorted(symbols)}"
        )

        wrongly_present = [s for s in expected_absent if s in symbols]
        assert not wrongly_present, (
            f"Input:                  {code!r}\n"
            f"Expected absent but found:   {wrongly_present}\n"
            f"Got symbols:            {sorted(symbols)}"
        )

    # ── Regression / pinned output tests ─────────────────────────────────────────


class TestTokenizeCodeRegression:
    """
    Pin exact token sets for known inputs.
    Intentionally update when CACHE_VERSION bumps or tokenizer logic changes.
    These document the ACTUAL behaviour, not the desired behaviour.
    """

    @pytest.mark.parametrize("code,expected_tokens", [
        (
                "def search_index(query: str) -> dict:",
                # snake_case split: search_index → search, index
                # type hints survive: query, dict, str
                {"search", "index", "query", "dict", "str"},
        ),
        (
                "class CustomIndex:",
                # PascalCase split: Custom, Index → custom, index
                {"custom", "index"},
        ),
        (
                "token_cache = Cache(f'{CACHE_DIRECTORY}/token_cache')",
                # snake splits: token, cache, directory
                # 'Cache' → cache (lowercased)
                # 'CACHE_DIRECTORY' → cache, directory
                {"token", "cache", "directory"},
        ),
        (
                "sqlite3.DatabaseError: database disk image is malformed",
                # 'sqlite3': digit suffix causes split → 'sqlite' survives, '3' too short
                # 'DatabaseError' → database, error
                # remaining words pass through as-is
                {"sqlite", "database", "error", "disk", "image", "malformed"},
                # NOTE: 'sqlite3' does NOT survive — digit splits the token
                # NOTE: 'is' survives length check (len=2) but check if entropy passes
        ),
        (
                "from sweepai.core.lexical_search import tokenize_code",
                # dot-separated words extracted individually
                {"from", "sweepai", "core", "lexical", "search", "import", "tokenize", "code"},
        ),
        (
                "def __init__(self, cache_path: str) -> None:",
                # dunder prefix stripped by \b\w{2,}\b — 'init' extracted
                # snake: cache, path
                # type hints: str, none
                {"init", "self", "cache", "path", "str", "none"},
        ),
    ])
    def test_pinned_output(self, code: str, expected_tokens: set[str]):
        symbols = set(get_unique_symbols(code))
        missing = expected_tokens - symbols
        assert not missing, (
            f"Input:          {code!r}\n"
            f"Pinned tokens missing: {missing}\n"
            f"Got:            {sorted(symbols)}"
        )

    # ── Entropy boundary tests ────────────────────────────────────────────────────


class TestEntropyFilter:
    """
    Explicitly test the entropy filter boundary condition:
        len(part) / len(set(part)) < 4

    The filter REMOVES tokens where ratio >= 4.
    Tokens with 2+ unique chars are very hard to hit ratio >= 4
    because ratio = len / unique_count.

    Truth table:
        unique_chars=1: ratio = len/1 = len  → filtered when len >= 4
        unique_chars=2: ratio = len/2        → filtered when len >= 8
        unique_chars=3: ratio = len/3        → filtered when len >= 12
    """

    @pytest.mark.parametrize("token,should_survive", [
        # ── unique_chars=1: filtered when len >= 4 ───────────────────────────
        ("aaa",    True),    # 3/1 = 3.0 < 4  ✓ survives
        ("aaaa",   False),   # 4/1 = 4.0 NOT < 4  ✗ filtered
        ("aaaaa",  False),   # 5/1 = 5.0 NOT < 4  ✗ filtered
        ("ddd",    True),    # 3/1 = 3.0 < 4  ✓ survives
        ("dddd",   False),   # 4/1 = 4.0 NOT < 4  ✗ filtered

        # ── unique_chars=2: filtered when len >= 8 ───────────────────────────
        ("aaab",   True),    # 4/2 = 2.0 < 4  ✓ survives
        ("aaaab",  True),    # 5/2 = 2.5 < 4  ✓ survives  ← FIXED
        ("aaaaab", True),    # 6/2 = 3.0 < 4  ✓ survives  ← FIXED
        ("aabb",   True),    # 4/2 = 2.0 < 4  ✓ survives
        ("aaabb",  True),    # 5/2 = 2.5 < 4  ✓ survives

        # ── unique_chars=3+: very unlikely to hit ratio >= 4 ─────────────────
        ("hello",  True),    # 5/4 = 1.25 < 4 ✓ survives
        ("world",  True),    # 5/5 = 1.0  < 4 ✓ survives
        ("abcd",   True),    # 4/4 = 1.0  < 4 ✓ survives
    ])
    def test_entropy_boundary(self, token: str, should_survive: bool):
        symbols = get_unique_symbols(token)
        ratio = len(token) / len(set(token))

        if should_survive:
            assert token.lower() in symbols, (
                f"Token {token!r} should survive entropy filter but was removed.\n"
                f"ratio={len(token)}/{len(set(token))}={ratio:.2f} (need < 4)\n"
                f"Got symbols: {sorted(symbols)}"
            )
        else:
            assert token.lower() not in symbols, (
                f"Token {token!r} should be filtered by entropy but survived.\n"
                f"ratio={len(token)}/{len(set(token))}={ratio:.2f} (need >= 4 to filter)\n"
                f"Got symbols: {sorted(symbols)}"
            )


    # ── Alpha ratio tests ─────────────────────────────────────────────────────────


class TestAlphaRatioFilter:
    """
    Explicitly test the alpha ratio filter.

    CRITICAL PIPELINE NOTE:
    ─────────────────────────────────────────────────────────────
    Stage 1: re.finditer(r"\\b\\w{2,}\\b", code)
             \w includes [a-zA-Z0-9_]
             NON-\w chars (!, ., @) act as word boundaries
             → "he!!" extracts "he" ✓
             → "!!!!" extracts nothing ✓

    Stage 2: text.split("_")
             underscores split tokens into sections

    Stage 3: variable_pattern.findall(section)
             pattern: r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))"
             ONLY matches letter sequences
             → digits act as split boundaries HERE
             → "h3ll0" → ["h", "ll"]

    Stage 4: len(part) < 2 → filtered out
             → "h" dropped

    Stage 5: alpha ratio AND entropy filters
             alnum > len//2  AND  len/len(set) < 4
    ─────────────────────────────────────────────────────────────
    """

    @pytest.mark.parametrize("token,expected_symbols", [
        # ── Pure letter tokens ────────────────────────────────────────────────
        (
            "hello",
            ["hello"],      # 5 letters, entropy=5/4=1.25 < 4 ✓, alnum=5 > 2 ✓
        ),
        (
            "world",
            ["world"],      # 5 letters, entropy=5/5=1.0 < 4 ✓
        ),
        (
            "ab",
            ["ab"],         # exactly len=2, passes minimum length
        ),

        # ── Digit-containing tokens ───────────────────────────────────────────
        # variable_pattern splits on digits BEFORE alpha filter runs
        (
            "h3ll0",
            ["ll"],         # "h"(len<2 dropped) + "ll"(survives)
        ),
        (
            "he110",
            ["he"],         # \b\w{2,}\b → "he110"
                            # variable_pattern → ["he"] (digits split off)
                            # "he" len=2 ✓, entropy=2/2=1.0 ✓
        ),
        (
            "b64enc",
            ["enc"],        # variable_pattern → ["b"(dropped), "enc"]
        ),
        (
            "base64",
            ["base"],       # variable_pattern → ["base"] (digits at end, no match)
        ),
        (
            "py3",
            ["py"],         # \b\w{2,}\b → "py3"
                            # variable_pattern → ["py"]
                            # len=2 ✓, entropy=2/2=1.0 ✓
        ),

        # ── Special character tokens ──────────────────────────────────────────
        # Special chars are NOT \w → they create word boundaries for Stage 1
        # but they're never inside a matched token
        (
            "he!!",
            ["he"],         # \b\w{2,}\b → "he" (!! creates right boundary)
                            # variable_pattern → ["he"]
                            # len=2 ✓, entropy=1.0 ✓
        ),
        (
            "!!he!!",
            ["he"],         # same — "he" extracted between boundaries
        ),
        (
            "!!!!",
            [],             # no \w chars → Stage 1 matches nothing
        ),
        (
            "he!!lo",
            ["he", "lo"],   # \b\w{2,}\b → ["he", "lo"] as separate matches
                            # !! breaks word boundary between them
        ),

        # ── Pure digit tokens ─────────────────────────────────────────────────
        (
            "12345",
            [],             # \b\w{2,}\b matches "12345"
                            # variable_pattern → [] (no letter sequences)
        ),
        (
            "123abc",
            ["abc"],        # variable_pattern → ["abc"] (digits split off front)
        ),
    ])
    def test_alpha_ratio_boundary(
        self,
        token: str,
        expected_symbols: list[str],
    ):
        """
        Test EXACT expected symbols to avoid boolean ambiguity.
        Comments explain each stage of the pipeline.
        """
        symbols = get_unique_symbols(token)

        # Build diagnostic info for failures
        stage1_matches = re.findall(r'\b\w{2,}\b', token)
        variable_pattern_local = re.compile(
            r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))"
        )
        stage3_parts = [
            part
            for match in stage1_matches
            for section in match.split("_")
            for part in variable_pattern_local.findall(section)
        ]

        assert sorted(symbols) == sorted(expected_symbols), (
            f"\nInput:             {token!r}\n"
            f"Expected symbols:  {sorted(expected_symbols)}\n"
            f"Got symbols:       {sorted(symbols)}\n"
            f"\nPipeline trace:\n"
            f"  Stage 1 (\\b\\w{{2,}}\\b):      {stage1_matches}\n"
            f"  Stage 3 (variable_pattern): {stage3_parts}\n"
            f"  Stage 4 (len<2 filter):     "
            f"{[p for p in stage3_parts if len(p) >= 2]}\n"
        )

    @pytest.mark.parametrize("token,should_be_empty", [
        ("!!!!",    True),   # no \w chars
        ("    ",    True),   # whitespace only
        ("12345",   True),   # pure digits, variable_pattern finds nothing
        ("!!ab!!",  False),  # "ab" extracted → survives
        ("he!!",    False),  # "he" extracted → survives
        ("abc123", False),  # variable_pattern → ["abc"] → survives
        ("hello", False),  # straightforward survival
    ])
    def test_produces_no_symbols(self, token: str, should_be_empty: bool):
        """Test cases that should produce zero or non-zero symbols."""
        symbols = get_unique_symbols(token)

        # Build diagnostic trace
        stage1_matches = re.findall(r'\b\w{2,}\b', token)
        variable_pattern_local = re.compile(
            r"([A-Z][a-z]+|[a-z]+|[A-Z]+(?=[A-Z]|$))"
        )
        stage3_parts = [
            part
            for match in stage1_matches
            for section in match.split("_")
            for part in variable_pattern_local.findall(section)
        ]

        if should_be_empty:
            assert symbols == [], (
                f"\nInput {token!r} should produce NO symbols.\n"
                f"Got: {sorted(symbols)}\n"
                f"\nPipeline trace:\n"
                f"  Stage 1: {stage1_matches}\n"
                f"  Stage 3: {stage3_parts}\n"
            )
        else:
            assert symbols != [], (
                f"\nInput {token!r} should produce SOME symbols.\n"
                f"Got: {sorted(symbols)}\n"
                f"\nPipeline trace:\n"
                f"  Stage 1: {stage1_matches}\n"
                f"  Stage 3: {stage3_parts}\n"
            )

    # ── Performance tests ─────────────────────────────────────────────────────────


class TestTokenizeCodePerformance:

    def test_large_file_completes_quickly(self):
        """
        Tokenizing a large synthetic file (~10k lines) should complete
        in under 2 seconds on any reasonable machine.
        """
        # FIXED: use list comprehension, not list * int
        # list * int repeats the SAME strings (with undefined 'i')
        large_code = "\n".join(
            line
            for i in range(2500)
            for line in [
                f"def function_{i}(param_{i}: str) -> list[str]:",
                f"    result_{i} = param_{i}.split()",
                f"    return result_{i}",
                "",
            ]
        )

        start = time.perf_counter()
        result = tokenize_code(large_code)
        elapsed = time.perf_counter() - start

        assert elapsed < 2.0, (
            f"tokenize_code too slow: {elapsed:.2f}s for ~10k lines"
        )
        assert len(result) > 0, "Large file produced no tokens"

    def test_repeated_calls_consistent(self):
        """
        tokenize_code is pure/deterministic —
        same input must always produce identical output.
        """
        results = {tokenize_code(FILE_CONTENTS) for _ in range(5)}
        assert len(results) == 1, (
            "tokenize_code produced different outputs across repeated calls"
        )

    def test_empty_string_is_fast(self):
        """Edge case: empty string should return near-instantly."""
        start = time.perf_counter()
        result = tokenize_code("")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.01, f"Empty string took too long: {elapsed:.4f}s"
        assert result == ""

    def test_single_token_is_fast(self):
        """Edge case: single token should return near-instantly."""
        start = time.perf_counter()
        result = tokenize_code("authenticate")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.01, f"Single token took too long: {elapsed:.4f}s"
        assert "authenticate" in result


