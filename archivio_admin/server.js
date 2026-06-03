// Archivio Admin — server locale privato (solo tuo Mac)
// Avvia con: node server.js
// Porta: 3099

const express = require('express');
const cors    = require('cors');
const fs      = require('fs');
const path    = require('path');
const { exec } = require('child_process');
const chokidar = require('chokidar');

const PORT = 3099;

// ── Configurazione percorsi ──────────────────────────────────────────────────
const ARCHIVIO_BASE = '/Users/virginiaorsini/Library/Mobile Documents/com~apple~CloudDocs/timoni-archivio-foto/Archivio_immagini';

// ── App ──────────────────────────────────────────────────────────────────────
const app = express();
app.use(cors());
app.use(express.json());

// ── Ricerca nell'archivio ────────────────────────────────────────────────────
function cercaFile(cartellaArchivio, titolo, personaggio, tipo) {
  const dir = path.join(ARCHIVIO_BASE, cartellaArchivio);
  if (!fs.existsSync(dir)) return [];

  const files = fs.readdirSync(dir).filter(f => f.toLowerCase().endsWith('.tif'));
  const t = (titolo || '').toLowerCase().trim();
  const p = (personaggio || '').toLowerCase().trim();

  return files.filter(f => {
    const fLow = f.toLowerCase();
    if (tipo === 'nome')    return p && fLow.startsWith(p);
    if (tipo === 'titolo')  return t && fLow.startsWith(t);
    if (tipo === 'avanzata') {
      if (t && p) return fLow.startsWith(t) && fLow.includes(` - ${p}`);
      if (t)      return fLow.startsWith(t);
      if (p)      return fLow.startsWith(p);
    }
    return false;
  }).map(f => path.join(dir, f));
}

function apriFile(fullPath) {
  exec(`open "${fullPath}"`, err => {
    if (err) console.error('Errore apertura:', fullPath, err.message);
  });
}

function delay(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// ── POST /cerca ──────────────────────────────────────────────────────────────
app.post('/cerca', (req, res) => {
  const { cartellaArchivio, titolo, personaggio, tipo } = req.body;
  if (!cartellaArchivio || !tipo) return res.status(400).json({ error: 'cartellaArchivio e tipo richiesti' });

  const files = cercaFile(cartellaArchivio, titolo, personaggio, tipo);
  res.json({ trovato: files.length > 0, files });
});

// ── POST /apri ───────────────────────────────────────────────────────────────
// Apre i file archivio trovati + la canvas (cartellaNumero/codice.tif) se esiste
app.post('/apri', async (req, res) => {
  const { cartellaArchivio, cartellaNumero, codice, titolo, personaggio, tipo } = req.body;
  if (!cartellaArchivio || !tipo) return res.status(400).json({ error: 'cartellaArchivio e tipo richiesti' });

  const filesArchivio = cercaFile(cartellaArchivio, titolo, personaggio, tipo);

  if (!filesArchivio.length) {
    return res.json({ trovato: false, aperto: [], canvasAperta: false });
  }

  // Apre i file archivio in ordine inverso (come preavanzata_archivio)
  for (let i = filesArchivio.length - 1; i >= 0; i--) {
    apriFile(filesArchivio[i]);
    await delay(800);
  }

  // Apre la canvas se esiste
  let canvasAperta = false;
  if (cartellaNumero && codice) {
    const canvasPath = path.join(cartellaNumero, `${codice}.tif`);
    if (fs.existsSync(canvasPath)) {
      await delay(800);
      apriFile(canvasPath);
      canvasAperta = true;
    }
  }

  res.json({ trovato: true, aperto: filesArchivio, canvasAperta });
});

// ── Watcher ──────────────────────────────────────────────────────────────────
let watcherInstance = null;
let watcherConfig   = null;
let watcherMappa    = {}; // codice → { titolo, personaggio }

function nomeDestinazione(codice, tipo) {
  const voce = watcherMappa[codice];
  if (!voce) return null; // codice non in mappa, ignora

  const t = (voce.titolo      || '').replace(/:/g, '').trim();
  const p = (voce.personaggio || '').replace(/:/g, '').trim();

  let base;
  if (tipo === 'avanzata') base = (t && p) ? `${t} - ${p}` : (t || p);
  else if (tipo === 'titolo') base = t || codice;
  else if (tipo === 'nome')   base = p || codice;
  else base = codice;

  return base ? `${base}.tif` : null;
}

function avviaWatcher(cartellaArchivio, tipo, exportFolder, righe) {
  if (watcherInstance) {
    watcherInstance.close();
    watcherInstance = null;
  }

  // Costruisce mappa codice → { titolo, personaggio }
  watcherMappa = {};
  (righe || []).forEach(r => { if (r.codice) watcherMappa[r.codice] = r; });

  const destDir = path.join(ARCHIVIO_BASE, cartellaArchivio);
  if (!fs.existsSync(destDir)) fs.mkdirSync(destDir, { recursive: true });

  watcherInstance = chokidar.watch(exportFolder, {
    ignored: /(^|[/\\])\../,
    persistent: true,
    ignoreInitial: true,
    depth: 1,
  });

  watcherConfig = { cartellaArchivio, tipo, exportFolder, avviatoAlle: new Date().toISOString() };

  watcherInstance.on('add',    fullPath => gestisciFile(fullPath, destDir, tipo));
  watcherInstance.on('change', fullPath => gestisciFile(fullPath, destDir, tipo));

  console.log(`Watcher avviato: ${exportFolder} → ${destDir} [${tipo}] (${Object.keys(watcherMappa).length} righe in mappa)`);
}

function gestisciFile(fullPath, destDir, tipo) {
  const basename = path.basename(fullPath);
  if (!basename.match(/\.(tif|tiff|psd)$/i)) return;

  const codice   = basename.replace(/\.(tif|tiff|psd)$/i, '');
  const nomeFile = nomeDestinazione(codice, tipo);

  if (!nomeFile) {
    console.log(`Codice "${codice}" non in mappa, ignorato.`);
    return;
  }

  const dest = path.join(destDir, nomeFile);
  try {
    fs.copyFileSync(fullPath, dest);
    console.log(`Copiato: ${basename} → ${nomeFile}`);
  } catch (err) {
    console.error('Errore copia:', err.message);
  }
}

app.post('/watcher/start', (req, res) => {
  const { cartellaArchivio, tipo, exportFolder, righe } = req.body;
  if (!cartellaArchivio || !tipo || !exportFolder) {
    return res.status(400).json({ error: 'cartellaArchivio, tipo, exportFolder richiesti' });
  }
  if (!fs.existsSync(exportFolder)) {
    return res.status(400).json({ error: `Cartella non trovata: ${exportFolder}` });
  }
  try {
    avviaWatcher(cartellaArchivio, tipo, exportFolder, righe);
    res.json({ ok: true, config: watcherConfig });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/watcher/stop', (req, res) => {
  if (watcherInstance) {
    watcherInstance.close();
    watcherInstance = null;
    watcherConfig   = null;
    console.log('Watcher fermato.');
  }
  res.json({ ok: true });
});

app.get('/watcher/status', (req, res) => {
  res.json({ attivo: !!watcherInstance, config: watcherConfig });
});

// ── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, '127.0.0.1', () => {
  console.log(`Archivio Admin in ascolto su http://127.0.0.1:${PORT}`);
  console.log(`Archivio base: ${ARCHIVIO_BASE}`);
});
