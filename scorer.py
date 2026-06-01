"""
Auto-score pending predictions using Manifold Markets + Gemini with Google Search.

Usage:
    python scorer.py                    # score all unscored pending predictions
    python scorer.py --dry-run          # preview without writing to DB
    python scorer.py --min-confidence 0.8
"""
import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DATABASE = os.path.join(os.path.dirname(__file__), 'bingo.db')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')


def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def ensure_suggestions_table(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER NOT NULL UNIQUE,
            source TEXT NOT NULL,
            suggested_status TEXT NOT NULL,
            confidence REAL,
            evidence TEXT,
            market_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (prediction_id) REFERENCES predictions(id)
        );
    ''')
    db.commit()


def parse_json_response(text):
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def search_manifold(query):
    """Search Manifold Markets for a resolved market matching the prediction."""
    try:
        resp = requests.get(
            'https://api.manifold.markets/v0/search-markets',
            params={'term': query, 'sort': 'score', 'limit': 5},
            timeout=10
        )
        if resp.status_code != 200:
            return None
        for market in resp.json():
            if market.get('isResolved') and market.get('resolution') in ('YES', 'NO'):
                return market
        return None
    except Exception:
        return None


def interpret_manifold_match(prediction_text, market, client):
    """Ask Gemini if a Manifold market actually matches the prediction."""
    prompt = f"""Does this prediction market match my prediction? Answer in JSON only.

Prediction: "{prediction_text}"

Market question: "{market.get('question', '')}"
Market resolution: {market.get('resolution', '')} (YES=happened, NO=didn't happen)
Market URL: {market.get('url', '')}

Return JSON: {{"matches": true/false, "status": "success"|"fail"|"unclear", "confidence": 0.0-1.0, "evidence": "one sentence"}}
If the market question doesn't clearly map to the prediction, set matches to false."""

    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash', contents=prompt
        )
        return parse_json_response(response.text)
    except Exception:
        return None


def score_with_gemini_search(prediction_text, client):
    """Use Gemini + Google Search grounding to evaluate a prediction."""
    prompt = f"""Search the web and evaluate this 2026 prediction: "{prediction_text}"

Determine if it has clearly come true, clearly failed, or is still unresolved.

Return JSON only: {{"status": "success"|"fail"|"unclear", "confidence": 0.0-1.0, "evidence": "1-2 sentences with key facts"}}

- success: prediction clearly came true
- fail: clearly didn't happen or the opposite occurred
- unclear: too early, insufficient evidence, or still ongoing"""

    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        return parse_json_response(response.text)
    except Exception as e:
        return {'status': 'unclear', 'confidence': 0.0, 'evidence': f'Search failed: {e}'}


def score_prediction(prediction_text, client):
    """Try Manifold first, fall back to Gemini search."""
    market = search_manifold(prediction_text)
    if market:
        result = interpret_manifold_match(prediction_text, market, client)
        if result and result.get('matches') and result.get('confidence', 0) >= 0.75:
            return {
                'source': 'manifold',
                'suggested_status': result['status'],
                'confidence': result['confidence'],
                'evidence': result.get('evidence', ''),
                'market_url': market.get('url', ''),
            }

    result = score_with_gemini_search(prediction_text, client)
    return {
        'source': 'gemini_search',
        'suggested_status': result.get('status', 'unclear'),
        'confidence': result.get('confidence', 0.0),
        'evidence': result.get('evidence', ''),
        'market_url': '',
    }


def main():
    parser = argparse.ArgumentParser(description='Auto-score pending bingo predictions.')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing to DB')
    parser.add_argument('--min-confidence', type=float, default=0.70,
                        help='Minimum confidence to save a suggestion (default: 0.70)')
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        print('Error: GEMINI_API_KEY not set in .env')
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    db = get_db()
    ensure_suggestions_table(db)

    pending = db.execute('''
        SELECT p.id, p.text, b.name
        FROM predictions p
        JOIN boards b ON b.id = p.board_id
        LEFT JOIN suggestions s ON s.prediction_id = p.id
        WHERE p.status = 'pending' AND s.id IS NULL
        ORDER BY p.text
    ''').fetchall()

    if not pending:
        print('Nothing to score — all pending predictions already have suggestions.')
        return

    print(f'Scoring {len(pending)} predictions (min confidence: {args.min_confidence:.0%})...\n')

    text_cache = {}  # dedup API calls for identical prediction texts
    saved = skipped = errors = 0

    for i, pred in enumerate(pending, 1):
        text = pred['text']
        label = f'[{i}/{len(pending)}]'
        print(f'{label} {text[:65]}')

        if text in text_cache:
            result = text_cache[text]
            print(f'         ↳ (cached) {result["suggested_status"]} '
                  f'({result["confidence"]:.0%}) via {result["source"]}')
        else:
            try:
                result = score_prediction(text, client)
                text_cache[text] = result
                print(f'         ↳ {result["suggested_status"]} ({result["confidence"]:.0%}) '
                      f'via {result["source"]}')
            except Exception as e:
                print(f'         ↳ ERROR: {e}')
                errors += 1
                continue

        if result['confidence'] < args.min_confidence:
            print(f'         ↳ skipped (below {args.min_confidence:.0%} threshold)')
            skipped += 1
            continue

        if args.dry_run:
            saved += 1
        else:
            db.execute(
                'INSERT OR IGNORE INTO suggestions '
                '(prediction_id, source, suggested_status, confidence, evidence, market_url, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (pred['id'], result['source'], result['suggested_status'],
                 result['confidence'], result['evidence'], result['market_url'],
                 datetime.utcnow().isoformat())
            )
            db.commit()
            saved += 1

        time.sleep(0.4)

    suffix = ' (dry run — nothing written)' if args.dry_run else ''
    print(f'\nDone{suffix}. Saved: {saved}, skipped (low confidence): {skipped}, errors: {errors}')


if __name__ == '__main__':
    main()
