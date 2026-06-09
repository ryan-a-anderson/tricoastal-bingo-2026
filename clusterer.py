"""
Cluster all predictions by semantic similarity using Google text-embedding-004.

Usage:
    python clusterer.py              # auto k = sqrt(n/2)
    python clusterer.py --k 10       # force 10 clusters
    python clusterer.py --rebuild    # re-embed even cached texts

Results stored in DB at cluster_cache table; view at /admin/clusters.
"""
import argparse
import json
import math
import os
import time

import numpy as np
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from google import genai

load_dotenv()

DATABASE_URL = os.environ.get('DATABASE_URL', '')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')


class _Db:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_db():
    return _Db(psycopg2.connect(DATABASE_URL))


def ensure_tables(db):
    for stmt in [
        '''CREATE TABLE IF NOT EXISTS embedding_cache (
            text TEXT PRIMARY KEY,
            embedding TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS cluster_cache (
            id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            result_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
    ]:
        db.execute(stmt)
    db.commit()


def get_embedding(text, client, db, rebuild=False):
    """Fetch embedding from cache or API."""
    if not rebuild:
        row = db.execute('SELECT embedding FROM embedding_cache WHERE text = %s', (text,)).fetchone()
        if row:
            return json.loads(row['embedding'])

    result = client.models.embed_content(
        model='models/text-embedding-004',
        contents=text
    )
    emb = result.embeddings[0].values
    db.execute(
        'INSERT INTO embedding_cache (text, embedding, created_at) VALUES (%s, %s, NOW()) '
        'ON CONFLICT (text) DO UPDATE SET embedding = EXCLUDED.embedding, created_at = NOW()',
        (text, json.dumps(emb))
    )
    db.commit()
    return emb


def kmeans_plusplus(X, k, rng):
    """k-means++ centroid initialization."""
    idx = [rng.integers(len(X))]
    for _ in range(k - 1):
        dists = np.min(
            [np.sum((X - X[i]) ** 2, axis=1) for i in idx], axis=0
        )
        probs = dists / dists.sum()
        idx.append(rng.choice(len(X), p=probs))
    return X[idx].copy()


def kmeans(X, k, max_iter=150):
    rng = np.random.default_rng(42)
    centroids = kmeans_plusplus(X, k, rng)
    labels = np.zeros(len(X), dtype=int)

    for _ in range(max_iter):
        dists = np.array([np.sum((X - c) ** 2, axis=1) for c in centroids])
        new_labels = np.argmin(dists, axis=0)
        if np.all(new_labels == labels):
            break
        labels = new_labels
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = X[mask].mean(axis=0)

    return labels


def label_cluster(texts, client):
    sample = texts[:10]
    prompt = (
        'These predictions are grouped together by topic similarity:\n'
        + '\n'.join(f'- {t}' for t in sample)
        + '\n\nWrite a concise 2-5 word topic label. Return ONLY the label.'
    )
    response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
    return response.text.strip().strip('"\'')


def main():
    parser = argparse.ArgumentParser(description='Cluster bingo predictions by topic.')
    parser.add_argument('--k', type=int, default=0, help='Number of clusters (0 = auto)')
    parser.add_argument('--rebuild', action='store_true', help='Re-embed even cached texts')
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        print('Error: GEMINI_API_KEY not set in .env')
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    db = get_db()
    ensure_tables(db)

    rows = db.execute('''
        SELECT p.text,
               COUNT(DISTINCT p.board_id) AS board_count,
               SUM(CASE WHEN p.status = 'success' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN p.status = 'fail'    THEN 1 ELSE 0 END) AS fail_count
        FROM predictions p
        WHERE p.status != 'free'
        GROUP BY p.text
        ORDER BY board_count DESC
    ''').fetchall()

    texts = [r['text'] for r in rows]
    meta = {r['text']: dict(r) for r in rows}

    if len(texts) < 4:
        print(f'Need at least 4 unique predictions (have {len(texts)}).')
        return

    k = args.k or max(3, round(math.sqrt(len(texts) / 2)))
    k = min(k, len(texts))
    print(f'Found {len(texts)} unique predictions → {k} clusters\n')

    print('Embedding predictions...')
    embeddings = []
    for i, text in enumerate(texts, 1):
        emb = get_embedding(text, client, db, rebuild=args.rebuild)
        embeddings.append(emb)
        if i % 20 == 0:
            print(f'  {i}/{len(texts)}')
        time.sleep(0.05)
    print(f'  {len(texts)}/{len(texts)} done\n')

    X = np.array(embeddings, dtype=np.float32)

    print('Clustering...')
    labels = kmeans(X, k)

    print('Labeling clusters...')
    clusters = []
    for cluster_id in range(k):
        cluster_texts = [texts[i] for i in range(len(texts)) if labels[i] == cluster_id]
        if not cluster_texts:
            continue
        label = label_cluster(cluster_texts, client)
        predictions_data = sorted(
            [{'text': t,
              'boards': meta[t]['board_count'],
              'successes': meta[t]['success_count'],
              'fails': meta[t]['fail_count']}
             for t in cluster_texts],
            key=lambda x: -x['boards']
        )
        clusters.append({
            'label': label,
            'count': len(cluster_texts),
            'predictions': predictions_data,
        })
        print(f'  {label}: {len(cluster_texts)} predictions')

    clusters.sort(key=lambda c: -c['count'])

    result = {
        'clusters': clusters,
        'total_unique': len(texts),
        'k': k,
    }

    db.execute(
        'INSERT INTO cluster_cache (id, result_json, created_at) VALUES (1, %s, NOW()) '
        'ON CONFLICT (id) DO UPDATE SET result_json = EXCLUDED.result_json, created_at = NOW()',
        (json.dumps(result),)
    )
    db.commit()
    print(f'\nSaved. View at /admin/clusters')


if __name__ == '__main__':
    main()
