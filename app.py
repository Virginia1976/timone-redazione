"""
Timone Manager — Redazione
App separata per la redazione: inserimento timoni senza archivio foto.
Porta: 5010
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import re
import subprocess

import requests
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for
from PIL import Image

load_dotenv(pathlib.Path(__file__).parent / '.env')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', '')
app.permanent_session_lifetime = __import__('datetime').timedelta(days=365)

PASSWORD_REDAZIONE = os.environ.get('PASSWORD_REDAZIONE', '')
PASSWORD_EDITOR    = os.environ.get('PASSWORD_EDITOR', '')

DATA_DIR        = pathlib.Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
EDITORIALE_BASE = os.environ.get('EDITORIALE_BASE', '/Volumes/EDITORIALE')

VALID_KEY = re.compile(r'^[a-z0-9_]{1,60}$')

TIMONI_META = {
    'nuovotv':   {'label': 'NuovoTV',     'percorso': 'NUOVOTV',   'tipo': 'tv'},
    'dipiutv':   {'label': 'DipiùTV',     'percorso': 'DIPIUTV',   'tipo': 'tv'},
    'tvmia':     {'label': 'TvMia',       'percorso': 'TVMIA',     'tipo': 'tv'},
    'nuovo':     {'label': 'Nuovo',       'percorso': 'NUOVO',     'tipo': 'rivista'},
    'dipiu':     {'label': 'Dipiu',       'percorso': 'DIPIU',     'tipo': 'rivista'},
    'divadonna': {'label': 'Diva e Donna','percorso': 'DIVADONNA', 'tipo': 'rivista'},
}

# ── Sessione et50 (autocomplete titoli) ───────────────────────────────────────
BASE_URL  = 'https://www.infotv.it'
ET50_BASE = 'https://www.infotv.it/et50-server/api'
USERNAME  = 'hubcom'
PASSWORD  = 'hubcom.infotv'

_et50 = requests.Session()
_et50.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Content-Type': 'application/json',
    'Origin': BASE_URL,
    'Referer': 'https://www.infotv.it/et50-server/palinsesti',
})
_et50_ok = False


def et50_login() -> bool:
    global _et50_ok
    try:
        r = _et50.post(f'{ET50_BASE}/login',
                       json={'username': USERNAME, 'password': PASSWORD},
                       timeout=15)
        token = r.headers.get('Authorization', '')
        if token:
            _et50.headers['Authorization'] = token
            _et50_ok = True
        else:
            _et50_ok = False
    except Exception as e:
        print(f'[ET50] Login fallito: {e}')
        _et50_ok = False
    return _et50_ok


# ── Sessione PRJDIA (autocomplete personaggi) ─────────────────────────────────
PRJDIA_LOGIN_API  = 'https://www.infotv.it/infotv-portal/rs/auth/login'
PRJDIA_SEARCH_API = 'https://www.infotv.it/afo/rs/ricerca'

_prjdia = requests.Session()
_prjdia.headers.update({
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept':       'application/json, text/plain, */*',
    'Content-Type': 'application/json',
    'Origin':  BASE_URL,
    'Referer': 'https://www.infotv.it/prjdia/ricerca',
})
_prjdia_ok = False


def prjdia_login() -> bool:
    global _prjdia_ok
    try:
        r = _prjdia.post(PRJDIA_LOGIN_API,
                         json={'username': USERNAME, 'password': PASSWORD},
                         timeout=15)
        token = r.headers.get('Authorization', '')
        if token:
            _prjdia.headers['Authorization'] = token
            _prjdia_ok = True
        else:
            _prjdia_ok = False
    except Exception as e:
        print(f'[PRJDIA] Login fallito: {e}')
        _prjdia_ok = False
    return _prjdia_ok


@app.before_request
def require_login():
    if request.endpoint in ('login', 'logout', 'static'):
        return
    if not session.get('ruolo'):
        if request.is_json or not request.accept_mimetypes.accept_html:
            return jsonify({'error': 'non autenticato'}), 401
        return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw and pw == PASSWORD_EDITOR:
            session.permanent = True
            session['ruolo'] = 'editor'
            return redirect(url_for('index'))
        elif pw and pw == PASSWORD_REDAZIONE:
            session.permanent = True
            session['ruolo'] = 'redazione'
            return redirect(url_for('index'))
        else:
            error = 'Password errata'
    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/ruolo')
def get_ruolo():
    return jsonify({'ruolo': session.get('ruolo', '')})


def _safe_path(cartella: str, codice: str) -> str | None:
    """Restituisce il path assoluto solo se non contiene traversal."""
    if '..' in cartella or '..' in codice or not cartella or not codice:
        return None
    return os.path.join(cartella, codice + '.tif')


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/save/<key>', methods=['POST'])
def save(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    data = request.get_json(force=True, silent=True) or {}
    path = DATA_DIR / f'{key}.json'
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return jsonify({'ok': True})


@app.route('/api/load/<key>')
def load(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    path = DATA_DIR / f'{key}.json'
    if path.exists():
        return jsonify(json.loads(path.read_text('utf-8')))
    return jsonify({'rows': []})


@app.route('/api/mtime/<key>')
def data_mtime(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    path = DATA_DIR / f'{key}.json'
    return jsonify({'mtime': path.stat().st_mtime if path.exists() else None})


@app.route('/api/list')
def list_timoni():
    saved = {}
    for f in DATA_DIR.glob('*.json'):
        try:
            d = json.loads(f.read_text('utf-8'))
            rows = d.get('rows', [])
            saved[f.stem] = {
                'total':  len(rows),
                'gialli': sum(1 for r in rows if r.get('colore') == 'giallo'),
            }
        except Exception:
            pass
    return jsonify(saved)


@app.route('/autocomplete_titoli', methods=['POST'])
def autocomplete_titoli():
    global _et50_ok
    data = request.json or {}
    q = data.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    if not _et50_ok:
        et50_login()

    def _query():
        return _et50.post(f'{ET50_BASE}/vfilmpgmtv/custom/findAllCustom',
                          json={'term': q}, timeout=10)
    try:
        r = _query()
        if r.status_code == 401:
            _et50_ok = False
            et50_login()
            r = _query()
        if r.status_code != 200:
            return jsonify([])
        suggestions = [
            {
                'titolo':   item.get('titolo', ''),
                'tipo':     item.get('tipo', ''),
                'codtipo':  item.get('codtipo', ''),
                'anno':     item.get('annoedizione'),
                'edizione': item.get('edizione', ''),
            }
            for item in r.json()[:20]
            if item.get('titolo')
        ]
        return jsonify(suggestions)
    except Exception as e:
        print(f'[AC] Errore: {e}')
        return jsonify([])


@app.route('/autocomplete_personaggi', methods=['POST'])
def autocomplete_personaggi():
    global _prjdia_ok
    data = request.json or {}
    q = data.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])
    if not _prjdia_ok:
        prjdia_login()

    body = {
        "text": q,
        "film": False, "tv": False, "personaggi": True, "sport": False,
        "filter": "1", "scheda": True, "foto": False,
        "tvmenu": False, "onlyfoto": False, "usableByTvmenu": False,
        "incomplete": False, "parameters": [], "area": None,
    }

    def _query():
        return _prjdia.post(PRJDIA_SEARCH_API, json=body, timeout=10)

    try:
        r = _query()
        if r.status_code == 401:
            _prjdia_ok = False
            prjdia_login()
            r = _query()
        if r.status_code != 200:
            return jsonify([])
        items = r.json().get('listaSchedePersonaggi', [])
        suggestions = [
            {
                'nome':        (item.get('nomearte') or item.get('nomeanagrafico') or '').strip(),
                'professione': (item.get('professione') or '').strip(),
            }
            for item in items[:20]
            if (item.get('nomearte') or item.get('nomeanagrafico'))
        ]
        return jsonify(suggestions)
    except Exception as e:
        print(f'[AC-P] Errore: {e}')
        return jsonify([])


@app.route('/cartelle')
def cartelle():
    timone_id = request.args.get('timone', '').strip()
    meta = TIMONI_META.get(timone_id)
    if not meta:
        return jsonify({'cartelle': []})
    base = os.path.join(EDITORIALE_BASE, meta['percorso'])
    if not os.path.isdir(base):
        return jsonify({'cartelle': []})
    items = []
    try:
        for entry in sorted(os.scandir(base), key=lambda e: e.name, reverse=True):
            if entry.is_dir() and not entry.name.startswith('.'):
                items.append({'nome': entry.name, 'percorso': entry.path})
    except PermissionError:
        pass
    return jsonify({'cartelle': items})


@app.route('/tif_thumb')
def tif_thumb():
    cartella = request.args.get('cartella', '').strip()
    codice   = request.args.get('codice', '').strip()
    path = _safe_path(cartella, codice)
    if not path:
        return '', 400
    if not os.path.isfile(path):
        return '', 404
    try:
        with Image.open(path) as img:
            img.thumbnail((80, 60))
            buf = io.BytesIO()
            img.convert('RGB').save(buf, format='JPEG', quality=75)
            buf.seek(0)
            return Response(buf.read(), mimetype='image/jpeg',
                            headers={'Cache-Control': 'no-cache'})
    except Exception as e:
        print(f'[THUMB] {e}')
        return '', 500



@app.route('/open_tif', methods=['POST'])
def open_tif():
    if session.get('ruolo') != 'editor':
        return jsonify({'ok': False, 'error': 'non autorizzato'}), 403
    data     = request.json or {}
    cartella = data.get('cartella', '').strip()
    codice   = data.get('codice', '').strip()
    path = _safe_path(cartella, codice)
    if not path:
        return jsonify({'ok': False, 'error': 'parametri non validi'}), 400
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'path': path})
    try:
        return Response(
            open(path, 'rb').read(),
            mimetype='image/tiff',
            headers={
                'Content-Disposition': f'attachment; filename="{codice}.tif"',
                'Cache-Control': 'no-cache',
            }
        )
    except Exception as e:
        print(f'[OPEN_TIF] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/tif_mtime', methods=['POST'])
def tif_mtime():
    data  = request.json or {}
    files = data.get('files', [])
    result = {}
    for f in files:
        path = _safe_path(f.get('cartella', ''), f.get('codice', ''))
        codice = f.get('codice', '')
        if not path or not codice:
            continue
        try:
            result[codice] = os.path.getmtime(path) if os.path.isfile(path) else None
        except Exception:
            result[codice] = None
    return jsonify(result)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5010, debug=True)
