import hashlib
import json
import random
import re
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import (Flask, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

DATABASE_URL = os.environ.get('DATABASE_URL', '')
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', '')

ADJECTIVES = [
    'amber', 'azure', 'bold', 'brave', 'bright', 'calm', 'clever', 'cool',
    'cosmic', 'crimson', 'daring', 'deft', 'dusty', 'eager', 'electric',
    'emerald', 'fierce', 'fleet', 'frosty', 'gentle', 'gilded', 'glowing',
    'golden', 'grand', 'hollow', 'icy', 'indigo', 'iron', 'ivory', 'jade',
    'keen', 'kind', 'lofty', 'lone', 'lucky', 'lunar', 'mellow', 'misty',
    'noble', 'pale', 'proud', 'quick', 'quiet', 'rapid', 'rare', 'regal',
    'rough', 'royal', 'rusty', 'sage', 'sandy', 'scarlet', 'serene', 'sharp',
    'silent', 'silver', 'sleek', 'slim', 'slow', 'smart', 'smooth', 'snowy',
    'solar', 'solid', 'somber', 'stark', 'steel', 'stern', 'still', 'stone',
    'storm', 'strange', 'strong', 'subtle', 'sunny', 'swift', 'tall', 'tame',
    'teal', 'thin', 'thorn', 'timid', 'tough', 'true', 'vast', 'velvet',
    'vivid', 'warm', 'wild', 'wise', 'worn', 'young', 'zealous', 'frozen',
    'neon', 'obsidian', 'pewter', 'russet', 'verdant',
]

NOUNS = [
    'anchor', 'anvil', 'arrow', 'atlas', 'bark', 'beam', 'bear', 'bell',
    'blade', 'bolt', 'brook', 'canyon', 'cape', 'cedar', 'chain', 'cliff',
    'cloud', 'coal', 'coast', 'comet', 'coral', 'crane', 'creek', 'crest',
    'crow', 'crown', 'dale', 'dart', 'dawn', 'delta', 'dome', 'dove',
    'dusk', 'dust', 'eagle', 'echo', 'elm', 'ember', 'falcon', 'fern',
    'field', 'fjord', 'flame', 'flint', 'flood', 'fog', 'forge', 'frost',
    'gale', 'gate', 'glade', 'glen', 'glow', 'gorge', 'grove', 'gull',
    'hawk', 'haze', 'helm', 'hill', 'horn', 'hull', 'keel', 'knoll',
    'lake', 'lance', 'lark', 'lava', 'leaf', 'ledge', 'light', 'loch',
    'mast', 'maze', 'mesa', 'mist', 'moon', 'moss', 'mount', 'oak',
    'orbit', 'otter', 'peak', 'pine', 'plume', 'pond', 'pool', 'prism',
    'quill', 'rain', 'rapid', 'raven', 'reed', 'reef', 'ridge', 'rift',
    'ring', 'ripple', 'river', 'rock', 'root', 'rune', 'sail', 'sand',
    'shard', 'shore', 'shroud', 'silt', 'slate', 'slope', 'snow', 'spark',
    'spire', 'spray', 'spring', 'star', 'stem', 'storm', 'stream', 'summit',
    'surge', 'sword', 'thorn', 'tide', 'timber', 'torch', 'trail', 'vale',
    'vapor', 'vine', 'void', 'wake', 'wave', 'wind', 'wolf', 'wood',
]


def generate_phrase():
    return f"{random.choice(ADJECTIVES)}-{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"


class _Db:
    """Thin psycopg2 wrapper matching the sqlite3 execute/fetchone/fetchall pattern."""
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
    if 'db' not in g:
        g.db = _Db(psycopg2.connect(DATABASE_URL))
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        for stmt in [
            '''CREATE TABLE IF NOT EXISTS boards (
                id SERIAL PRIMARY KEY,
                phrase TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                board_id INTEGER NOT NULL REFERENCES boards(id),
                position INTEGER NOT NULL,
                text TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                updated_at TIMESTAMP
            )''',
            '''CREATE TABLE IF NOT EXISTS suggestions (
                id SERIAL PRIMARY KEY,
                prediction_id INTEGER NOT NULL UNIQUE REFERENCES predictions(id),
                source TEXT NOT NULL,
                suggested_status TEXT NOT NULL,
                confidence REAL,
                evidence TEXT,
                market_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''',
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


def get_bingo_lines(statuses):
    completed = {i for i, s in enumerate(statuses) if s in ('success', 'free')}
    winners = set()
    for row in range(5):
        line = [row * 5 + col for col in range(5)]
        if all(i in completed for i in line):
            winners.update(line)
    for col in range(5):
        line = [row * 5 + col for row in range(5)]
        if all(i in completed for i in line):
            winners.update(line)
    for line in ([0, 6, 12, 18, 24], [4, 8, 12, 16, 20]):
        if all(i in completed for i in line):
            winners.update(line)
    return winners


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/find', methods=['POST'])
def find():
    phrase = request.form.get('phrase', '').strip().lower()
    if not phrase:
        flash('Please enter your secret phrase.')
        return redirect(url_for('index'))
    return redirect(url_for('board', phrase=phrase))


@app.route('/create', methods=['GET', 'POST'])
def create():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        raw = [request.form.get(f'pred_{i}', '').strip() for i in range(24)]

        errors = []
        if not name:
            errors.append('Name is required.')
        if not email or '@' not in email:
            errors.append('A valid email is required.')
        if any(not p for p in raw):
            errors.append('All 24 prediction fields are required.')
        normalized = [p.lower() for p in raw if p]
        if len(normalized) != len(set(normalized)):
            errors.append('Each prediction must be unique — remove any duplicates.')

        if errors:
            for e in errors:
                flash(e)
            return render_template('create.html', form_data=request.form)

        db = get_db()
        phrase = None
        for _ in range(20):
            candidate = generate_phrase()
            if not db.execute('SELECT id FROM boards WHERE phrase = %s', (candidate,)).fetchone():
                phrase = candidate
                break

        if not phrase:
            flash('Could not generate a unique phrase. Please try again.')
            return render_template('create.html', form_data=request.form)

        row = db.execute(
            'INSERT INTO boards (phrase, name, email) VALUES (%s, %s, %s) RETURNING id',
            (phrase, name, email)
        ).fetchone()
        board_id = row['id']

        pred_index = 0
        for pos in range(25):
            if pos == 12:
                db.execute(
                    "INSERT INTO predictions (board_id, position, text, status) VALUES (%s, %s, %s, 'free')",
                    (board_id, pos, 'FREE SPACE')
                )
            else:
                db.execute(
                    'INSERT INTO predictions (board_id, position, text) VALUES (%s, %s, %s)',
                    (board_id, pos, raw[pred_index])
                )
                pred_index += 1

        db.commit()
        return redirect(url_for('board', phrase=phrase, new='1'))

    return render_template('create.html', form_data={})


@app.route('/board/<phrase>')
def board(phrase):
    db = get_db()
    board_row = db.execute('SELECT * FROM boards WHERE phrase = %s', (phrase,)).fetchone()
    if not board_row:
        flash("Board not found. Double-check your secret phrase.")
        return redirect(url_for('index'))
    preds = db.execute(
        'SELECT * FROM predictions WHERE board_id = %s ORDER BY position',
        (board_row['id'],)
    ).fetchall()
    statuses = [p['status'] for p in preds]
    bingo_cells = get_bingo_lines(statuses)
    has_bingo = len(bingo_cells) > 0
    is_new = request.args.get('new') == '1'
    return render_template('board.html', board=board_row, predictions=preds,
                           bingo_cells=bingo_cells, has_bingo=has_bingo, is_new=is_new)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin'):
        return redirect(url_for('admin_home'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        provided = hashlib.sha256(password.encode()).hexdigest()
        if ADMIN_PASSWORD_HASH and provided == ADMIN_PASSWORD_HASH:
            session['admin'] = True
            return redirect(url_for('admin_home'))
        flash('Incorrect password.')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None)
    return redirect(url_for('index'))


@app.route('/admin')
def admin_home():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    db = get_db()
    boards = db.execute(
        "SELECT b.*, COUNT(p.id) FILTER (WHERE p.status = 'success') AS hits "
        'FROM boards b LEFT JOIN predictions p ON p.board_id = b.id '
        'GROUP BY b.id ORDER BY b.created_at DESC'
    ).fetchall()
    return render_template('admin_boards.html', boards=boards)


@app.route('/admin/board/<int:board_id>')
def admin_board(board_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    db = get_db()
    board_row = db.execute('SELECT * FROM boards WHERE id = %s', (board_id,)).fetchone()
    if not board_row:
        flash('Board not found.')
        return redirect(url_for('admin_home'))
    preds = db.execute(
        'SELECT p.*, s.id AS sug_id, s.source AS sug_source, '
        's.suggested_status, s.confidence, s.evidence, s.market_url '
        'FROM predictions p '
        'LEFT JOIN suggestions s ON s.prediction_id = p.id '
        'WHERE p.board_id = %s ORDER BY p.position',
        (board_id,)
    ).fetchall()
    statuses = [p['status'] for p in preds]
    bingo_cells = get_bingo_lines(statuses)
    has_bingo = len(bingo_cells) > 0
    return render_template('admin_board.html', board=board_row, predictions=preds,
                           bingo_cells=bingo_cells, has_bingo=has_bingo)


@app.route('/admin/board/<int:board_id>/prediction/<int:pred_id>', methods=['POST'])
def update_prediction(board_id, pred_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    status = request.form.get('status')
    if status not in ('pending', 'success', 'fail'):
        flash('Invalid status.')
        return redirect(url_for('admin_board', board_id=board_id))
    db = get_db()
    db.execute(
        "UPDATE predictions SET status = %s, updated_at = %s "
        "WHERE id = %s AND board_id = %s AND status != 'free'",
        (status, datetime.utcnow().isoformat(), pred_id, board_id)
    )
    db.execute('DELETE FROM suggestions WHERE prediction_id = %s', (pred_id,))
    db.commit()
    return redirect(url_for('admin_board', board_id=board_id))


@app.route('/admin/suggestions/<int:sug_id>/apply', methods=['POST'])
def apply_suggestion(sug_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    db = get_db()
    sug = db.execute(
        'SELECT s.*, p.board_id FROM suggestions s '
        'JOIN predictions p ON p.id = s.prediction_id WHERE s.id = %s',
        (sug_id,)
    ).fetchone()
    if not sug:
        flash('Suggestion not found.')
        return redirect(url_for('admin_home'))
    board_id = sug['board_id']
    db.execute(
        "UPDATE predictions SET status = %s, updated_at = %s WHERE id = %s AND status != 'free'",
        (sug['suggested_status'], datetime.utcnow().isoformat(), sug['prediction_id'])
    )
    db.execute('DELETE FROM suggestions WHERE id = %s', (sug_id,))
    db.commit()
    return redirect(url_for('admin_board', board_id=board_id))


@app.route('/admin/suggestions/<int:sug_id>/dismiss', methods=['POST'])
def dismiss_suggestion(sug_id):
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    db = get_db()
    sug = db.execute(
        'SELECT s.*, p.board_id FROM suggestions s '
        'JOIN predictions p ON p.id = s.prediction_id WHERE s.id = %s',
        (sug_id,)
    ).fetchone()
    board_id = sug['board_id'] if sug else None
    db.execute('DELETE FROM suggestions WHERE id = %s', (sug_id,))
    db.commit()
    if board_id:
        return redirect(url_for('admin_board', board_id=board_id))
    return redirect(url_for('admin_home'))


@app.route('/admin/clusters')
def admin_clusters():
    if not session.get('admin'):
        return redirect(url_for('admin_login'))
    db = get_db()
    row = db.execute('SELECT result_json, created_at FROM cluster_cache WHERE id = 1').fetchone()
    clusters_data = None
    generated_at = None
    if row:
        clusters_data = json.loads(row['result_json'])
        generated_at = row['created_at']
    return render_template('admin_clusters.html', data=clusters_data, generated_at=generated_at)


_STOP_WORDS = {
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'is', 'are', 'will', 'that', 'this', 'have', 'has', 'be',
    'from', 'by', 'as', 'into', 'out', 'over', 'not', 'its', 'their',
    'than', 'more', 'most', 'also', 'does', 'did', 'was', 'were', 'been',
    'would', 'could', 'should', 'which', 'who', 'what', 'when', 'where',
}


@app.route('/api/similar')
def api_similar():
    q = request.args.get('q', '').strip()
    if len(q) < 8:
        return jsonify({'count': 0, 'examples': []})

    words = [w for w in re.findall(r'\b[a-zA-Z]{4,}\b', q.lower())
             if w not in _STOP_WORDS]
    if not words:
        return jsonify({'count': 0, 'examples': []})

    db = get_db()
    placeholders = ' OR '.join(["LOWER(p.text) LIKE %s" for _ in words])
    params = [f'%{w}%' for w in words]

    rows = db.execute(
        f"SELECT p.text, p.board_id FROM predictions p "
        f"WHERE p.status != 'free' AND ({placeholders})",
        params
    ).fetchall()

    board_ids = {r['board_id'] for r in rows}
    seen = set()
    examples = []
    for r in rows:
        if r['text'] not in seen and r['text'].lower() != q.lower():
            seen.add(r['text'])
            examples.append(r['text'])
        if len(examples) == 3:
            break

    return jsonify({'count': len(board_ids), 'examples': examples})


with app.app_context():
    if DATABASE_URL:
        init_db()

if __name__ == '__main__':
    app.run(debug=True)
