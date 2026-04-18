"""Seed staleness review — cross-reference seeded rows against ingested evidence.

The orchestrator is :func:`app.services.review.seed_staleness.run_seed_review`.
Each per-type reviewer lives in :mod:`app.services.review.reviewers`.
"""
