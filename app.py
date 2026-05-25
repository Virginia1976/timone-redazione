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
from datetime import timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for, send_file
from PIL import Image

load_dotenv(pathlib.Path(__file__).parent / '.env')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-local-secret-key')
app.permanent_session_lifetime = timedelta(days=1)

EDITOR_USERNAME    = os.environ.get('EDITOR_USERNAME', '')
EDITOR_PASSWORD    = os.environ.get('EDITOR_PASSWORD', '')
REDAZIONE_USERNAME = os.environ.get('REDAZIONE_USERNAME', '')
REDAZIONE_PASSWORD = os.environ.get('REDAZIONE_PASSWORD', '')
ADMIN_USERNAME     = os.environ.get('ADMIN_USERNAME', '')
ADMIN_PASSWORD     = os.environ.get('ADMIN_PASSWORD', '')

_attivi_raw = os.environ.get('TIMONI_ATTIVI', '')
TIMONI_ATTIVI = [t.strip() for t in _attivi_raw.split(',') if t.strip()] if _attivi_raw else []

DATA_DIR        = pathlib.Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)
WEEKS_DIR       = DATA_DIR / '_weeks'
WEEKS_DIR.mkdir(exist_ok=True)
EDITORIALE_BASE = os.environ.get('EDITORIALE_BASE', '/Volumes/EDITORIALE')
EDITORIALE_SMB  = os.environ.get('EDITORIALE_SMB', '')

VALID_KEY    = re.compile(r'^[a-z0-9_]{1,60}$')
KNOWN_TIMONI = {'nuovotv', 'dipiutv', 'tvmia', 'nuovo', 'dipiu', 'divadonna'}

TLP_SCONTORNI = pathlib.Path(os.environ.get('TLP_SCONTORNI', '/Volumes/TLPserver/ARCHIVIO_FOTO/__SCONTORNI'))

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
    r"^(?:il|lo|la|i|gli|le|un|una|uno|l'|the|a|an)\s+",
    re.IGNORECASE,
)

def strip_articolo(titolo: str) -> str:
    return _ARTICOLI.sub('', titolo).strip()


def timone_from_key(key: str) -> str | None:
    for t in KNOWN_TIMONI:
        if key == t or key.startswith(t + '_'):
            return t
    return None


def get_week_meta(timone: str) -> dict:
    path = WEEKS_DIR / f'{timone}.json'
    if path.exists():
        try:
            return json.loads(path.read_text('utf-8'))
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
        'ruolo':         ruolo,
        'timoni_attivi': timoni,
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


@app.route('/api/save/<key>', methods=['POST'])
def save(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    if timone and get_week_meta(timone).get('chiusa'):
        return jsonify({'error': 'settimana chiusa', 'chiusa': True}), 403
    data = request.get_json(force=True, silent=True) or {}
    d    = week_data_dir(timone) if timone else DATA_DIR
    (d / f'{key}.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return jsonify({'ok': True})


@app.route('/api/load/<key>')
def load(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    d      = week_data_dir(timone) if timone else DATA_DIR
    path   = d / f'{key}.json'
    if path.exists():
        return jsonify(json.loads(path.read_text('utf-8')))
    return jsonify({'rows': []})


@app.route('/api/mtime/<key>')
def data_mtime(key):
    if not VALID_KEY.match(key):
        return jsonify({'error': 'chiave non valida'}), 400
    timone = timone_from_key(key)
    d      = week_data_dir(timone) if timone else DATA_DIR
    path   = d / f'{key}.json'
    return jsonify({'mtime': path.stat().st_mtime if path.exists() else None})


@app.route('/api/list')
def list_timoni():
    saved   = {}
    checked = set()
    for timone in KNOWN_TIMONI:
        d = week_data_dir(timone)
        if d in checked:
            continue
        checked.add(d)
        for f in d.glob('*.json'):
            try:
                data = json.loads(f.read_text('utf-8'))
                rows = data.get('rows', [])
                saved[f.stem] = {
                    'total':  len(rows),
                    'gialli': sum(1 for r in rows if r.get('colore') == 'giallo'),
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
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    body    = request.get_json(force=True, silent=True) or {}
    dal     = body.get('dal', '').strip()
    al      = body.get('al', '').strip()
    current = get_week_meta(timone)
    new_wid = dal   # la data di inizio è l'identificatore della settimana
    changed = new_wid != current.get('week_id', '')
    new_meta = {'dal': dal, 'al': al, 'week_id': new_wid, 'chiusa': False}
    set_week_meta(timone, new_meta)
    if new_wid:
        (DATA_DIR / new_wid).mkdir(exist_ok=True)
    return jsonify({'ok': True, 'changed': changed, **new_meta})


@app.route('/api/week/<timone>/chiudi', methods=['POST'])
def chiudi_week(timone):
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    meta = get_week_meta(timone)
    meta['chiusa'] = True
    set_week_meta(timone, meta)
    return jsonify({'ok': True})


@app.route('/api/week/<timone>/riapri', methods=['POST'])
def riapri_week(timone):
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'ok': False, 'error': 'non autorizzato'}), 403
    if timone not in KNOWN_TIMONI:
        return jsonify({'error': 'timone non valido'}), 400
    meta = get_week_meta(timone)
    meta['chiusa'] = False
    set_week_meta(timone, meta)
    return jsonify({'ok': True})


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
    if session.get('ruolo') not in ('editor', 'admin'):
        return jsonify({'ok': False, 'error': 'non autorizzato'}), 403
    data     = request.json or {}
    cartella = data.get('cartella', '').strip()
    codice   = data.get('codice', '').strip()
    path = _safe_path(cartella, codice)
    if not path:
        return jsonify({'ok': False, 'error': 'parametri non validi'}), 400

    if os.path.isfile(path):
        try:
            subprocess.Popen(['open', path])
            return jsonify({'ok': True, 'method': 'local'})
        except Exception as e:
            print(f'[OPEN_TIF] {e}')
            return jsonify({'ok': False, 'error': str(e)}), 500

    if EDITORIALE_SMB:
        idx = cartella.upper().find('/EDITORIALE/')
        rel = cartella[idx + len('/EDITORIALE'):] if idx >= 0 else '/' + os.path.basename(cartella)
        smb_url = EDITORIALE_SMB.rstrip('/') + rel.rstrip('/') + '/' + codice + '.tif'
        return jsonify({'ok': True, 'method': 'smb', 'url': smb_url})

    return jsonify({'ok': False, 'path': path})


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


@app.route('/scont_thumb')
def scont_thumb():
    cartella = request.args.get('cartella', '').strip()
    nome     = request.args.get('nome', '').strip()
    if '..' in cartella or '..' in nome or not cartella or not nome:
        return '', 400
    for ext in ('.psd', '.jpg', '.jpeg', '.png'):
        path = os.path.join(cartella, nome + ext)
        if not os.path.isfile(path):
            continue
        if ext == '.psd':
            # PIL legge i PSD in modo errato; sips fallisce su PSD complessi
            # → usa qlmanage (QuickLook macOS) che gestisce qualsiasi PSD
            tmp_dir = tempfile.mkdtemp(prefix='scont_ql_')
            try:
                basename = os.path.basename(path)
                subprocess.run(
                    ['qlmanage', '-t', '-s', '80', '-o', tmp_dir, path],
                    capture_output=True, timeout=15,
                )
                thumb = os.path.join(tmp_dir, basename + '.png')
                if os.path.isfile(thumb):
                    with Image.open(thumb) as img:
                        img.thumbnail((80, 60))
                        buf = io.BytesIO()
                        img.convert('RGB').save(buf, format='JPEG', quality=75)
                        buf.seek(0)
                        jpeg = buf.read()
                    return Response(jpeg, mimetype='image/jpeg',
                                    headers={'Cache-Control': 'no-cache'})
                print(f'[SCONT_THUMB qlmanage] thumb non trovato in {tmp_dir}')
            except Exception as e:
                print(f'[SCONT_THUMB qlmanage] {e}')
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            # PIL funziona bene per JPG/PNG
            try:
                with Image.open(path) as img:
                    img.thumbnail((80, 60))
                    buf = io.BytesIO()
                    img.convert('RGB').save(buf, format='JPEG', quality=75)
                    buf.seek(0)
                    return Response(buf.read(), mimetype='image/jpeg',
                                    headers={'Cache-Control': 'no-cache'})
            except Exception as e:
                print(f'[SCONT_THUMB PIL] {e}')
    return '', 404


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
    results: list[dict] = []
    try:
        for f in cartella.rglob('*'):
            if f.suffix.lower() not in ('.psd', '.jpg', '.jpeg'):
                continue
            nome = strip_articolo(strip_parens(f.stem)).lower()
            if titolo_lower in nome:
                results.append({
                    'path':     str(f),
                    'nome':     f.stem,
                    'relativo': str(f.relative_to(TLP_SCONTORNI)),
                })
            if len(results) >= 100:
                break
    except Exception as e:
        print(f'[CERCA_SCONT_T] {e}')
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
            if len(results) >= 100:
                break
    except Exception as e:
        print(f'[CERCA_SCONT_P] {e}')
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
    mime = 'image/vnd.adobe.photoshop' if ext == '.psd' else 'image/jpeg'
    return send_file(str(full), mimetype=mime, as_attachment=True, download_name=full.name)


if __name__ == '__main__':
    _debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=5010, debug=_debug, use_reloader=False)
