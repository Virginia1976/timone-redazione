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
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import timedelta, date as date_

import requests
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for, send_file
from PIL import Image

load_dotenv(pathlib.Path(__file__).parent / '.env')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-local-secret-key')
app.permanent_session_lifetime = timedelta(days=1)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Executor per operazioni I/O su volumi SMB: impone un timeout così un hang
# sulla rete non blocca il thread Flask a tempo indeterminato.
_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='smb-io')

def _smb_call(fn, timeout=8):
    return _io_pool.submit(fn).result(timeout=timeout)

EDITOR_USERNAME       = os.environ.get('EDITOR_USERNAME', '')
EDITOR_PASSWORD       = os.environ.get('EDITOR_PASSWORD', '')
REDAZIONE_USERNAME    = os.environ.get('REDAZIONE_USERNAME', '')
REDAZIONE_PASSWORD    = os.environ.get('REDAZIONE_PASSWORD', '')
ADMIN_USERNAME        = os.environ.get('ADMIN_USERNAME', '')
ADMIN_PASSWORD        = os.environ.get('ADMIN_PASSWORD', '')
PALINSESTI_USERNAME   = os.environ.get('PALINSESTI_USERNAME', 'palinsesti')
PALINSESTI_PASSWORD   = os.environ.get('PALINSESTI_PASSWORD', 'palinsesti')

_attivi_raw = os.environ.get('TIMONI_ATTIVI', '')
TIMONI_ATTIVI = [t.strip() for t in _attivi_raw.split(',') if t.strip()] if _attivi_raw else []

DATA_DIR        = pathlib.Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
WEEKS_DIR       = DATA_DIR / '_weeks'
WEEKS_DIR.mkdir(exist_ok=True)
WEEKS_CHIUSA_DIR = WEEKS_DIR / 'chiuse'
WEEKS_CHIUSA_DIR.mkdir(exist_ok=True)
EDITORIALE_BASE = os.environ.get('EDITORIALE_BASE', '/Volumes/EDITORIALE')
EDITORIALE_SMB  = os.environ.get('EDITORIALE_SMB', '')

VALID_KEY    = re.compile(r'^[a-z0-9_]{1,60}$')
KNOWN_TIMONI = {'nuovotv', 'dipiutv', 'tvmia', 'nuovo', 'dipiu', 'divadonna'}

TLP_SCONTORNI    = pathlib.Path(os.environ.get('TLP_SCONTORNI', '/Volumes/TLPserver/ARCHIVIO_FOTO/__SCONTORNI'))
TLP_DIPIU_FOTINE = TLP_SCONTORNI / 'DIPIU_fotine_sfondino'

CATEGORIE_TLP: dict[str, str] = {
    'film':        '__FILM',
    'serie':       '__SERIE',
    'cartoni':     '__CARTONI',
    'soap':        '__SOAP',
    'documentari': '__DOCUMENTARIO-DOCUFILM',
    'tv':          '__TV',
    'sport':       '__SPORT',
    'teatro':      '__TEATRO',
    'monumenti':   '__MONUMENTI - OGGETTI',
}

def strip_parens(name: str) -> str:
    return re.sub(r'\s*\([^)]*\)', '', name).strip()

_ARTICOLI = re.compile(
    r"^(?:il|lo|la|i|gli|le|un|una|uno|the|a|an)\s+|^l'",
    re.IGNORECASE,
)

def strip_articolo(titolo: str) -> str:
    return _ARTICOLI.sub('', titolo).strip()


def timone_from_key(key: str) -> str | None:
    for t in KNOWN_TIMONI:
        if key == t or key.startswith(t + '_'):
            return t
    return None


def _chiusa_flag_path(timone: str, week_id: str) -> pathlib.Path:
    return WEEKS_CHIUSA_DIR / f'{timone}_{week_id}'

def is_week_chiusa(timone: str, week_id: str) -> bool:
    return bool(week_id) and _chiusa_flag_path(timone, week_id).exists()

def set_week_chiusa_flag(timone: str, week_id: str, chiusa: bool) -> None:
    p = _chiusa_flag_path(timone, week_id)
    if chiusa:
        p.touch()
    else:
        p.unlink(missing_ok=True)


def get_week_meta(timone: str) -> dict:
    path = WEEKS_DIR / f'{timone}.json'
    if path.exists():
        try:
            meta = json.loads(path.read_text('utf-8'))
            week_id = meta.get('week_id', '')
            meta['chiusa'] = is_week_chiusa(timone, week_id)
            return meta
        except Exception:
            pass
    return {'dal': '', 'al': '', 'week_id': '', 'chiusa': False}


def set_week_meta(timone: str, meta: dict) -> None:
    (WEEKS_DIR / f'{timone}.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8'
    )


def week_data_dir(timone: str) -> pathlib.Path:
    wid = (get_week_meta(timone).get('week_id') or '').strip()
    if not wid:
        return DATA_DIR   # compatibilità con file esistenti nella root
    d = DATA_DIR / wid
    d.mkdir(exist_ok=True)
    return d


def resolve_week_dir(timone: str) -> pathlib.Path:
    """Risolve la directory settimana da ?week= nella query string (per-utente),
    con fallback alla settimana globale corrente."""
    wid = request.args.get('week', '').strip()
    if wid and re.match(r'^\d{4}-\d{2}-\d{2}$', wid):
        d = DATA_DIR / wid
        d.mkdir(exist_ok=True)
        return d
    return week_data_dir(timone)

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


def _invert_nome(s: str) -> str:
    """Converte 'COGNOME NOME' → 'Nome Cognome' (formato DB infotv)."""
    parts = s.strip().split()
    if len(parts) < 2:
        return s.title()
    nome, cognome = parts[-1], ' '.join(parts[:-1])
    return f"{nome.title()} {cognome.title()}"


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


def _sola_lettura():
    return session.get('ruolo') == 'palinsesti'


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
        u = request.form.get('username', '')
        pw = request.form.get('password', '')
        if u and pw and ADMIN_USERNAME and u == ADMIN_USERNAME and pw == ADMIN_PASSWORD:
            session.permanent = True
            session['ruolo'] = 'admin'
            return redirect(url_for('index'))
        elif u and pw and u == EDITOR_USERNAME and pw == EDITOR_PASSWORD:
            session.permanent = True
            session['ruolo'] = 'editor'
            return redirect(url_for('index'))
        elif u and pw and u == REDAZIONE_USERNAME and pw == REDAZIONE_PASSWORD:
            session.permanent = True
            session['ruolo'] = 'redazione'
            return redirect(url_for('index'))
        elif u and pw and u == PALINSESTI_USERNAME and pw == PALINSESTI_PASSWORD:
            session.permanent = True
            session['ruolo'] = 'palinsesti'
            return redirect(url_for('index'))
        else:
            error = 'Credenziali non valide'
    return render_template('login.html', error=error)


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/config')
def get_config():
    ruolo = session.get('ruolo', '')
    timoni = list(KNOWN_TIMONI) if ruolo == 'admin' else TIMONI_ATTIVI
    return jsonify({
        'ruolo':           ruolo,
        'timoni_attivi':   timoni,
        'editoriale_base': EDITORIALE_BASE,
        'editoriale_smb':  EDITORIALE_SMB,
    })


def _safe_path(cartella: str, codice: str) -> str | None:
    """Restituisce il path assoluto solo se non contiene traversal."""
    if '..' in cartella or '..' in codice or not cartella or not codice:
        return None
    return os.path.join(cartella, codice + '.tif')


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


def _atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix('.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _is_global_week_chiusa(timone: str) -> bool:
    """True se la settimana che si sta accedendo è chiusa (flag per-settimana)."""
    wid_param = request.args.get('week', '').strip()
    if wid_param and re.match(r'^\d{4}-\d{2}-\d{2}$', wid_param):
        return is_week_chiusa(timone, wid_param)
    return get_week_meta(timone).get('chiusa', False)


@app.route('/api/save/<key>', methods=['POST'])
def save(key):
    if _sola_lettura(): return jsonify({'error': 'Accesso in sola lettura'}), 403
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    if timone and _is_global_week_chiusa(timone):
        return jsonify({'error': 'settimana chiusa', 'chiusa': True}), 403
    data = request.get_json(force=True, silent=True) or {}
    d    = resolve_week_dir(timone) if timone else DATA_DIR
    path = d / f'{key}.json'
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return jsonify({'ok': True, 'mtime': path.stat().st_mtime})


@app.route('/api/patch/<key>', methods=['POST'])
def patch_rows(key):
    if _sola_lettura(): return jsonify({'error': 'Accesso in sola lettura'}), 403
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    if timone and _is_global_week_chiusa(timone):
        return jsonify({'error': 'settimana chiusa', 'chiusa': True}), 403
    body    = request.get_json(force=True, silent=True) or {}
    patches = body.get('patches', {})   # { codice: { campo: valore } }
    if not patches:
        return jsonify({'ok': True, 'mtime': None})
    d    = resolve_week_dir(timone) if timone else DATA_DIR
    path = d / f'{key}.json'
    try:
        existing = json.loads(path.read_text('utf-8')) if path.exists() else {'rows': []}
    except Exception:
        existing = {'rows': []}
    rows = existing.get('rows', [])
    applied = 0
    for codice, fields in patches.items():
        row = next((r for r in rows if r.get('codice') == codice), None)
        if row is not None:
            row.update(fields)
            applied += 1
    _atomic_write(path, json.dumps(existing, ensure_ascii=False, indent=2))
    return jsonify({'ok': True, 'mtime': path.stat().st_mtime, 'applied': applied})


_UPDATE_FIELDS = {'orario', 'titolo', 'tipo', 'personaggio', 'anno', 'stagione', 'note', 'trama'}

@app.route('/api/update/<key>', methods=['POST'])
def update_rows(key):
    if _sola_lettura(): return jsonify({'error': 'Accesso in sola lettura'}), 403
    """Aggiorna campi di testo per codice. Usato da carica_timone.
    Body: { "updates": [{ "codice": "...", "titolo": "...", ... }] }
    Risposta: { "ok": true, "updated": N }
    """
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    if timone and _is_global_week_chiusa(timone):
        return jsonify({'error': 'settimana chiusa', 'chiusa': True}), 403
    body    = request.get_json(force=True, silent=True) or {}
    updates = body.get('updates', [])
    if not updates:
        return jsonify({'error': 'nessun aggiornamento'}), 400
    d    = resolve_week_dir(timone) if timone else DATA_DIR
    path = d / f'{key}.json'
    try:
        existing = json.loads(path.read_text('utf-8')) if path.exists() else {'rows': []}
    except Exception:
        existing = {'rows': []}
    rows     = existing.get('rows', [])
    by_code  = {r.get('codice'): r for r in rows if r.get('codice')}
    updated  = 0
    for upd in updates:
        codice = upd.get('codice', '').strip()
        if not codice or codice not in by_code:
            continue
        row = by_code[codice]
        for field in _UPDATE_FIELDS:
            if field in upd:
                row[field] = upd[field]
        updated += 1
    if updated == 0:
        return jsonify({'error': 'nessun codice abbinato'}), 400
    _atomic_write(path, json.dumps(existing, ensure_ascii=False, indent=2))
    return jsonify({'ok': True, 'updated': updated})


@app.route('/api/load/<key>')
def load(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    d      = resolve_week_dir(timone) if timone else DATA_DIR
    path   = d / f'{key}.json'
    if path.exists():
        return jsonify(json.loads(path.read_text('utf-8')))
    return jsonify({'rows': []})


@app.route('/api/mtime/<key>')
def data_mtime(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    d      = resolve_week_dir(timone) if timone else DATA_DIR
    path   = d / f'{key}.json'
    return jsonify({'mtime': path.stat().st_mtime if path.exists() else None})


@app.route('/api/list')
def list_timoni():
    week_param = request.args.get('week', '').strip()
    saved = {}
    for timone in KNOWN_TIMONI:
        if week_param and re.match(r'^\d{4}-\d{2}-\d{2}$', week_param):
            wid = week_param
        else:
            wid = (get_week_meta(timone).get('week_id') or '').strip()
        if not wid:
            continue   # nessuna settimana impostata, nulla da contare
        d      = DATA_DIR / wid
        prefix = timone + '_'
        for f in d.glob('*.json'):
            # considera solo i file che appartengono a questo timone
            if not (f.stem == timone or f.stem.startswith(prefix)):
                continue
            try:
                data = json.loads(f.read_text('utf-8'))
                rows = data.get('rows', [])
                saved[f.stem] = {
                    'total':       len(rows),
                    'gialli':      sum(1 for r in rows if r.get('colore') == 'giallo'),
                    'da_lavorare': sum(1 for r in rows if not r.get('spunta') and not r.get('_separator')),
                }
            except Exception:
                pass
    return jsonify(saved)


# ── Gestione settimana ────────────────────────────────────────────────────────

@app.route('/api/week/<timone>', methods=['GET'])
def get_week(timone):
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    return jsonify(get_week_meta(timone))


@app.route('/api/week/<timone>', methods=['POST'])
def set_week(timone):
    if _sola_lettura(): return jsonify({'error': 'Accesso in sola lettura'}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    body    = request.get_json(force=True, silent=True) or {}
    dal     = body.get('dal', '').strip()
    al      = body.get('al', '').strip()
    current = get_week_meta(timone)
    new_wid = dal   # la data di inizio è l'identificatore della settimana
    changed = new_wid != current.get('week_id', '')
    new_meta = {'dal': dal, 'al': al, 'week_id': new_wid}
    set_week_meta(timone, new_meta)
    if new_wid:
        (DATA_DIR / new_wid).mkdir(exist_ok=True)
        set_week_chiusa_flag(timone, new_wid, False)  # impostare come globale rimuove il flag chiusa
    chiusa = is_week_chiusa(timone, new_wid) if new_wid else False
    return jsonify({'ok': True, 'changed': changed, 'chiusa': chiusa, **new_meta})


@app.route('/api/week/<timone>/chiudi', methods=['POST'])
def chiudi_week(timone):
    if _sola_lettura(): return jsonify({'error': 'Accesso in sola lettura'}), 403
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'error': 'Solo editor e admin possono chiudere le settimane'}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    week_id = get_week_meta(timone).get('week_id', '')
    if week_id:
        set_week_chiusa_flag(timone, week_id, True)
    return jsonify({'ok': True})


@app.route('/api/week/<timone>/chiudi-archiviata', methods=['POST'])
def chiudi_week_archiviata(timone):
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'error': 'Solo editor e admin possono chiudere le settimane'}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    body = request.get_json(force=True, silent=True) or {}
    week_id = body.get('week_id', '').strip()
    if not week_id or not re.match(r'^\d{4}-\d{2}-\d{2}$', week_id):
        return jsonify({'error': 'week_id non valido'}), 400
    if week_id == get_week_meta(timone).get('week_id', ''):
        return jsonify({'error': 'Usa /chiudi per la settimana corrente'}), 400
    set_week_chiusa_flag(timone, week_id, True)
    return jsonify({'ok': True})


@app.route('/api/copia-titoli/<key>')
def copia_titoli(key):
    """Restituisce i soli titoli (senza personaggi) dalla settimana precedente,
    pronti per essere importati come righe 'copiato' nella settimana corrente."""
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    if not timone:
        return jsonify({'error': 'timone non valido'}), 400

    current_wid = (get_week_meta(timone).get('week_id') or '').strip()

    # Cerca la cartella-settimana più recente precedente a quella corrente
    prev_wid = None
    try:
        candidates = sorted(
            [
                d.name for d in DATA_DIR.iterdir()
                if d.is_dir()
                and d.name != '_weeks'
                and re.match(r'^\d{4}-\d{2}-\d{2}$', d.name)
                and (not current_wid or d.name < current_wid)
            ],
            reverse=True,
        )
        if candidates:
            prev_wid = candidates[0]
    except Exception:
        pass

    if not prev_wid:
        return jsonify({'rows': [], 'prev_week': None})

    prev_path = DATA_DIR / prev_wid / f'{key}.json'
    if not prev_path.exists():
        return jsonify({'rows': [], 'prev_week': prev_wid})

    try:
        data = json.loads(prev_path.read_text('utf-8'))
        rows = data.get('rows', [])
        copied = [
            {'codice': r.get('codice', ''), 'titolo': r.get('titolo', ''), 'tipo': r.get('tipo', '')}
            for r in rows
            if not r.get('_separator') and r.get('titolo')
        ]
        return jsonify({'rows': copied, 'prev_week': prev_wid})
    except Exception:
        return jsonify({'rows': [], 'prev_week': prev_wid})


@app.route('/api/week/<timone>/riapri', methods=['POST'])
def riapri_week(timone):
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'ok': False, 'error': 'non autorizzato'}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    body = request.get_json(force=True, silent=True) or {}
    week_id = body.get('week_id') or get_week_meta(timone).get('week_id', '')
    if week_id:
        set_week_chiusa_flag(timone, week_id, False)
    return jsonify({'ok': True})


@app.route('/api/archive/<timone>')
def list_archive(timone):
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    current_wid = (get_week_meta(timone).get('week_id') or '').strip()
    weeks = []
    for d in sorted(DATA_DIR.iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith('_') or d.name == current_wid:
            continue
        try:
            dal = date_.fromisoformat(d.name)
        except ValueError:
            continue
        has_data = any(
            f.stem == timone or f.stem.startswith(timone + '_')
            for f in d.glob('*.json')
        )
        if has_data:
            al = dal + timedelta(days=6)
            weeks.append({'week_id': d.name, 'dal': d.name, 'al': al.isoformat(),
                          'chiusa': is_week_chiusa(timone, d.name)})
    return jsonify(weeks)


# ── Settimane applicate (tracking per "Altre settimane") ──────────────────────

def _applicate_path(timone: str) -> pathlib.Path:
    return WEEKS_DIR / f'applicate_{timone}.json'

def get_applicate(timone: str) -> list:
    p = _applicate_path(timone)
    try:
        return json.loads(p.read_text('utf-8')) if p.exists() else []
    except Exception:
        return []

def add_applicata(timone: str, week_id: str) -> None:
    ids = get_applicate(timone)
    if week_id not in ids:
        ids.append(week_id)
        _applicate_path(timone).write_text(json.dumps(ids), encoding='utf-8')


@app.route('/api/week/<timone>/segna-applicata', methods=['POST'])
def segna_applicata(timone):
    if _sola_lettura(): return jsonify({'ok': False}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    body = request.get_json(force=True, silent=True) or {}
    week_id = body.get('week_id', '').strip()
    if week_id and re.match(r'^\d{4}-\d{2}-\d{2}$', week_id):
        add_applicata(timone, week_id)
    return jsonify({'ok': True})


@app.route('/api/week/<timone>/applicate')
def list_applicate(timone):
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    current_wid = get_week_meta(timone).get('week_id', '')
    ids = [w for w in get_applicate(timone) if w != current_wid and not is_week_chiusa(timone, w)]
    return jsonify({'week_ids': ids})


@app.route('/api/archive/load/<week_id>/<key>')
def archive_load(week_id, key):
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', week_id):
        return jsonify({'error': 'week_id non valido'}), 400
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    path = DATA_DIR / week_id / f'{key}.json'
    if path.exists():
        return jsonify(json.loads(path.read_text('utf-8')))
    return jsonify({'rows': []})


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


def _cast_da_titolo(titolo: str) -> list:
    """Cerca lo show per titolo in InfoTv e restituisce i nomi del cast (già invertiti)."""
    body = {
        "text": titolo,
        "film": False, "tv": True, "personaggi": False, "sport": False,
        "filter": "1", "scheda": True, "foto": False,
        "tvmenu": False, "onlyfoto": False, "usableByTvmenu": False,
        "incomplete": False, "parameters": [], "area": None,
    }
    try:
        r = _prjdia.post(PRJDIA_SEARCH_API, json=body, timeout=8)
        if r.status_code != 200:
            return []
        shows = r.json().get('listaSchedeTv', [])
        if not shows:
            return []
        titolo_low = titolo.lower()
        match = next((s for s in shows if (s.get('titolo') or '').lower() == titolo_low), shows[0])
        cast_str = match.get('cast') or ''
        print(f'[AC-P CAST] show={match.get("titolo")!r} cast={cast_str[:120]!r}')
        nomi = [p.strip() for p in cast_str.split(';') if p.strip()]
        return nomi
    except Exception as e:
        print(f'[AC-P CAST] errore: {e}')
        return []


@app.route('/autocomplete_personaggi', methods=['POST'])
def autocomplete_personaggi():
    global _prjdia_ok
    data = request.json or {}
    q      = data.get('q', '').strip()
    titolo = data.get('titolo', '').strip()
    if len(q) < 2:
        return jsonify([])
    if not _prjdia_ok:
        prjdia_login()

    body_p = {
        "text": q,
        "film": False, "tv": False, "personaggi": True, "sport": False,
        "filter": "1", "scheda": True, "foto": False,
        "tvmenu": False, "onlyfoto": False, "usableByTvmenu": False,
        "incomplete": False, "parameters": [], "area": None,
    }

    try:
        r = _prjdia.post(PRJDIA_SEARCH_API, json=body_p, timeout=10)
        if r.status_code == 401:
            _prjdia_ok = False
            prjdia_login()
            r = _prjdia.post(PRJDIA_SEARCH_API, json=body_p, timeout=10)
        if r.status_code != 200:
            return jsonify([])

        seen = set()
        suggestions = []

        # ── 1. schede personaggio autonome ────────────────────────────────────
        for item in r.json().get('listaSchedePersonaggi', []):
            nome = _invert_nome(item.get('nomearte') or item.get('nomeanagrafico') or '')
            if nome and nome not in seen:
                seen.add(nome)
                suggestions.append({
                    'nome':        nome,
                    'professione': (item.get('professione') or '').strip(),
                })

        # ── 2. cast dallo show del titolo corrente (solo se nessun risultato) ────
        if not suggestions and titolo:
            q_low = q.lower()
            for nome in _cast_da_titolo(titolo):
                if nome and q_low in nome.lower() and nome not in seen:
                    seen.add(nome)
                    suggestions.append({'nome': nome, 'professione': ''})

        return jsonify(suggestions[:20])
    except Exception as e:
        print(f'[AC-P] Errore: {e}')
        return jsonify([])


@app.route('/tif_thumb')
def tif_thumb():
    cartella  = request.args.get('cartella', '').strip()
    codice    = request.args.get('codice', '').strip()
    scontorno = request.args.get('scontorno', '0') == '1'
    path = _safe_path(cartella, codice)
    if not path:
        return '', 400
    path_lower = _safe_path(cartella, codice.lower())
    is_scont   = '_scont' in codice.lower() or scontorno

    def _do():
        if is_scont:
            actual = None
            for ext in ('.psd', '.jpg', '.jpeg'):
                for base in (codice, codice.lower()):
                    p = os.path.join(cartella, base + ext)
                    if os.path.isfile(p):
                        actual = p
                        break
                if actual:
                    break
        else:
            actual = path if os.path.isfile(path) else (path_lower if path_lower and os.path.isfile(path_lower) else None)
        if actual is None:
            return None
        ext = pathlib.Path(actual).suffix.lower()
        if ext == '.psd':
            tmp_dir = tempfile.mkdtemp(prefix='scont_ql_')
            try:
                basename = os.path.basename(actual)
                subprocess.run(
                    ['qlmanage', '-t', '-s', '192', '-o', tmp_dir, actual],
                    capture_output=True, timeout=15,
                )
                thumb_png = os.path.join(tmp_dir, basename + '.png')
                if os.path.isfile(thumb_png):
                    with Image.open(thumb_png) as img:
                        img.thumbnail((192, 144))
                        buf = io.BytesIO()
                        img.convert('RGB').save(buf, format='JPEG', quality=85)
                        buf.seek(0)
                        return buf.read()
                return None
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            with Image.open(actual) as img:
                img.thumbnail((192, 144))
                buf = io.BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=85)
                buf.seek(0)
                return buf.read()
    try:
        data = _smb_call(_do)
        if data is None:
            return '', 404
        return Response(data, mimetype='image/jpeg', headers={'Cache-Control': 'no-cache'})
    except FuturesTimeoutError:
        print(f'[THUMB] timeout SMB: {path}')
        return '', 503
    except Exception as e:
        print(f'[THUMB] {e}')
        return '', 500




@app.route('/tif_mtime', methods=['POST'])
def tif_mtime():
    data        = request.json or {}
    files       = data.get('files', [])
    tutti_scont = bool(data.get('scontorni', False))

    # Raggruppa per cartella: una sola scandir per directory evita la
    # incoerenza tra la cache degli attributi e quella delle directory di macOS SMB.
    by_cartella: dict[str, list[str]] = {}
    for f in files:
        cartella = f.get('cartella', '')
        codice   = f.get('codice', '')
        if cartella and codice:
            by_cartella.setdefault(cartella, []).append(codice)

    def _scan_dir(cartella: str, codici: list[str]) -> tuple[dict[str, int | None], list[str]]:
        wanted: dict[str, str] = {}
        new_wanted: dict[str, str] = {}
        for c in codici:
            wanted[c.lower() + '.tif'] = c
            if '_scont' in c.lower() or tutti_scont:
                wanted[c.lower() + '.psd']  = c
                wanted[c.lower() + '.jpg']  = c
                wanted[c.lower() + '.jpeg'] = c
                new_wanted[c.lower() + '_new.psd']  = c
                new_wanted[c.lower() + '_new.jpg']  = c
                new_wanted[c.lower() + '_new.jpeg'] = c
        out = {c: None for c in codici}
        new_set: set[str] = set()
        try:
            with os.scandir(cartella) as it:
                for entry in it:
                    name_lc = entry.name.lower()
                    c = new_wanted.get(name_lc)
                    if c is not None:
                        new_set.add(c)
                        try:
                            out[c] = round(entry.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            pass
                        continue
                    c = wanted.get(name_lc)
                    if c is not None and out[c] is None:
                        try:
                            out[c] = round(entry.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            pass
        except OSError:
            pass
        return out, list(new_set)

    result: dict[str, int | None] = {}
    new_codici: list[str] = []
    futures = {
        _io_pool.submit(_scan_dir, cartella, codici): cartella
        for cartella, codici in by_cartella.items()
    }
    for future in futures:
        try:
            mtimes, news = future.result(timeout=8)
            result.update(mtimes)
            new_codici.extend(news)
        except Exception:
            pass
    return jsonify({**result, '_new': new_codici})



@app.route('/tlp_thumb')
def tlp_thumb():
    rel = request.args.get('rel', '').strip()
    if not rel or '..' in rel:
        return '', 400
    path = str(TLP_SCONTORNI / rel)
    try:
        pathlib.Path(path).resolve().relative_to(TLP_SCONTORNI.resolve())
    except ValueError:
        return '', 400
    if not os.path.isfile(path):
        return '', 404

    ext = pathlib.Path(path).suffix.lower()
    if ext == '.psd':
        tmp_dir = tempfile.mkdtemp(prefix='tlp_ql_')
        try:
            basename = os.path.basename(path)
            subprocess.run(
                ['qlmanage', '-t', '-s', '100', '-o', tmp_dir, path],
                capture_output=True, timeout=15,
            )
            thumb = os.path.join(tmp_dir, basename + '.png')
            if os.path.isfile(thumb):
                with Image.open(thumb) as img:
                    img.thumbnail((100, 75))
                    buf = io.BytesIO()
                    img.convert('RGB').save(buf, format='JPEG', quality=80)
                    buf.seek(0)
                    jpeg = buf.read()
                return Response(jpeg, mimetype='image/jpeg',
                                headers={'Cache-Control': 'max-age=3600'})
        except Exception as e:
            print(f'[TLP_THUMB qlmanage] {e}')
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        try:
            with Image.open(path) as img:
                img.thumbnail((100, 75))
                buf = io.BytesIO()
                img.convert('RGB').save(buf, format='JPEG', quality=80)
                buf.seek(0)
                return Response(buf.read(), mimetype='image/jpeg',
                                headers={'Cache-Control': 'max-age=3600'})
        except Exception as e:
            print(f'[TLP_THUMB PIL] {e}')
    return '', 404


@app.route('/cerca_scontorno_titolo', methods=['POST'])
def cerca_scontorno_titolo():
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'error': 'non autorizzato'}), 403
    data      = request.json or {}
    titolo    = data.get('titolo', '').strip()
    categoria = data.get('categoria', '').strip()
    if not titolo or categoria not in CATEGORIE_TLP:
        return jsonify({'results': [], 'error': 'parametri mancanti'}), 400

    cartella = TLP_SCONTORNI / CATEGORIE_TLP[categoria]
    if not cartella.exists():
        return jsonify({'results': [], 'error': 'cartella non trovata'})

    titolo_lower = strip_articolo(titolo).lower()
    is_serie = categoria in ('serie', 'soap', 'cartoni', 'documentari')
    results: list[dict] = []
    try:
        for f in cartella.rglob('*'):
            if f.suffix.lower() not in ('.psd', '.jpg', '.jpeg'):
                continue
            label = f.parent.name if is_serie else f.stem
            nome  = strip_articolo(strip_parens(label)).lower()
            if titolo_lower in nome:
                results.append({
                    'path':     str(f),
                    'nome':     label,
                    'relativo': str(f.relative_to(TLP_SCONTORNI)),
                })
    except Exception as e:
        print(f'[CERCA_SCONT_T] {e}')
    results.sort(key=lambda r: pathlib.Path(r['path']).name.lower())
    return jsonify({'results': results})


@app.route('/cerca_scontorno_personaggio', methods=['POST'])
def cerca_scontorno_personaggio():
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'error': 'non autorizzato'}), 403
    data        = request.json or {}
    personaggio = data.get('personaggio', '').strip()
    if not personaggio:
        return jsonify({'results': [], 'error': 'personaggio mancante'}), 400

    # personaggio qui è la singola parola (cognome) scelta dall'utente nel dropdown
    lettera = personaggio[0].upper() if personaggio else ''
    base    = TLP_SCONTORNI / '__PERSONAGGI'
    lett_d  = base / f'{lettera}_personaggi'
    search  = lett_d if lett_d.exists() else base

    termine = personaggio.lower()
    results: list[dict] = []
    try:
        for f in search.rglob('*'):
            if f.suffix.lower() not in ('.psd', '.jpg', '.jpeg'):
                continue
            if termine in f.stem.lower():
                results.append({
                    'path':     str(f),
                    'nome':     f.stem,
                    'relativo': str(f.relative_to(TLP_SCONTORNI)),
                })
    except Exception as e:
        print(f'[CERCA_SCONT_P] {e}')
    results.sort(key=lambda r: r['nome'].lower())
    return jsonify({'results': results})


@app.route('/apri_scontorno', methods=['POST'])
def apri_scontorno():
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'ok': False, 'error': 'non autorizzato'}), 403
    data = request.json or {}
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'ok': False, 'error': 'percorso mancante'}), 400
    try:
        pathlib.Path(path).resolve().relative_to(TLP_SCONTORNI.resolve())
    except ValueError:
        return jsonify({'ok': False, 'error': 'percorso non valido'}), 400
    if not os.path.isfile(path):
        return jsonify({'ok': False, 'error': 'file non trovato'}), 404
    try:
        subprocess.Popen(['open', path])
        return jsonify({'ok': True})
    except Exception as e:
        print(f'[APRI_SCONT] {e}')
        return jsonify({'ok': False, 'error': str(e)}), 500




@app.route('/scarica_scontorno')
def scarica_scontorno():
    if session.get('ruolo') not in ('editor', 'admin'):
        return 'non autorizzato', 403
    rel = request.args.get('rel', '').strip()
    if not rel:
        return 'percorso mancante', 400
    try:
        full = (TLP_SCONTORNI / rel).resolve()
        full.relative_to(TLP_SCONTORNI.resolve())
    except ValueError:
        return 'percorso non valido', 400
    if not full.is_file():
        return 'file non trovato', 404
    ext = full.suffix.lower()
    mime = ('image/vnd.adobe.photoshop' if ext == '.psd'
            else 'image/tiff'           if ext in ('.tif', '.tiff')
            else 'image/jpeg')
    return send_file(str(full), mimetype=mime, as_attachment=True, download_name=full.name)


_DIPIU_CARTELLE = {
    'personaggi': 'PERSONAGGI',
    'serie':      'SERIE',
    'soap':       'SOAP',
    'sport':      'SPORT',
}

@app.route('/cerca_fotina_dipiu', methods=['POST'])
def cerca_fotina_dipiu():
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'error': 'non autorizzato'}), 403
    data        = request.json or {}
    categoria   = data.get('categoria',   '').strip()
    titolo      = data.get('titolo',      '').strip()
    personaggio = data.get('personaggio', '').strip()

    if categoria not in _DIPIU_CARTELLE:
        return jsonify({'results': [], 'error': 'categoria non valida'}), 400

    cartella = TLP_DIPIU_FOTINE / _DIPIU_CARTELLE[categoria]
    if not cartella.exists():
        return jsonify({'results': [], 'error': 'cartella non trovata'})

    results: list[dict] = []
    _NUM_TRAIL = re.compile(r'\d+$')
    try:
        for f in cartella.rglob('*'):
            if f.suffix.lower() != '.tif':
                continue
            stem      = _NUM_TRAIL.sub('', f.stem).strip()
            stem_low  = stem.lower()

            if categoria == 'personaggi':
                if personaggio and personaggio.lower() in stem_low:
                    results.append({'path': str(f), 'nome': f.stem,
                                    'relativo': str(f.relative_to(TLP_SCONTORNI))})

            elif categoria in ('serie', 'soap'):
                tit_key = strip_articolo(titolo).lower()
                if tit_key and tit_key in stem_low:
                    if not personaggio or personaggio.lower() in stem_low:
                        results.append({'path': str(f), 'nome': f.stem,
                                        'relativo': str(f.relative_to(TLP_SCONTORNI))})

            elif categoria == 'sport':
                if personaggio and personaggio.lower() in stem_low:
                    if not titolo or titolo.lower() in stem_low:
                        results.append({'path': str(f), 'nome': f.stem,
                                        'relativo': str(f.relative_to(TLP_SCONTORNI))})
    except Exception as e:
        print(f'[CERCA_FOTINA_DIPIU] {e}')

    results.sort(key=lambda r: r['nome'].lower())
    return jsonify({'results': results})


if __name__ == '__main__':
    _debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    _cert = os.environ.get('SSL_CERT')
    _key = os.environ.get('SSL_KEY')
    _ssl = (_cert, _key) if _cert and _key else None
    app.run(host='0.0.0.0', port=5010, debug=_debug, use_reloader=False, ssl_context=_ssl)
