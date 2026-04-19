"""
Document chunker: split a plain-text document into overlapping windows of
approximately ``chunk_size`` words, breaking on sentence boundaries where
possible.

No NLP dependency is used. Sentence detection is a simple split on ". "
(period-space), which handles the vast majority of prose found in SEC filings,
Federal Register notices, and news articles.

Word count (``len(text.split())``) is used as the token approximation.
This is intentionally coarse — it avoids a tokeniser dependency and is
accurate enough for the 512-word default window that targets ~700 BPE tokens,
safely below the 8192-token limit of text-embedding-3-small.
"""

from __future__ import annotations


def chunk_text(
    text: str,
    chunk_size: int = 512,
    overlap: int = 64,
) -> list[str]:
    """Split ``text`` into overlapping word-count windows.

    Args:
        text:       The plain-text document to split.
        chunk_size: Target maximum number of words per chunk.
        overlap:    Number of words from the end of chunk N to carry forward
                    as the beginning of chunk N+1. Must be < ``chunk_size``.

    Returns:
        A list of non-empty chunk strings. If ``text`` is shorter than
        ``chunk_size`` words, a single-element list is returned.
        Empty strings are never included.

    Algorithm:
        1. Split on ``". "`` to produce sentence fragments.
        2. Accumulate sentences into a current window until adding the next
           sentence would exceed ``chunk_size`` words.
        3. When a window is full, emit it and seed the next window with the
           last ``overlap`` words from the emitted window (so context is
           preserved across chunk boundaries).
        4. Sentences longer than ``chunk_size`` words are hard-split by words
           so they are never silently dropped.
    """
    if overlap >= chunk_size:
        raise ValueError(
            f"overlap ({overlap}) must be less than chunk_size ({chunk_size})"
        )

    if not text or not text.strip():
        return []

    # --- 1. Sentence segmentation -------------------------------------------
    # Split on ". " to get fragments; restore the trailing period on all but
    # the final fragment so re-joined text is grammatically intact.
    raw_fragments = text.split(". ")
    sentences: list[str] = []
    for i, frag in enumerate(raw_fragments):
        frag = frag.strip()
        if not frag:
            continue
        # Re-add the period that was consumed by split(), except on the last
        # fragment which may already end with punctuation.
        if i < len(raw_fragments) - 1 and not frag.endswith("."):
            frag = frag + "."
        sentences.append(frag)

    if not sentences:
        return []

    # --- 2. Window accumulation ---------------------------------------------
    chunks: list[str] = []
    current_words: list[str] = []

    for sentence in sentences:
        sentence_words = sentence.split()

        # Hard-split sentences that exceed chunk_size on their own.
        if len(sentence_words) > chunk_size:
            # Flush whatever is in the current window first.
            if current_words:
                chunks.append(" ".join(current_words))
                current_words = current_words[-overlap:]

            # Then emit the long sentence in chunk_size slices.
            for word_start in range(0, len(sentence_words), chunk_size - overlap):
                slice_words = sentence_words[word_start : word_start + chunk_size]
                if slice_words:
                    chunks.append(" ".join(slice_words))
            # Seed next window with the tail of the last long-sentence slice.
            current_words = sentence_words[-(overlap):]
            continue

        # Normal case: would adding this sentence overflow the window?
        if len(current_words) + len(sentence_words) > chunk_size and current_words:
            chunks.append(" ".join(current_words))
            # Seed next window with overlap tail of the emitted window.
            current_words = current_words[-overlap:] + sentence_words
        else:
            current_words.extend(sentence_words)

    # Flush the final window.
    if current_words:
        chunks.append(" ".join(current_words))

    # --- 3. Filter empty strings (defensive) --------------------------------
    return [c for c in chunks if c.strip()]
