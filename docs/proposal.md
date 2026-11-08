# Paper Trail: A Multi-Agent Receipt and Price Tracker

Project Proposal, October 2026

## Overview

Paper Trail is a web app that turns photos of receipts into a searchable record of purchases: what was bought, where, and at what price, with a price history for each item. It runs as a single Python container on a home server. The agents are built with LangGraph for its explicit control over agent flow, retry loops and saved state. Models run through Ollama on a desktop GPU (RTX 3080); because it is not always on, uploads wait in a queue that is processed whenever the GPU is available. All data is stored in one SQLite database with the sqlite-vec extension, so everything stays on private hardware.

## Orchestration

A supervisor agent manages each receipt through a shared state, assigns work to four specialist agents, and uses their results to decide whether to retry, re-read the image with a vision model, or send the receipt to manual review:

- The Extraction agent reads the receipt and outputs store, date, items, prices, tax and total.
- The Validation agent checks that the amounts add up and, if not, returns the receipt to Extraction with specific feedback.
- The Product Matching agent maps store abbreviations such as "KS ORG EGGS 24CT" to a consistent product, so items can be tracked across receipts and stores.
- The Enrichment agent adds category, brand, size, unit price and search tags, so items can be found by words not on the receipt.

## Prompting

Each agent has its own role prompt with a fixed JSON output schema and examples of difficult receipts. Validation failures are fed back as specific corrections rather than generic retries, and all agents treat receipt text as data, not instructions.

## Tool Use

Tools are served through an MCP server: OCR, an arithmetic checker, product lookup and similarity search, a database writer and a review-queue tool. Each agent can call only the tools its role needs.

## Memory

Short-term memory is the shared state saved by LangGraph's SQLite checkpointer, so an interrupted receipt resumes where it stopped. Long-term memory stores corrections to product names and categories, so repeated items are matched from memory instead of being inferred again. New items are matched using RAG (similarity search over known products), so fewer receipts should need review over time.

## Guardrails

Totals are checked in code rather than by the model, outputs must match a fixed schema and category list, and low-confidence results go to a review queue instead of being saved. Card numbers are masked before storage, and the app is not exposed to the internet: remote access requires WireGuard.

## Evaluation

About 50 receipts will be hand-labeled to measure extraction, matching and categorization accuracy. Results will be compared with a single-prompt baseline and re-measured after a few weeks to see whether matching improves as memory grows.
