# -*- coding: utf-8 -*-
"""
Application d'affichage des emplois du temps sur TV, navigateur et clé USB.
Lancement local : streamlit run app_edt_tv.py
"""
from __future__ import annotations
import io, json, re, unicodedata, html as htmlmod, base64, mimetypes, uuid, hmac, os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

APP_TITLE = "Plateforme d'affichage des EDT — UDL-SBA"
COLONNES = ["Enseignements", "Code", "Enseignants", "Horaire", "Jours", "Lieu", "Promotion", "Département"]
JOURS_ORDRE = ["Dimanche", "Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
TOUS_JOURS, TOUS_CRENEAUX = "Tous les jours", "Tous les créneaux"
TOUTES_PROMOTIONS, TOUS_DEPARTEMENTS = "Toutes les promotions", "Tous les départements"
TYPES_ENSEIGNEMENT = ["Cours", "TD", "TP", "Atelier"]
CHOIX_TYPES_ENSEIGNEMENT = [
    "Tous les enseignements", "Cours", "TD", "TP", "Atelier",
    "Cours + TD", "TD + TP", "Cours + TP", "Sélection personnalisée",
]

# Rubrique Actualités / Annonces / Avis. Les fichiers restent dans le dossier de l'application.
APP_DIR = Path(__file__).resolve().parent
FICHIER_PUBLICATIONS = APP_DIR / "publications_edt.json"
DOSSIER_MEDIAS_PUBLICATIONS = APP_DIR / "publications_medias"
CATEGORIES_PUBLICATION = ["News / Actualité", "Annonce", "Avis"]
PUBLICS_PUBLICATION = ["Étudiants", "Enseignants", "Étudiants et enseignants"]
EXTENSIONS_PUBLICATION = ["png", "jpg", "jpeg", "webp", "gif", "pdf", "txt", "doc", "docx", "xls", "xlsx", "ppt", "pptx"]
TAILLE_MAX_PIECE_JOINTE = 4 * 1024 * 1024  # 4 Mo par pièce jointe
TAILLE_MAX_MEDIAS_HTML = 8 * 1024 * 1024  # limite d'intégration des pièces jointes dans le HTML autonome

st.set_page_config(page_title=APP_TITLE, page_icon="📺", layout="wide", initial_sidebar_state="expanded")
st.markdown("""
<style>
.block-container {padding-top:1.1rem;padding-bottom:1rem;max-width:1800px}
div[data-testid="stMetric"] {background:#f8fafc;border:1px solid #dbeafe;padding:12px 16px;border-radius:12px}
.tv-intro {background:linear-gradient(120deg,#0f172a,#1d4ed8);color:white;padding:20px 24px;border-radius:16px;margin-bottom:16px}
.tv-intro h1 {color:white;font-size:2rem;margin:0 0 5px 0}
.tv-intro p {color:#dbeafe;margin:0}
</style>
""", unsafe_allow_html=True)

def _sans_accents(value: Any) -> str:
    txt = "" if value is None else str(value).strip()
    txt = unicodedata.normalize("NFKD", txt)
    return "".join(ch for ch in txt if not unicodedata.combining(ch)).lower()

def _normaliser_entete(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _sans_accents(value)).strip()

def _normaliser_horaire(value: Any) -> str:
    txt = "" if value is None else str(value).strip()
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r"\s*[-–—]\s*", " - ", txt)
    txt = re.sub(r"(\d{1,2})\s*h\s*(\d{2})", r"\1h\2", txt, flags=re.I)
    txt = re.sub(r"(\d{1,2})\s*h\b", r"\1h", txt, flags=re.I)
    txt = re.sub(r"(\d{1,2}):(\d{2})", r"\1h\2", txt)
    return txt

def _minutes_debut(value: Any) -> int:
    match = re.search(r"(\d{1,2})\s*h\s*(\d{0,2})", _normaliser_horaire(value), flags=re.I)
    return int(match.group(1))*60 + int(match.group(2) or 0) if match else 10**8

# Classement par heure de début : Matin = 08h00–12h30 ; Après-midi = 12h30–17h00.
# La borne de fin est exclue : une séance commençant à 12h30 appartient à l'après-midi.
PERIODES_HORAIRES = {
    "Matin": (8 * 60, 12 * 60 + 30),
    "Après-midi": (12 * 60 + 30, 17 * 60),
}
CHOIX_PERIODES_HORAIRES = list(PERIODES_HORAIRES)

def _horaire_dans_periode(horaire: Any, periode: str) -> bool:
    limites = PERIODES_HORAIRES.get(periode)
    if limites is None:
        return False
    debut = _minutes_debut(horaire)
    return limites[0] <= debut < limites[1]

def _separer_enseignants(value: Any) -> list[str]:
    """Sépare les enseignants d'une cellule lorsque plusieurs noms sont indiqués."""
    texte = "" if value is None else str(value).strip()
    noms = re.split(r"\s*(?:/|\+)\s*", texte)
    return [re.sub(r"\s+", " ", nom).strip(" .;,") for nom in noms if nom.strip(" .;, +/")]

def _cle_enseignant(value: Any) -> str:
    return _sans_accents(re.sub(r"\s+", " ", "" if value is None else str(value)).strip())

def _jour_index(value: Any) -> int:
    v = _sans_accents(value)
    for i, day in enumerate(JOURS_ORDRE):
        if _sans_accents(day) == v:
            return i
    return 100

def _type_enseignement(ligne: Any) -> str:
    """Détermine le type depuis les préfixes réellement présents dans Code et Enseignements."""
    code = _sans_accents(ligne.get("Code", ""))
    intitule = _sans_accents(ligne.get("Enseignements", ""))
    # Dans le classeur source, les valeurs commencent par TP, TD, Cours ou Atelier.
    # On privilégie le champ Code, puis le préfixe de l'intitulé, pour éviter les faux Cours.
    prefixes = [
        (r"^tp(?:\d+)?(?=$|[\s\-–—_/])", "TP"),
        (r"^td(?:\d+)?(?=$|[\s\-–—_/])", "TD"),
        (r"^cours(?=$|[\s\-–—_/])", "Cours"),
        (r"^atelier(?=$|[\s\-–—_/])", "Atelier"),
    ]
    for valeur in (code, intitule):
        for motif, type_nom in prefixes:
            if re.search(motif, valeur, flags=re.I):
                return type_nom
    # Compatibilité avec d'autres classeurs où le type est indiqué dans le texte.
    texte = f"{code} {intitule}"
    if re.search(r"(?<![a-z0-9])tp\d*(?![a-z0-9])", texte) or re.search(r"travaux pratiques?", texte):
        return "TP"
    if re.search(r"(?<![a-z0-9])td\d*(?![a-z0-9])", texte) or re.search(r"travaux diriges?", texte):
        return "TD"
    if re.search(r"(?<![a-z0-9])atelier(?![a-z0-9])", texte):
        return "Atelier"
    return "Cours"

def _types_a_afficher(choix: str, personnalises: list[str] | None = None) -> list[str]:
    if choix == "Tous les enseignements":
        return TYPES_ENSEIGNEMENT.copy()
    if choix == "Sélection personnalisée":
        return [t for t in TYPES_ENSEIGNEMENT if t in (personnalises or [])]
    if "+" in choix:
        return [t.strip() for t in choix.split("+") if t.strip() in TYPES_ENSEIGNEMENT]
    return [choix] if choix in TYPES_ENSEIGNEMENT else TYPES_ENSEIGNEMENT.copy()

def _nettoyer_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise ValueError("Le fichier Excel ne contient aucune ligne de données.")
    alias = {
        "enseignements":"Enseignements", "enseignement":"Enseignements", "matiere":"Enseignements", "intitule":"Enseignements",
        "code":"Code", "type":"Code", "code enseignement":"Code",
        "enseignants":"Enseignants", "enseignant":"Enseignants", "professeur":"Enseignants", "professeurs":"Enseignants",
        "horaire":"Horaire", "creneau":"Horaire", "creneaux horaires":"Horaire",
        "jours":"Jours", "jour":"Jours", "lieu":"Lieu", "salle":"Lieu", "local":"Lieu",
        "promotion":"Promotion", "promotions":"Promotion", "departement":"Département",
        "departement affiliation":"Département",
    }
    df = df.copy()
    df.columns = [alias.get(_normaliser_entete(c), str(c).strip()) for c in df.columns]
    result = pd.DataFrame(index=df.index)
    for col in df.columns:
        if col not in result.columns:
            result[col] = df[col]
        else:
            blank = result[col].isna() | (result[col].astype(str).str.strip() == "")
            result.loc[blank, col] = df.loc[blank, col]
    df = result
    required = ["Enseignements", "Horaire", "Jours", "Promotion"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError("Colonnes indispensables introuvables : "+", ".join(missing)+". Colonnes reconnues : "+", ".join(map(str,df.columns)))
    for col in COLONNES:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["Horaire"] = df["Horaire"].map(_normaliser_horaire)
    df = df[df[COLONNES].apply(lambda row:any(str(x).strip() for x in row),axis=1)].copy()
    df["_type_enseignement"] = df.apply(_type_enseignement, axis=1)
    df["_jour_tri"] = df["Jours"].map(_jour_index)
    df["_heure_tri"] = df["Horaire"].map(_minutes_debut)
    df["_promo_tri"] = df["Promotion"].str.casefold()
    df["_ens_tri"] = df["Enseignements"].str.casefold()
    return df.sort_values(["_jour_tri","_heure_tri","_promo_tri","_ens_tri"],kind="stable").reset_index(drop=True)

@st.cache_data(show_spinner=False)
def _charger_excel(data: bytes) -> pd.DataFrame:
    return _nettoyer_dataframe(pd.read_excel(io.BytesIO(data), dtype=str))

def _records(df: pd.DataFrame) -> list[dict]:
    """Sérialise les lignes avec le type calculé côté Python pour assurer des couleurs fiables en HTML."""
    rows = df[COLONNES].fillna("").to_dict(orient="records")
    types = df["_type_enseignement"].tolist() if "_type_enseignement" in df.columns else [
        _type_enseignement(row) for row in rows
    ]
    result = []
    for row, type_nom in zip(rows, types):
        item = {k: (str(v).strip() if v is not None else "") for k, v in row.items()}
        item["TypeEnseignement"] = str(type_nom or "Cours").strip()
        result.append(item)
    return result

def _json_safe(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False, separators=(",",":")).replace("</","<\\/")


def _mot_de_passe_admin_publications() -> str:
    """Mot de passe fourni par Streamlit Secrets ou par variable d'environnement."""
    mot = os.environ.get("PUBLICATION_ADMIN_PASSWORD", "").strip()
    try:
        mot_secret = str(st.secrets.get("PUBLICATION_ADMIN_PASSWORD", "")).strip()
        if mot_secret:
            mot = mot_secret
    except Exception:
        pass
    return mot

def _charger_publications() -> list[dict]:
    """Charge les publications sauvegardées sur le disque de l'application."""
    try:
        if not FICHIER_PUBLICATIONS.exists():
            return []
        contenu = json.loads(FICHIER_PUBLICATIONS.read_text(encoding="utf-8"))
        return contenu if isinstance(contenu, list) else []
    except (OSError, json.JSONDecodeError):
        return []

def _sauvegarder_publications(publications: list[dict]) -> None:
    FICHIER_PUBLICATIONS.parent.mkdir(parents=True, exist_ok=True)
    temporaire = FICHIER_PUBLICATIONS.with_suffix(".json.tmp")
    temporaire.write_text(json.dumps(publications, ensure_ascii=False, indent=2), encoding="utf-8")
    temporaire.replace(FICHIER_PUBLICATIONS)

def _chemin_piece_jointe(publication: dict) -> Path | None:
    relatif = str(publication.get("attachment_path", "")).strip()
    if not relatif:
        return None
    base = APP_DIR.resolve()
    chemin = (APP_DIR / relatif).resolve()
    if chemin == base or base not in chemin.parents:
        return None
    return chemin if chemin.is_file() else None

def _publication_en_cours(publication: dict, aujourd_hui: date | None = None) -> bool:
    if not publication.get("active", True):
        return False
    aujourd_hui = aujourd_hui or date.today()
    fin = str(publication.get("expires_on", "")).strip()
    if fin:
        try:
            if date.fromisoformat(fin) < aujourd_hui:
                return False
        except ValueError:
            pass
    return True

def _publications_pour_html(publications: list[dict]) -> list[dict]:
    """Prépare les annonces actives et intègre les pièces jointes dans le HTML hors ligne."""
    result: list[dict] = []
    poids_integre = 0
    for pub in publications:
        if not _publication_en_cours(pub):
            continue
        item = {
            "category": str(pub.get("category", "Avis")),
            "audience": str(pub.get("audience", "Étudiants et enseignants")),
            "title": str(pub.get("title", "")),
            "body": str(pub.get("body", "")),
            "created_at": str(pub.get("created_at", "")),
            "attachment_name": str(pub.get("attachment_name", "")),
            "attachment_data": "",
            "attachment_mime": str(pub.get("attachment_mime", "application/octet-stream")),
        }
        path = _chemin_piece_jointe(pub)
        if path:
            taille = path.stat().st_size
            if poids_integre + taille <= TAILLE_MAX_MEDIAS_HTML:
                mime = item["attachment_mime"] or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                item["attachment_mime"] = mime
                item["attachment_data"] = "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")
                poids_integre += taille
            else:
                item["attachment_name"] = ""  # évite un lien cassé dans le HTML hors ligne
        result.append(item)
    return result

def _page_html(records: list[dict], title: str, subtitle: str, *, filters: bool, speed: int = 24, autoplay: bool = True, layout: str = "Liste défilante", selected_teachers: list[str] | None = None, publications: list[dict] | None = None, auto_daily: bool = False, default_department: str = "ELT") -> str:
    data_js = _json_safe(records)
    news_js = _json_safe(_publications_pour_html(publications or []))
    filter_markup = ""
    if filters:
        filter_markup = """
        <div class="filters">
          <label>Jour<select id="f-day"></select></label>
          <label>Créneau horaire<select id="f-time"></select></label>
          <label>Promotion<select id="f-promo"></select></label>
          <label>Département<select id="f-dept"></select></label>
          <div class="filter-field"><span>Enseignant(s)</span>
            <div class="teacher-picker" id="teacherPicker">
              <button id="teacherToggle" class="picker-toggle" type="button" aria-expanded="false">Tous les enseignants</button>
              <div class="teacher-options" id="teacherOptions" hidden>
                <input id="teacherSearch" type="search" placeholder="Rechercher un enseignant…" aria-label="Rechercher un enseignant">
                <div class="teacher-checkboxes" id="teacherCheckboxes"></div>
                <div class="teacher-actions"><button id="teacherClear" type="button">Effacer</button><span id="teacherCount">Tous les enseignants</span></div>
              </div>
            </div>
          </div>
          <label>Type d'enseignement<select id="f-kind"></select></label>
          <button id="apply">Afficher l'EDT</button>
        </div>"""
    layout_lc = layout.lower()
    initial_layout = "teacher" if layout_lc.startswith("par enseignant") else ("grid" if layout_lc.startswith("grille") else "list")
    controls = """
      <button id="pauseBtn" type="button">⏸ Pause</button>
      <button id="fullscreenBtn" type="button">⛶ Plein écran</button>
      <label class="speed-label">Vitesse <input id="speed" type="range" min="5" max="80" step="5" value="24"></label>
      <label class="view-label">Vue <select id="view"><option value="list">Liste défilante</option><option value="grid">Grille</option><option value="teacher">Par enseignant</option></select></label>
      <label class="news-mode-toggle"><input id="newsFullMode" type="checkbox" checked> Avis en plein écran (automatique)</label>
      <div class="type-legend" aria-label="Légende des couleurs"><span class="legend-item cours"><i class="legend-dot"></i>Cours</span><span class="legend-item td"><i class="legend-dot"></i>TD</span><span class="legend-item tp"><i class="legend-dot"></i>TP</span><span class="legend-item atelier"><i class="legend-dot"></i>Atelier</span></div>"""
    auto_control_markup = ""
    if filters:
        checked = " checked" if auto_daily else ""
        auto_control_markup = f'<label class="auto-toggle"><input id="autoDaily" type="checkbox"{checked}> Automatique : jour du PC · {htmlmod.escape(default_department)} · créneau actuel + suivants · par promotion</label>'
    elif auto_daily:
        auto_control_markup = f'<span class="auto-badge" id="autoStatus">📅 Planning automatique du jour · {htmlmod.escape(default_department)} · créneau actuel + suivants</span>'
    return fr"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="application-name" content="EDT TV UDL-SBA"><title>{htmlmod.escape(title)}</title>
<style>
*{{box-sizing:border-box}} html,body{{margin:0;width:100%;height:100%;font-family:Arial,Helvetica,sans-serif;background:#07111f;color:#f8fafc}}
body{{display:flex;flex-direction:column;overflow:hidden}}
.top{{padding:16px 24px;background:linear-gradient(115deg,#0f172a,#1e40af 70%,#0891b2);display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:3px solid #38bdf8}}
.brand h1{{margin:0;font-size:clamp(22px,2.1vw,38px);letter-spacing:.2px}} .brand p{{margin:5px 0 0;color:#dbeafe;font-size:clamp(13px,1vw,18px)}}
.clock{{font-size:clamp(18px,1.8vw,30px);font-weight:800;white-space:nowrap;text-align:right}} .clock small{{display:block;font-size:.55em;color:#dbeafe;margin-top:3px}}
.filters{{display:flex;flex-wrap:wrap;gap:12px;align-items:end;padding:12px 22px;background:#0b1728;border-bottom:1px solid #29405b}}
label,.filter-field{{display:flex;flex-direction:column;gap:5px;color:#bfdbfe;font-size:13px;font-weight:700}} select,button,input{{font:inherit}} select{{min-width:180px;max-width:300px;padding:10px;background:#fff;color:#0f172a;border-radius:8px;border:1px solid #93c5fd}}
button{{background:#0ea5e9;color:#001a2a;border:none;border-radius:8px;padding:10px 14px;font-weight:800;cursor:pointer}} button:hover{{background:#7dd3fc}}
.teacher-picker{{position:relative;min-width:220px;max-width:320px;color:#0f172a;font-weight:400}}
.picker-toggle{{width:100%;min-height:39px;text-align:left;background:#fff;color:#0f172a;border:1px solid #93c5fd;font-weight:600;padding:9px 11px;white-space:normal}}
.teacher-options{{position:absolute;z-index:30;top:calc(100% + 5px);left:0;width:min(360px,85vw);max-height:360px;padding:10px;background:#fff;border:1px solid #93c5fd;border-radius:9px;box-shadow:0 12px 28px #0007;color:#0f172a}}
.teacher-options[hidden]{{display:none}} .teacher-options input[type=search]{{width:100%;padding:8px;border:1px solid #cbd5e1;border-radius:6px;margin-bottom:8px;color:#0f172a;background:#fff}}
.teacher-checkboxes{{max-height:245px;overflow:auto;border-top:1px solid #e2e8f0;border-bottom:1px solid #e2e8f0;padding:5px 0}}
.teacher-option{{display:flex;flex-direction:row;align-items:center;gap:8px;padding:6px 4px;color:#0f172a;font-size:13px;font-weight:500;cursor:pointer}}
.teacher-option input{{flex:0 0 auto}} .teacher-actions{{display:flex;align-items:center;justify-content:space-between;gap:10px;padding-top:8px;color:#334155;font-size:12px}}
.teacher-actions button{{background:#e2e8f0;color:#0f172a;padding:6px 9px;font-size:12px}}
.tools{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 22px;background:#081322;border-bottom:1px solid #263b54}}
.tools button{{padding:7px 11px;font-size:13px}} .speed-label,.view-label{{flex-direction:row;align-items:center}} input[type=range]{{width:120px}}
.type-legend{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:3px 5px;color:#e2e8f0;font-size:12px;font-weight:800}}
.legend-item{{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}}
.legend-dot{{width:11px;height:11px;border-radius:50%;display:inline-block}}
.legend-item.cours .legend-dot{{background:#60a5fa}} .legend-item.td .legend-dot{{background:#4ade80}} .legend-item.tp .legend-dot{{background:#f87171}} .legend-item.atelier .legend-dot{{background:#c084fc}}
.status{{margin-left:auto;color:#cbd5e1;font-weight:700;font-size:14px}}
.auto-toggle{{display:flex;flex-direction:row;align-items:center;gap:8px;color:#fef3c7;background:#713f12;border:1px solid #fbbf24;border-radius:8px;padding:8px 10px;font-size:12px;font-weight:800}}
 .auto-toggle input{{width:16px;height:16px;accent-color:#f59e0b}}
 .auto-badge{{display:inline-flex;align-items:center;padding:7px 10px;border-radius:8px;background:#713f12;color:#fef3c7;border:1px solid #fbbf24;font-size:12px;font-weight:900}}
.news-panel{{display:flex;align-items:center;gap:18px;flex:0 0 auto;padding:10px 22px;background:linear-gradient(110deg,#422006,#78350f 48%,#1e293b);border-bottom:2px solid #fbbf24;max-height:180px;overflow:hidden}}
.news-panel[hidden]{{display:none}}
.news-copy{{min-width:0;flex:1;overflow:hidden}}
.news-topline{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px}}
.news-category{{display:inline-block;background:#fbbf24;color:#422006;padding:4px 8px;border-radius:5px;font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.3px}}
.news-title{{font-size:clamp(16px,1.25vw,24px);font-weight:900;color:#fff;overflow-wrap:anywhere}}
#newsMeta{{font-size:12px;color:#fde68a;margin:3px 0}}
#newsBody{{margin:5px 0 0;color:#fff7ed;font-size:clamp(13px,1vw,18px);white-space:pre-wrap;overflow-wrap:anywhere;max-height:72px;overflow:hidden}}
.news-attachment{{display:inline-block;margin-top:6px;color:#fde68a;font-weight:800;font-size:13px}}
#newsImage{{max-height:90px;max-width:130px;object-fit:contain;border-radius:8px;background:#fff;padding:3px}}
.news-stamp{{flex:0 0 auto;text-align:center;color:#fde68a;font-size:12px;font-weight:900;letter-spacing:1px}}
.news-stamp strong{{display:block;font-size:25px;color:#fff;margin-bottom:4px}}
.news-mode-toggle{{display:flex;flex-direction:row;align-items:center;gap:7px;color:#fef3c7;font-weight:800;font-size:13px;white-space:nowrap}}
.news-mode-toggle input{{width:17px;height:17px;accent-color:#fbbf24}}
.news-fullscreen{{position:fixed;inset:0;z-index:1000;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:5vh 7vw;background:radial-gradient(circle at top right,#92400e,#422006 48%,#07111f 100%);color:#fff;overflow:hidden}}
.news-fullscreen[hidden]{{display:none}}
.news-fullscreen .full-category{{font-size:clamp(24px,2.4vw,42px);font-weight:900;letter-spacing:2px;text-transform:uppercase;color:#fde68a;margin-bottom:2vh}}
.news-fullscreen .full-title{{font-size:clamp(38px,4.5vw,82px);line-height:1.1;font-weight:900;margin:0 0 2vh;max-width:1500px;overflow-wrap:anywhere}}
.news-fullscreen .full-meta{{font-size:clamp(18px,1.5vw,28px);color:#fde68a;margin-bottom:3vh}}
.news-fullscreen .full-body{{font-size:clamp(24px,2.4vw,44px);line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;max-width:1500px;max-height:52vh;overflow:auto}}
.news-fullscreen .full-attachment{{margin-top:2vh;font-size:clamp(18px,1.4vw,26px);font-weight:800;color:#fde68a}}
.news-fullscreen img{{max-width:min(55vw,700px);max-height:25vh;object-fit:contain;margin-top:2vh;border-radius:12px;background:#fff;padding:8px}}
.upcoming-panel{{flex:0 0 auto;max-height:280px;overflow:hidden;padding:9px 22px 11px;background:linear-gradient(110deg,#422006,#7c2d12 48%,#1e293b);border-bottom:2px solid #fbbf24}}
.upcoming-panel[hidden]{{display:none}}
.upcoming-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:7px;color:#fff7ed}}
.upcoming-head strong{{font-size:clamp(16px,1.25vw,24px);font-weight:900;color:#fde68a}}
#upcomingMeta{{font-weight:800;color:#fff;font-size:clamp(13px,1vw,18px);margin-left:10px}}
.upcoming-count{{font-size:12px;color:#fde68a;font-weight:900;white-space:nowrap}}
.upcoming-cards{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;max-height:220px;overflow:auto;padding:2px 4px 5px 0}}
.upcoming-cards .card{{padding:8px 10px;border-left-width:5px;border-radius:8px}}
.upcoming-cards .subject{{font-size:clamp(13px,1vw,18px);line-height:1.18;margin-bottom:5px}}
.upcoming-cards .badges{{gap:4px;margin-bottom:5px}}
.upcoming-cards .badge{{font-size:10px;padding:3px 5px}}
.upcoming-cards .details{{grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:4px;font-size:clamp(10px,.75vw,13px)}}
#board{{position:relative;flex:1;overflow-y:auto;overscroll-behavior:contain;padding:20px 22px 34px;scrollbar-color:#38bdf8 #0f172a;scrollbar-width:thin}}
.day-heading{{font-size:clamp(18px,1.5vw,28px);font-weight:900;color:#7dd3fc;border-bottom:1px solid #33506f;padding:0 0 8px;margin:12px 0 14px}}
.hour-heading{{color:#fde68a;font-weight:900;font-size:clamp(15px,1.15vw,22px);margin:18px 0 8px}}

.auto-time-slot{{margin:0 0 26px;padding:0 0 18px;border-bottom:2px solid #294763}}
.auto-time-heading{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 13px;padding:10px 13px;border-radius:10px;background:#12253c;border-left:7px solid #64748b}}
.auto-time-heading .slot-state{{display:inline-block;padding:5px 9px;border-radius:6px;font-size:clamp(11px,.9vw,15px);font-weight:900;letter-spacing:.4px;background:#334155;color:#f8fafc}}
.auto-time-heading .slot-time{{font-size:clamp(18px,1.55vw,29px);font-weight:900;color:#fff}}
.auto-time-heading .slot-count{{margin-left:auto;font-size:clamp(11px,.85vw,14px);font-weight:800;color:#cbd5e1}}
.auto-time-slot.is-current .auto-time-heading{{background:linear-gradient(100deg,#064e3b,#14532d);border-left-color:#4ade80}}
.auto-time-slot.is-current .slot-state{{background:#4ade80;color:#052e16}}
.auto-time-slot.is-next .auto-time-heading{{background:linear-gradient(100deg,#713f12,#422006);border-left-color:#fbbf24}}
.auto-time-slot.is-next .slot-state{{background:#fbbf24;color:#422006}}
.auto-time-slot.is-future .auto-time-heading{{border-left-color:#38bdf8}}
.auto-time-slot.is-future .slot-state{{background:#075985;color:#e0f2fe}}

.cards{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}
.card{{border:1px solid #344d6c;border-left:7px solid #38bdf8;border-radius:12px;background:linear-gradient(120deg,#102038,#142944);padding:15px 17px;box-shadow:0 5px 15px #0003;break-inside:avoid}}
/* Couleurs distinctes selon le type d'enseignement : Cours bleu, TD vert, TP rouge. */
.card.cours{{border-color:#60a5fa;border-left-color:#93c5fd;background:linear-gradient(120deg,#123d80,#1e40af)}}
.card.td{{border-color:#4ade80;border-left-color:#86efac;background:linear-gradient(120deg,#14532d,#166534)}}
.card.tp{{border-color:#f87171;border-left-color:#fca5a5;background:linear-gradient(120deg,#7f1d1d,#b91c1c)}}
.card.atelier{{border-color:#c084fc;border-left-color:#d8b4fe;background:linear-gradient(120deg,#581c87,#7e22ce)}}
.subject{{font-size:clamp(18px,1.55vw,28px);line-height:1.22;font-weight:900;color:#fff;margin-bottom:10px}}
.badges{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:9px}} .badge{{border-radius:6px;padding:4px 8px;font-size:12px;font-weight:900;background:#1d4ed8;color:white}}
.badge.tp{{background:#fecaca;color:#7f1d1d}} .badge.td{{background:#bbf7d0;color:#14532d}} .badge.cours{{background:#bfdbfe;color:#1e3a8a}} .badge.atelier{{background:#f3e8ff;color:#581c87}}
.details{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:7px;color:#dbeafe;font-size:clamp(13px,1.05vw,18px)}} .details b{{color:#7dd3fc}}
.grid-table{{width:100%;border-collapse:separate;border-spacing:5px;table-layout:fixed}} .grid-table th{{background:#1e40af;color:white;padding:12px 6px;border-radius:7px;font-size:clamp(14px,1.15vw,20px)}} .grid-table td{{vertical-align:top;background:#0e1d30;border:1px solid #2f4968;border-radius:8px;padding:7px;overflow-wrap:anywhere}}
.grid-item{{background:#142944;border-left:4px solid #38bdf8;padding:8px;margin:5px 0;border-radius:5px;font-size:clamp(12px,.95vw,17px)}}
.grid-item.cours{{background:#1e3a8a;border-left-color:#60a5fa}} .grid-item.td{{background:#14532d;border-left-color:#4ade80}} .grid-item.tp{{background:#7f1d1d;border-left-color:#f87171}} .grid-item.atelier{{background:#581c87;border-left-color:#c084fc}}
.grid-item strong{{display:block;color:white;margin-bottom:4px}} .grid-item small{{color:#f1f5f9}}
.empty{{text-align:center;color:#cbd5e1;padding:60px 20px;font-size:clamp(20px,2vw,32px)}} .foot{{padding:7px 20px;background:#06101d;color:#8fa8c4;font-size:12px;display:flex;justify-content:space-between;gap:12px}}
@media(max-width:800px){{.top{{align-items:flex-start;flex-direction:column}} .clock{{text-align:left}} .filters label{{width:100%}} .filters select{{max-width:none;width:100%}} .status{{margin-left:0}} .grid-table{{table-layout:auto}} .cards{{grid-template-columns:1fr}} .auto-time-heading .slot-count{{margin-left:0}} .upcoming-cards{{grid-template-columns:repeat(2,minmax(0,1fr))}} .upcoming-panel{{padding:8px 12px}} .news-panel{{align-items:flex-start;gap:8px;padding:9px 12px}} .news-stamp{{display:none}} #newsBody{{max-height:60px}}}}

/* TYPOGRAPHIE TV AGRANDIE : priorité à la lecture à distance */
.brand h1 {{font-size:clamp(30px,2.35vw,50px)!important;line-height:1.08;letter-spacing:.4px}}
.brand p {{font-size:clamp(18px,1.3vw,26px)!important;line-height:1.25}}
.clock {{font-size:clamp(30px,2.4vw,48px)!important}}
.status {{font-size:clamp(16px,1.15vw,24px)!important}}
.auto-toggle,.auto-badge {{font-size:clamp(15px,1.05vw,22px)!important;padding:12px 16px}}
#board {{padding:24px 30px 54px}}
.auto-time-slot {{margin-bottom:36px;padding-bottom:24px}}
.auto-time-heading {{gap:16px;margin:7px 0 18px;padding:16px 20px;border-radius:13px}}
.auto-time-heading .slot-state {{font-size:clamp(17px,1.2vw,24px)!important;padding:10px 14px}}
.auto-time-heading .slot-time {{font-size:clamp(28px,2.3vw,46px)!important;line-height:1.1}}
.auto-time-heading .slot-count {{font-size:clamp(16px,1.1vw,22px)!important}}
.cards {{gap:18px}}
.card {{padding:23px 25px;border-left-width:10px;border-radius:14px}}
#board .subject {{font-size:clamp(26px,2.25vw,48px)!important;line-height:1.16;margin-bottom:15px;letter-spacing:.1px}}
#board .badges {{gap:9px;margin-bottom:13px}}
#board .badge {{font-size:clamp(16px,1.05vw,22px)!important;padding:7px 11px;line-height:1.2}}
#board .details {{font-size:clamp(20px,1.48vw,34px)!important;line-height:1.32;gap:11px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}}
#board .details b {{font-weight:900}}
.day-heading {{font-size:clamp(26px,2vw,40px)!important;line-height:1.15;padding-bottom:12px}}
.hour-heading {{font-size:clamp(22px,1.65vw,34px)!important}}
.grid-table th {{font-size:clamp(22px,1.65vw,34px)!important;padding:16px 9px}}
.grid-item {{font-size:clamp(18px,1.35vw,29px)!important;padding:12px;margin:8px 0;border-left-width:7px;line-height:1.3}}
.grid-item strong {{font-size:1.1em;line-height:1.2}}
.grid-item small {{font-size:1em;line-height:1.4}}
.empty {{font-size:clamp(28px,2.5vw,48px)!important;line-height:1.25}}
.upcoming-panel {{max-height:340px;padding:14px 24px 16px}}
.upcoming-head strong {{font-size:clamp(22px,1.7vw,34px)!important}}
#upcomingMeta {{font-size:clamp(18px,1.3vw,27px)!important}}
.upcoming-count {{font-size:clamp(16px,1.05vw,22px)!important}}
.upcoming-cards {{gap:10px;max-height:265px}}
.upcoming-cards .card {{padding:12px 14px}}
.upcoming-cards .subject {{font-size:clamp(18px,1.35vw,28px)!important;line-height:1.2}}
.upcoming-cards .badge {{font-size:clamp(14px,.95vw,19px)!important;padding:5px 7px}}
.upcoming-cards .details {{font-size:clamp(15px,1.05vw,22px)!important;grid-template-columns:1fr;line-height:1.3}}
.news-title {{font-size:clamp(22px,1.7vw,34px)!important}}
#newsBody {{font-size:clamp(18px,1.3vw,27px)!important;max-height:110px;line-height:1.3}}
.news-category {{font-size:clamp(15px,1vw,20px)!important;padding:6px 10px}}
@media(max-width:800px) {{
  .brand h1 {{font-size:30px!important}}
  .brand p {{font-size:18px!important}}
  .clock {{font-size:30px!important}}
  #board {{padding:16px 12px 36px}}
  .cards {{grid-template-columns:1fr}}
  #board .subject {{font-size:26px!important}}
  #board .details {{font-size:20px!important;grid-template-columns:1fr}}
  .auto-time-heading .slot-time {{font-size:28px!important}}
  .upcoming-cards {{grid-template-columns:1fr}}
}}

</style></head>
<body>
<header class="top"><div class="brand"><h1>{htmlmod.escape(title)}</h1><p>{htmlmod.escape(subtitle)}</p></div><div class="clock"><span id="clock">--:--:--</span><small id="date"></small></div></header>
{filter_markup}
<div class="tools">{controls}{auto_control_markup}<span class="status" id="status">Chargement des emplois du temps…</span></div>
<section class="news-panel" id="newsPanel" hidden aria-live="polite">
  <div class="news-copy">
    <div class="news-topline"><span class="news-category" id="newsCategory">AVIS</span><strong class="news-title" id="newsTitle"></strong></div>
    <div id="newsMeta"></div><p id="newsBody"></p>
    <a id="newsAttachment" class="news-attachment" hidden target="_blank" rel="noopener noreferrer"></a>
  </div>
  <img id="newsImage" alt="Illustration de l'annonce" hidden>
  <div class="news-stamp"><strong>INFO</strong>ACTUALITÉS<br>ET AVIS</div>
</section>
<section class="upcoming-panel" id="upcomingPanel" hidden aria-live="polite">
  <div class="upcoming-head"><div><strong id="upcomingTitle">⏭ PROCHAIN CRÉNEAU</strong><span id="upcomingMeta"></span></div><span class="upcoming-count" id="upcomingCount"></span></div>
  <div class="upcoming-cards" id="upcomingCards"></div>
</section>
<section class="news-fullscreen" id="newsFullscreen" hidden aria-live="polite">
  <div class="full-category" id="fullNewsCategory">AVIS</div>
  <h2 class="full-title" id="fullNewsTitle"></h2>
  <div class="full-meta" id="fullNewsMeta"></div>
  <div class="full-body" id="fullNewsBody"></div>
  <a class="full-attachment" id="fullNewsAttachment" hidden target="_blank" rel="noopener noreferrer"></a>
  <img id="fullNewsImage" alt="Illustration de la publication" hidden>
</section>
<main id="board" aria-live="polite"></main><footer class="foot"><span>Université Djillali Liabès · Faculté de Génie Électrique</span><span>Affichage TV — actualisé dans cette page</span></footer>
<script>
const DATA={data_js};
const NEWS={news_js};
const DAYS={json.dumps(JOURS_ORDRE, ensure_ascii=False)};
const INITIAL_SPEED={int(speed)};
const PRESELECTED_TEACHERS={json.dumps(selected_teachers or [], ensure_ascii=False)};
const AUTO_DAILY_DEFAULT={str(bool(auto_daily)).lower()};
const DEFAULT_DEPARTMENT={json.dumps(default_department, ensure_ascii=False)};
let autoplay={str(bool(autoplay)).lower()}, stopped=false, lastFrame=0, speed=INITIAL_SPEED;
let autoMode=AUTO_DAILY_DEFAULT, autoDateKey='', autoBaseTimestamp=Date.now(), autoDaySlots=[], autoStartIndex=0, autoRenderedSignature='';
let activeLayout={json.dumps(initial_layout)};
const board=document.getElementById('board'), statusEl=document.getElementById('status');
function esc(s){{return String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function dayIndex(s){{let n=String(s||'').toLowerCase();return DAYS.findIndex(d=>d.toLowerCase()===n);}}
function timeStart(s){{let m=String(s||'').match(/(\d{{1,2}})\s*h\s*(\d{{0,2}})/i);return m?Number(m[1])*60+Number(m[2]||0):99999;}}
function timeMatches(value,selected){{let t=timeStart(value);if(selected==='Matin')return t>=480&&t<750;if(selected==='Après-midi')return t>=750&&t<1020;return value===selected;}}
function normalizeKey(value){{return String(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\s+/g,' ').trim().toLocaleLowerCase('fr');}}
function pcDayName(){{return new Intl.DateTimeFormat('fr-FR',{{weekday:'long'}}).format(new Date());}}
function pcDateKey(now){{return `${{now.getFullYear()}}-${{String(now.getMonth()+1).padStart(2,'0')}}-${{String(now.getDate()).padStart(2,'0')}}`;}}
function timeRange(value){{const matches=[...String(value||'').matchAll(/(\d{{1,2}})\s*h\s*(\d{{0,2}})/gi)].map(m=>Number(m[1])*60+Number(m[2]||0));const start=matches.length?matches[0]:timeStart(value);const end=matches.length>1?matches[1]:start+90;return {{start,end}};}}
function teacherNames(value){{return String(value||'').split(/[\/+]/).map(n=>n.replace(/\s+/g,' ').trim().replace(/^[ .;,]+|[ .;,]+$/g,'')).filter(Boolean);}}
function teacherKey(value){{return String(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\s+/g,' ').trim().toLocaleLowerCase('fr');}}
function natural(a,b){{return String(a).localeCompare(String(b),'fr',{{numeric:true,sensitivity:'base'}});}}
function uniqueSorted(key, order){{let vals=[...new Set(DATA.map(r=>String(r[key]||'').trim()).filter(Boolean))];if(order)vals.sort((a,b)=>timeStart(a)-timeStart(b)||natural(a,b));else if(key==='Jours')vals.sort((a,b)=>(dayIndex(a)+100)-(dayIndex(b)+100)||natural(a,b));else vals.sort(natural);return vals;}}
function fillSelect(id, first, vals){{let el=document.getElementById(id);if(!el)return;el.innerHTML='';let opt=document.createElement('option');opt.value='';opt.textContent=first;el.appendChild(opt);vals.forEach(v=>{{let o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);}});}}
function promotionsForDepartment(dept){{const relevant=DATA.filter(r=>!dept||normalizeKey(r.Département)===normalizeKey(dept));return [...new Set(relevant.map(r=>String(r.Promotion||'').trim()).filter(Boolean))].sort(natural);}}
function kind(r){{const fourni=String(r.TypeEnseignement||'').trim().toLowerCase();if(fourni==='tp')return 'TP';if(fourni==='td')return 'TD';if(fourni==='atelier')return 'Atelier';if(fourni==='cours')return 'Cours';let code=String(r.Code||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').trim().toLowerCase();let titre=String(r.Enseignements||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').trim().toLowerCase();const valeurs=[code,titre];for(const v of valeurs){{if(/^tp(?:\d+)?(?=$|[\s\-_/])/.test(v))return 'TP';if(/^td(?:\d+)?(?=$|[\s\-_/])/.test(v))return 'TD';if(/^atelier(?=$|[\s\-_/])/.test(v))return 'Atelier';if(/^cours(?=$|[\s\-_/])/.test(v))return 'Cours';}}if(/travaux pratiques?/.test(code+' '+titre))return 'TP';if(/travaux diriges?/.test(code+' '+titre))return 'TD';return 'Cours';}}
function card(r){{let k=kind(r).toLowerCase();let typeLabel=k==='cours'?'COURS':k.toUpperCase();return `<article class="card ${{k}}"><div class="subject">${{esc(r.Enseignements||'Enseignement non renseigné')}}</div><div class="badges"><span class="badge ${{k}}">${{typeLabel}}</span><span class="badge">${{esc(r.Code||'Code non renseigné')}}</span><span class="badge">${{esc(r.Horaire||'Horaire non renseigné')}}</span><span class="badge">${{esc(r.Jours||'Jour non renseigné')}}</span></div><div class="details"><span><b>Enseignant(s) :</b> ${{esc(r.Enseignants||'Non renseigné')}}</span><span><b>Lieu :</b> ${{esc(r.Lieu||'Non renseigné')}}</span><span><b>Promotion :</b> ${{esc(r.Promotion||'Non renseignée')}}</span><span><b>Département :</b> ${{esc(r.Département||'Non renseigné')}}</span></div></article>`;}}
function renderList(rows){{if(!rows.length){{board.innerHTML='<div class="empty">Aucun enseignement ne correspond aux filtres sélectionnés.</div>';return;}}let html='',lastDay='';rows.forEach(r=>{{if(r.Jours!==lastDay){{if(lastDay)html+='</div>';lastDay=r.Jours;html+=`<h2 class="day-heading">${{esc(lastDay)}}</h2><div class="cards">`;}}html+=card(r);}});if(lastDay)html+='</div>';board.innerHTML=html;}}
function promotionSorted(rows){{return [...rows].sort((a,b)=>natural(a.Promotion,b.Promotion)||timeStart(a.Horaire)-timeStart(b.Horaire)||natural(a.Enseignements,b.Enseignements)||natural(a.Code,b.Code));}}
function hideUpcomingPanel(){{const panel=document.getElementById('upcomingPanel');if(panel)panel.hidden=true;}}
function renderGrid(rows){{if(!rows.length){{board.innerHTML='<div class="empty">Aucun enseignement ne correspond aux filtres sélectionnés.</div>';return;}}let days=[...new Set(rows.map(r=>r.Jours))].sort((a,b)=>(dayIndex(a)+100)-(dayIndex(b)+100));let hours=[...new Set(rows.map(r=>r.Horaire))].sort((a,b)=>timeStart(a)-timeStart(b)||natural(a,b));let html='<table class="grid-table"><thead><tr><th>Créneau</th>'+days.map(d=>`<th>${{esc(d)}}</th>`).join('')+'</tr></thead><tbody>';hours.forEach(h=>{{html+=`<tr><th>${{esc(h)}}</th>`;days.forEach(d=>{{let cell=rows.filter(r=>r.Horaire===h&&r.Jours===d);html+='<td>'+cell.map(r=>`<div class="grid-item ${{kind(r).toLowerCase()}}"><strong>${{esc(r.Enseignements)}}</strong><small>${{esc(r.Code)}} · ${{esc(r.Promotion)}}<br>${{esc(r.Enseignants)}}<br>${{esc(r.Lieu)}} · ${{esc(r.Département)}}</small></div>`).join('')+'</td>';}});html+='</tr>';}});html+='</tbody></table>';board.innerHTML=html;}}
function getSelectedTeachers(){{let boxes=document.querySelectorAll('#teacherCheckboxes input[type="checkbox"]');if(boxes.length)return [...boxes].filter(b=>b.checked).map(b=>b.value);return PRESELECTED_TEACHERS;}}
function renderTeachers(rows){{let chosen=getSelectedTeachers();if(!chosen.length){{board.innerHTML='<div class="empty">Choisissez un ou plusieurs enseignants dans le filtre « Enseignant(s) ».</div>';return;}}let html='';chosen.forEach(name=>{{let matches=rows.filter(r=>teacherNames(r.Enseignants).some(n=>teacherKey(n)===teacherKey(name)));html+=`<h2 class="day-heading">👤 ${{esc(name)}} <span style="font-size:.7em;color:#bfdbfe">— ${{matches.length}} séance(s)</span></h2>`;if(matches.length){{html+='<div class="cards">'+matches.map(card).join('')+'</div>';}}else{{html+='<div class="empty" style="padding:16px">Aucune séance pour cet enseignant avec les autres filtres.</div>';}}}});board.innerHTML=html;}}
function autoRowsForPcDay(){{const day=normalizeKey(pcDayName());return DATA.filter(r=>normalizeKey(r.Département)===normalizeKey(DEFAULT_DEPARTMENT)&&normalizeKey(r.Jours)===day).sort((a,b)=>timeStart(a.Horaire)-timeStart(b.Horaire)||natural(a.Promotion,b.Promotion)||natural(a.Enseignements,b.Enseignements));}}
function initializeAutoDay(now,rows) {{
  const key=pcDateKey(now);
  if(key!==autoDateKey){{
    autoDateKey=key;
    autoRenderedSignature='';
    autoDaySlots=[...new Set(rows.map(r=>String(r.Horaire||'').trim()).filter(Boolean))].sort((a,b)=>timeStart(a)-timeStart(b)||natural(a,b));
  }}
}}
function renderAutoTimeline(rows,now){{
  const nowSeconds=now.getHours()*3600+now.getMinutes()*60+now.getSeconds()+now.getMilliseconds()/1000;
  const visibleRows=rows.filter(r=>{{
    const range=timeRange(r.Horaire);
    return Number.isFinite(range.start)&&range.start<1440&&Number.isFinite(range.end)&&range.end*60>nowSeconds;
  }});
  const groupsMap=new Map();
  visibleRows.forEach(r=>{{
    const label=String(r.Horaire||'Horaire non renseigné').trim();
    if(!groupsMap.has(label))groupsMap.set(label,[]);
    groupsMap.get(label).push(r);
  }});
  const slots=[...groupsMap.entries()].map(([label,items])=>({{label,range:timeRange(label),items:promotionSorted(items)}}))
    .sort((a,b)=>a.range.start-b.range.start||a.range.end-b.range.end||natural(a.label,b.label));
  const statusFor=slot=>(slot.range.start*60<=nowSeconds&&nowSeconds<slot.range.end*60)?'CURRENT':'FUTURE';
  const signature=pcDateKey(now)+'|'+JSON.stringify(slots.map(slot=>[slot.label,statusFor(slot),slot.items.map(r=>[r.Promotion,r.Code,r.Enseignements,r.Enseignants,r.Lieu])]));
  if(!slots.length){{
    const message=rows.length
      ?'Tous les créneaux du département '+DEFAULT_DEPARTMENT+' sont terminés pour aujourd’hui.'
      :'Aucune séance du département '+DEFAULT_DEPARTMENT+' n’est programmée pour '+pcDayName()+'.';
    if(signature!==autoRenderedSignature){{board.innerHTML='<div class="empty">'+esc(message)+'</div>';board.scrollTop=0;autoRenderedSignature=signature;}}
    return {{slots:[],visibleCount:0,nowSeconds}};
  }}
  if(signature!==autoRenderedSignature){{
    let futureIndex=0;
    const sections=slots.map(slot=>{{
      const state=statusFor(slot);
      let cssState='is-future',stateLabel='À VENIR';
      if(state==='CURRENT'){{cssState='is-current';stateLabel='● EN COURS';}}
      else if(futureIndex++===0){{cssState='is-next';stateLabel='⏭ PROCHAIN CRÉNEAU';}}
      const promoCount=new Set(slot.items.map(r=>String(r.Promotion||'').trim()).filter(Boolean)).size;
      return `<section class="auto-time-slot ${{cssState}}"><div class="auto-time-heading"><span class="slot-state">${{stateLabel}}</span><strong class="slot-time">${{esc(slot.label)}}</strong><span class="slot-count">${{promoCount}} promotion(s) · ${{slot.items.length}} séance(s) · tri par promotion</span></div><div class="cards">${{slot.items.map(card).join('')}}</div></section>`;
    }}).join('');
    board.innerHTML='<div class="auto-timeline">'+sections+'</div>';
    board.scrollTop=0;
    autoRenderedSignature=signature;
  }}
  return {{slots,visibleCount:visibleRows.length,nowSeconds}};
}}
function updateAutoSlotRender(){{
  const now=new Date(),rows=autoRowsForPcDay();
  initializeAutoDay(now,rows);
  hideUpcomingPanel();
  const result=renderAutoTimeline(rows,now);
  const current=result.slots.filter(slot=>slot.range.start*60<=result.nowSeconds&&result.nowSeconds<slot.range.end*60);
  const next=result.slots.find(slot=>slot.range.start*60>result.nowSeconds);
  let positionText='';
  if(current.length)positionText='En cours : '+current.map(slot=>slot.label).join(' / ');
  else if(next)positionText='Prochain : '+next.label;
  else positionText='Journée terminée';
  if(current.length&&next)positionText+=' · ensuite : '+next.label;
  statusEl.textContent='MODE AUTOMATIQUE · DÉPARTEMENT '+DEFAULT_DEPARTMENT+' · '+pcDayName()+' · '+positionText+' · '+result.visibleCount+' séance(s) restantes';
  const stamp=document.getElementById('autoStatus');
  if(stamp)stamp.textContent='📅 '+pcDayName()+' · '+DEFAULT_DEPARTMENT+' · Créneau actuel + tous les suivants';
}}
function disableAutomaticMode(){{const autoEl=document.getElementById('autoDaily');if(autoEl&&autoEl.checked)autoEl.checked=false;autoMode=false;autoRenderedSignature='';hideUpcomingPanel();}}
function kindsForSelection(){{let choice=document.getElementById('f-kind')?.value||'';if(!choice)return ['Cours','TD','TP','Atelier'];if(choice.includes('+'))return choice.split('+').map(x=>x.trim());return [choice];}}
function getFilteredRows(){{let day=document.getElementById('f-day')?.value||'',tm=document.getElementById('f-time')?.value||'',pr=document.getElementById('f-promo')?.value||'',de=document.getElementById('f-dept')?.value||'',types=kindsForSelection(),teachers=getSelectedTeachers(),teacherKeys=teachers.map(teacherKey);return DATA.filter(r=>(!day||r.Jours===day)&&(!tm||timeMatches(r.Horaire,tm))&&(!pr||r.Promotion===pr)&&(!de||r.Département===de)&&(!teacherKeys.length||teacherNames(r.Enseignants).some(n=>teacherKeys.includes(teacherKey(n))))&&types.includes(kind(r))).sort((a,b)=>(dayIndex(a.Jours)+100)-(dayIndex(b.Jours)+100)||timeStart(a.Horaire)-timeStart(b.Horaire)||natural(a.Promotion,b.Promotion)||natural(a.Enseignements,b.Enseignements));}}
function updateRender(){{if(autoMode){{updateAutoSlotRender();return;}}hideUpcomingPanel();let rows=getFilteredRows();if(activeLayout==='grid')renderGrid(rows);else if(activeLayout==='teacher')renderTeachers(rows);else renderList(rows);statusEl.textContent=rows.length.toLocaleString('fr-FR')+' séance(s) affichée(s)';board.scrollTop=0;}}
function syncTeacherPicker(){{let names=getSelectedTeachers(),button=document.getElementById('teacherToggle'),count=document.getElementById('teacherCount');if(button)button.textContent=names.length?`${{names.length}} enseignant(s) sélectionné(s)`:'Tous les enseignants';if(count)count.textContent=names.length?`${{names.length}} sélectionné(s)`:'Tous les enseignants';}}
function initTeacherPicker(){{let host=document.getElementById('teacherCheckboxes');if(!host)return;let names=[...new Set(DATA.flatMap(r=>teacherNames(r.Enseignants)).filter(Boolean))].sort(natural);names.forEach((name,i)=>{{let label=document.createElement('label');label.className='teacher-option';label.dataset.search=teacherKey(name);let box=document.createElement('input');box.type='checkbox';box.value=name;box.id='teacher-option-'+i;box.addEventListener('change',()=>{{disableAutomaticMode();syncTeacherPicker();updateRender();}});let txt=document.createElement('span');txt.textContent=name;label.append(box,txt);host.appendChild(label);}});let toggle=document.getElementById('teacherToggle'),options=document.getElementById('teacherOptions'),search=document.getElementById('teacherSearch');toggle?.addEventListener('click',()=>{{let open=options.hidden;options.hidden=!open;toggle.setAttribute('aria-expanded',String(open));if(open)search?.focus();}});search?.addEventListener('input',()=>{{let q=teacherKey(search.value);host.querySelectorAll('.teacher-option').forEach(el=>{{el.style.display=el.dataset.search.includes(q)?'flex':'none';}});}});document.getElementById('teacherClear')?.addEventListener('click',()=>{{disableAutomaticMode();host.querySelectorAll('input[type="checkbox"]').forEach(b=>b.checked=false);syncTeacherPicker();updateRender();}});document.addEventListener('click',ev=>{{let picker=document.getElementById('teacherPicker');if(picker&&!picker.contains(ev.target)){{options.hidden=true;toggle?.setAttribute('aria-expanded','false');}}}});syncTeacherPicker();}}
function renderNews(index){{let panel=document.getElementById('newsPanel');if(!panel)return;if(!NEWS.length){{panel.hidden=true;return;}}let item=NEWS[index%NEWS.length];panel.hidden=false;document.getElementById('newsCategory').textContent=item.category||'Avis';document.getElementById('newsTitle').textContent=item.title||'Information';document.getElementById('newsMeta').textContent=[item.audience||'Étudiants et enseignants',item.created_at?'Publié le '+item.created_at:''].filter(Boolean).join(' · ');document.getElementById('newsBody').textContent=item.body||'';let link=document.getElementById('newsAttachment'),img=document.getElementById('newsImage');link.hidden=true;link.removeAttribute('href');img.hidden=true;img.removeAttribute('src');if(item.attachment_data&&item.attachment_name){{link.href=item.attachment_data;link.download=item.attachment_name;link.textContent='📎 Ouvrir / télécharger : '+item.attachment_name;link.hidden=false;if(String(item.attachment_mime||'').startsWith('image/')){{img.src=item.attachment_data;img.alt=item.attachment_name;img.hidden=false;}}}}}}
let newsIndex=0;renderNews(newsIndex);if(NEWS.length>1)setInterval(()=>{{newsIndex=(newsIndex+1)%NEWS.length;renderNews(newsIndex);}},14000);
// Mode optionnel : une publication en plein écran, puis retour automatique à l'EDT.
let fullNewsTimer=null, fullNewsShowing=false, fullNewsIndex=0;
const fullNewsToggle=document.getElementById('newsFullMode');
const fullNewsPanel=document.getElementById('newsFullscreen');
function fillFullNews(index){{
  const item=NEWS[index%NEWS.length];
  document.getElementById('fullNewsCategory').textContent=item.category||'Avis';
  document.getElementById('fullNewsTitle').textContent=item.title||'Information';
  document.getElementById('fullNewsMeta').textContent=[item.audience||'Étudiants et enseignants',item.created_at?'Publié le '+item.created_at:''].filter(Boolean).join(' · ');
  document.getElementById('fullNewsBody').textContent=item.body||'';
  const link=document.getElementById('fullNewsAttachment'),img=document.getElementById('fullNewsImage');
  link.hidden=true;link.removeAttribute('href');img.hidden=true;img.removeAttribute('src');
  if(item.attachment_data&&item.attachment_name){{
    link.href=item.attachment_data;link.download=item.attachment_name;link.textContent='📎 Ouvrir / télécharger : '+item.attachment_name;link.hidden=false;
    if(String(item.attachment_mime||'').startsWith('image/')){{img.src=item.attachment_data;img.alt=item.attachment_name;img.hidden=false;}}
  }}
}}
function stopFullNewsCycle(){{
  if(fullNewsTimer){{clearTimeout(fullNewsTimer);fullNewsTimer=null;}}
  fullNewsShowing=false;if(fullNewsPanel)fullNewsPanel.hidden=true;
}}
function scheduleFullNewsCycle(delay){{
  if(fullNewsTimer)clearTimeout(fullNewsTimer);
  fullNewsTimer=setTimeout(()=>{{
    if(!fullNewsToggle||!fullNewsToggle.checked||!NEWS.length){{stopFullNewsCycle();return;}}
    if(!fullNewsShowing){{
      fillFullNews(fullNewsIndex);fullNewsPanel.hidden=false;fullNewsShowing=true;
      fullNewsTimer=setTimeout(()=>{{
        fullNewsPanel.hidden=true;fullNewsShowing=false;fullNewsIndex=(fullNewsIndex+1)%NEWS.length;
        scheduleFullNewsCycle(45000);
      }},12000);
    }}else{{fullNewsPanel.hidden=true;fullNewsShowing=false;scheduleFullNewsCycle(45000);}}
  }},delay);
}}
if(fullNewsToggle){{
  if(NEWS.length){{scheduleFullNewsCycle(15000);}}
  else{{fullNewsToggle.checked=false;fullNewsToggle.disabled=true;fullNewsToggle.parentElement.title='Aucun avis ou annonce actif : mode indisponible';}}
  fullNewsToggle.addEventListener('change',()=>{{
    stopFullNewsCycle();
    if(fullNewsToggle.checked&&NEWS.length)scheduleFullNewsCycle(1000);
  }});
}}
if(document.getElementById('f-day')){{fillSelect('f-day','Tous les jours',uniqueSorted('Jours'));fillSelect('f-time','Tous les créneaux',['Matin','Après-midi',...uniqueSorted('Horaire',true)]);fillSelect('f-dept','Tous les départements',uniqueSorted('Département'));fillSelect('f-kind','Tous les enseignements',['Cours','TD','TP','Atelier','Cours + TD','TD + TP','Cours + TP']);const daySelect=document.getElementById('f-day');const today=normalizeKey(pcDayName());const todayOpt=[...daySelect.options].find(o=>normalizeKey(o.value)===today);if(todayOpt)daySelect.value=todayOpt.value;const deptSelect=document.getElementById('f-dept');const eltOpt=[...deptSelect.options].find(o=>normalizeKey(o.value)===normalizeKey(DEFAULT_DEPARTMENT));if(eltOpt)deptSelect.value=eltOpt.value;const promoSelect=document.getElementById('f-promo');function refreshPromotionOptions(){{const previous=promoSelect.value;const options=promotionsForDepartment(deptSelect.value);fillSelect('f-promo','Toutes les promotions',options);promoSelect.value=options.includes(previous)?previous:'';}}refreshPromotionOptions();deptSelect.addEventListener('change',()=>{{refreshPromotionOptions();disableAutomaticMode();updateRender();}});initTeacherPicker();const autoEl=document.getElementById('autoDaily');if(autoEl){{autoMode=autoEl.checked;autoEl.addEventListener('change',()=>{{autoMode=autoEl.checked;autoDateKey='';autoRenderedSignature='';updateRender();}});}}document.getElementById('apply').addEventListener('click',()=>{{disableAutomaticMode();updateRender();}});['f-day','f-time','f-promo','f-kind'].forEach(id=>document.getElementById(id).addEventListener('change',()=>{{disableAutomaticMode();updateRender();}}));}}
let speedEl=document.getElementById('speed');if(speedEl){{speedEl.value=INITIAL_SPEED;speedEl.addEventListener('input',()=>{{speed=Number(speedEl.value);}});}}
let viewEl=document.getElementById('view');if(viewEl){{viewEl.value=activeLayout;viewEl.addEventListener('change',()=>{{activeLayout=viewEl.value;updateRender();}});}}
let pauseBtn=document.getElementById('pauseBtn');if(pauseBtn)pauseBtn.addEventListener('click',()=>{{stopped=!stopped;pauseBtn.textContent=stopped?'▶ Reprendre':'⏸ Pause';}});
let fsBtn=document.getElementById('fullscreenBtn');if(fsBtn)fsBtn.addEventListener('click',()=>{{if(!document.fullscreenElement)document.documentElement.requestFullscreen?.();else document.exitFullscreen?.();}});
function tickClock(){{const now=new Date();document.getElementById('clock').textContent=now.toLocaleTimeString('fr-FR');document.getElementById('date').textContent=now.toLocaleDateString('fr-FR',{{weekday:'long',year:'numeric',month:'long',day:'numeric'}});}}
setInterval(()=>{{tickClock();if(autoMode)updateAutoSlotRender();}},1000);tickClock();
function scrollFrame(ts){{if(!lastFrame)lastFrame=ts;let dt=Math.min(50,ts-lastFrame);lastFrame=ts;if(autoplay&&!stopped&&board.scrollHeight>board.clientHeight+10){{board.scrollTop+=speed*dt/1000;if(board.scrollTop+board.clientHeight>=board.scrollHeight-2)board.scrollTop=0;}}requestAnimationFrame(scrollFrame);}}
updateRender();requestAnimationFrame(scrollFrame);
</script></body></html>"""

st.markdown(f"""<div class="tv-intro"><h1>📺 {APP_TITLE}</h1><p>Affichage plein écran, défilement automatique et version HTML autonome pour clé USB.</p></div>""", unsafe_allow_html=True)
uploaded = st.file_uploader("📂 Téléverser le fichier Excel des emplois du temps (.xlsx)", type=["xlsx"])
data_bytes, source_name = None, None
if uploaded is not None:
    data_bytes, source_name = uploaded.getvalue(), uploaded.name
else:
    chemins_par_defaut = [
        Path(__file__).with_name("dataEDT-ELT-S1-2027.xlsx"),
        Path(__file__).with_name("dataEDT-ELT-S1-2027(1).xlsx"),
        Path(__file__).with_name("EDT_Edition.xlsx"),
    ]
    default_path = next((p for p in chemins_par_defaut if p.exists()), None)
    if default_path is not None:
        data_bytes, source_name = default_path.read_bytes(), default_path.name
        st.info(f"Fichier chargé automatiquement : **{source_name}**. Téléversez un autre fichier Excel pour le remplacer.")
    else:
        st.warning("Téléversez un fichier Excel. Colonnes minimales : Enseignements, Code, Horaire, Jours et Promotion.")
        st.stop()
try:
    df = _charger_excel(data_bytes)
except Exception as e:
    st.error(f"Impossible de lire ce fichier Excel : {e}")
    st.stop()

# Sécurité de compatibilité : après un redémarrage ou avec un cache Streamlit
# provenant d'une ancienne version, la colonne calculée peut être absente.
# On la recalcule ici avant d'appliquer le filtre Type d'enseignement.
if "_type_enseignement" not in df.columns:
    df = df.copy()
    df["_type_enseignement"] = df.apply(_type_enseignement, axis=1)
else:
    type_vide = df["_type_enseignement"].isna() | (df["_type_enseignement"].astype(str).str.strip() == "")
    if type_vide.any():
        df = df.copy()
        df.loc[type_vide, "_type_enseignement"] = df.loc[type_vide].apply(_type_enseignement, axis=1)

records_all = _records(df)
promotions = sorted([x for x in df["Promotion"].unique() if x], key=str.casefold)
departements = sorted([x for x in df["Département"].unique() if x], key=str.casefold)
enseignants = sorted({nom for valeur in df["Enseignants"] for nom in _separer_enseignants(valeur) if nom}, key=str.casefold)
jours = [d for d in JOURS_ORDRE if d in set(df["Jours"])]
jours += sorted([d for d in df["Jours"].unique() if d and d not in jours], key=str.casefold)
horaires = sorted([h for h in df["Horaire"].unique() if h], key=lambda x: (_minutes_debut(x), x.casefold()))

m1,m2,m3,m4=st.columns(4)
m1.metric("Séances chargées",f"{len(df):,}".replace(","," "))
m2.metric("Promotions",len(promotions))
m3.metric("Départements",len(departements))
m4.metric("Jours détectés",len(jours))
comptage_types = df["_type_enseignement"].value_counts().to_dict()
ordre_types = ["Cours", "TD", "TP", "Atelier"]
types_resume = " · ".join(f"{nom} : {int(comptage_types.get(nom, 0))}" for nom in ordre_types if comptage_types.get(nom, 0))
st.caption(f"Fichier source : {source_name} · Types détectés : {types_resume} · Le classeur original n'est jamais modifié.")

# -------------------- ACTUALITÉS, ANNONCES ET AVIS --------------------
st.divider()
st.subheader("📰 Actualités, annonces et avis")
st.write("Rédigez un message directement ou joignez un document / une image. Les publications actives apparaîtront dans l’aperçu TV et dans le fichier HTML autonome téléchargé.")
publications = _charger_publications()
publications_actives = [pub for pub in publications if _publication_en_cours(pub)]
admin_password = _mot_de_passe_admin_publications()

if not admin_password:
    st.warning("La gestion des publications est verrouillée tant qu’un mot de passe administrateur n’a pas été configuré.")
    with st.expander("🔐 Configurer l’accès administrateur", expanded=True):
        st.markdown("Créez le fichier `.streamlit/secrets.toml` dans le dossier de l’application et ajoutez la ligne suivante avec votre propre mot de passe :")
        st.code('PUBLICATION_ADMIN_PASSWORD = "Remplacez-par-un-mot-de-passe-long-et-personnel"', language="toml")
        st.caption("Sur Streamlit Community Cloud, ajoutez cette valeur dans Settings → Secrets avant de partager le lien de l’application.")
elif not st.session_state.get("publication_admin_authentifie", False):
    with st.form("connexion_administration_publications"):
        mot_de_passe_saisi = st.text_input("Mot de passe administrateur pour publier", type="password")
        tentative_connexion = st.form_submit_button("🔓 Accéder à la gestion des publications", type="primary", use_container_width=True)
    if tentative_connexion:
        if hmac.compare_digest(mot_de_passe_saisi, admin_password):
            st.session_state["publication_admin_authentifie"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
else:
    top_admin1, top_admin2 = st.columns([4, 1])
    with top_admin1:
        st.success("Session administrateur : vous pouvez rédiger, téléverser et gérer les publications.")
    with top_admin2:
        if st.button("Déconnexion", key="deconnexion_admin_publications", use_container_width=True):
            st.session_state["publication_admin_authentifie"] = False
            st.rerun()
    with st.expander("✍️ Rédiger ou téléverser une nouvelle publication", expanded=False):
        limiter_date = st.checkbox("Définir une date de fin d’affichage", value=False, key="limiter_date_publication_edt")
        if limiter_date:
            date_fin_publication = st.date_input("Afficher jusqu’au", value=date.today() + timedelta(days=30), min_value=date.today(), key="date_fin_publication_edt")
        else:
            date_fin_publication = None
        with st.form("form_publication_edt", clear_on_submit=True):
            nc1, nc2 = st.columns(2)
            with nc1:
                nouvelle_categorie = st.selectbox("Rubrique", CATEGORIES_PUBLICATION, key="nouvelle_categorie_edt")
            with nc2:
                nouveau_public = st.selectbox("Public concerné", PUBLICS_PUBLICATION, index=2, key="nouveau_public_edt")
            nouveau_titre = st.text_input("Titre de la publication *", placeholder="Ex. Avis aux étudiants de L2…", max_chars=180)
            nouveau_message = st.text_area("Texte à publier", placeholder="Écrivez ici le message complet, les dates, les lieux et les consignes…", height=140, max_chars=10000)
            fichier_publication = st.file_uploader("Pièce jointe facultative (image, PDF, Word, Excel ou PowerPoint — 4 Mo maximum)", type=EXTENSIONS_PUBLICATION, accept_multiple_files=False, key="fichier_publication_edt")
            publier = st.form_submit_button("📢 Publier l’annonce / l’avis", type="primary", use_container_width=True)
        if publier:
            if not nouveau_titre.strip():
                st.error("Veuillez renseigner le titre de la publication.")
            elif not nouveau_message.strip() and fichier_publication is None:
                st.error("Écrivez un message ou ajoutez une pièce jointe avant de publier.")
            elif fichier_publication is not None and len(fichier_publication.getvalue()) > TAILLE_MAX_PIECE_JOINTE:
                st.error("La pièce jointe dépasse 4 Mo. Veuillez choisir un fichier plus petit.")
            else:
                maintenant = datetime.now()
                publication = {
                    "id": uuid.uuid4().hex,
                    "category": nouvelle_categorie,
                    "audience": nouveau_public,
                    "title": nouveau_titre.strip(),
                    "body": nouveau_message.strip(),
                    "created_at": maintenant.strftime("%d/%m/%Y à %H:%M"),
                    "created_sort": maintenant.isoformat(timespec="minutes"),
                    "expires_on": date_fin_publication.isoformat() if date_fin_publication else "",
                    "attachment_name": "",
                    "attachment_path": "",
                    "attachment_mime": "",
                    "active": True,
                }
                if fichier_publication is not None:
                    DOSSIER_MEDIAS_PUBLICATIONS.mkdir(parents=True, exist_ok=True)
                    nom_original = fichier_publication.name.replace("\\", "/").split("/")[-1]
                    extension = Path(nom_original).suffix.lower()
                    chemin_media = DOSSIER_MEDIAS_PUBLICATIONS / f"{publication['id']}{extension}"
                    chemin_media.write_bytes(fichier_publication.getvalue())
                    publication["attachment_name"] = nom_original
                    publication["attachment_path"] = str(chemin_media.relative_to(APP_DIR))
                    publication["attachment_mime"] = fichier_publication.type or mimetypes.guess_type(nom_original)[0] or "application/octet-stream"
                publications.append(publication)
                _sauvegarder_publications(publications)
                st.session_state["publication_edt_enregistree"] = True
                st.rerun()

    if st.session_state.pop("publication_edt_enregistree", False):
        st.success("Publication enregistrée. Elle sera affichée dans l’aperçu TV et dans le HTML autonome.")

    publications = _charger_publications()
    publications_actives = [pub for pub in publications if _publication_en_cours(pub)]
    st.subheader(f"📋 Publications en cours ({len(publications_actives)}) et gestion des archives")
    filtre_publications = st.radio("Afficher", ["En cours", "Toutes", "Archivées"], horizontal=True, key="filtre_gestion_publications_edt")
    if filtre_publications == "En cours":
        pubs_a_afficher = [pub for pub in publications if _publication_en_cours(pub)]
    elif filtre_publications == "Archivées":
        pubs_a_afficher = [pub for pub in publications if not pub.get("active", True)]
    else:
        pubs_a_afficher = publications[:]
    pubs_a_afficher.sort(key=lambda pub: str(pub.get("created_sort", pub.get("created_at", ""))), reverse=True)
    if not pubs_a_afficher:
        st.info("Aucune publication dans cette catégorie. Utilisez le formulaire ci-dessus pour rédiger une annonce ou téléverser un document.")
    for pub in pubs_a_afficher:
        statut_pub = "En cours" if _publication_en_cours(pub) else ("Archivée" if not pub.get("active", True) else "Expirée")
        titre_expander = f"{'🟢' if statut_pub == 'En cours' else '⚪'} {pub.get('category', 'Avis')} — {pub.get('title', 'Sans titre')} ({statut_pub})"
        with st.expander(titre_expander, expanded=False):
            st.caption(f"Public : {pub.get('audience', 'Étudiants et enseignants')} · Publié le : {pub.get('created_at', 'Date non renseignée')}" + (f" · Fin : {pub.get('expires_on')}" if pub.get('expires_on') else ""))
            if pub.get("body"):
                st.markdown(pub["body"])
            chemin_piece = _chemin_piece_jointe(pub)
            if chemin_piece:
                if str(pub.get("attachment_mime", "")).startswith("image/"):
                    st.image(str(chemin_piece), caption=pub.get("attachment_name", "Pièce jointe"), use_container_width=True)
                st.download_button("📎 Télécharger la pièce jointe : " + pub.get("attachment_name", "fichier"), data=chemin_piece.read_bytes(), file_name=pub.get("attachment_name") or chemin_piece.name, mime=pub.get("attachment_mime") or "application/octet-stream", key=f"telecharger_publication_{pub.get('id')}")
            ac1, ac2 = st.columns(2)
            with ac1:
                if pub.get("active", True):
                    if st.button("📦 Archiver", key=f"archiver_publication_{pub.get('id')}", use_container_width=True):
                        for element in publications:
                            if element.get("id") == pub.get("id"):
                                element["active"] = False
                                break
                        _sauvegarder_publications(publications)
                        st.rerun()
                else:
                    if st.button("♻️ Republier", key=f"republier_publication_{pub.get('id')}", use_container_width=True):
                        for element in publications:
                            if element.get("id") == pub.get("id"):
                                element["active"] = True
                                break
                        _sauvegarder_publications(publications)
                        st.rerun()
            with ac2:
                confirmer_suppression = st.checkbox("Confirmer la suppression définitive", key=f"confirmer_suppression_publication_{pub.get('id')}")
                if confirmer_suppression and st.button("🗑️ Supprimer", key=f"supprimer_publication_{pub.get('id')}", type="secondary", use_container_width=True):
                    publications = [element for element in publications if element.get("id") != pub.get("id")]
                    if chemin_piece and chemin_piece.exists():
                        try:
                            chemin_piece.unlink()
                        except OSError:
                            pass
                    _sauvegarder_publications(publications)
                    st.rerun()

st.divider()
st.subheader("🎛️ Choisir l’EDT à afficher")
mode_auto_jour = st.checkbox("📅 Automatique : jour du PC · ELT · créneau actuel + tous les suivants · tri par promotion", value=True, key="mode_auto_jour_elt")
jours_options = [TOUS_JOURS]+jours
nom_jour_pc = {0:"Lundi", 1:"Mardi", 2:"Mercredi", 3:"Jeudi", 4:"Vendredi", 5:"Samedi", 6:"Dimanche"}[date.today().weekday()]
jour_index_defaut = next((i for i, j in enumerate(jours_options) if _sans_accents(j)==_sans_accents(nom_jour_pc)), 0)
idx_dept_elt = next((i+1 for i, d in enumerate(departements) if _sans_accents(d)=="elt"), 0)
c1,c2,c3,c4=st.columns([1,1.45,1.35,1])
with c1: jour_sel=st.selectbox("Jour",jours_options,index=jour_index_defaut,disabled=mode_auto_jour)
with c2: creneaux_sel=st.multiselect("Créneau(x) horaire(s)",[TOUS_CRENEAUX]+CHOIX_PERIODES_HORAIRES+horaires,default=[TOUS_CRENEAUX],disabled=mode_auto_jour,help="Décochez le mode automatique pour sélectionner les créneaux manuellement. Matin : 08h00–12h30 ; après-midi : 12h30–17h00.")

# La liste Promotion dépend du département sélectionné. Les clés dédiées permettent
# de remettre automatiquement la promotion à « Toutes les promotions » si le
# département change, au lieu de conserver une promotion qui n'existe pas dans ce département.
_dept_key = "edt_tv_departement_filtre"
_promo_key = "edt_tv_promotion_filtre"
_dept_options = [TOUS_DEPARTEMENTS] + departements
_dept_defaut = _dept_options[idx_dept_elt] if 0 <= idx_dept_elt < len(_dept_options) else TOUS_DEPARTEMENTS
if st.session_state.get(_dept_key) not in _dept_options:
    st.session_state[_dept_key] = _dept_defaut

def _reset_promotion_filtre():
    st.session_state[_promo_key] = TOUTES_PROMOTIONS

def _promotions_du_departement(departement_selectionne):
    if departement_selectionne == TOUS_DEPARTEMENTS:
        source_promotions = df["Promotion"]
    else:
        masque_departement = df["Département"].map(_sans_accents) == _sans_accents(departement_selectionne)
        source_promotions = df.loc[masque_departement, "Promotion"]
    return sorted([str(x).strip() for x in source_promotions.dropna().unique() if str(x).strip()], key=str.casefold)

_dept_courant = st.session_state.get(_dept_key, _dept_defaut)
_promotions_disponibles = [TOUTES_PROMOTIONS] + _promotions_du_departement(_dept_courant)
if st.session_state.get(_promo_key) not in _promotions_disponibles:
    st.session_state[_promo_key] = TOUTES_PROMOTIONS
with c3:
    promo_sel = st.selectbox("Promotion", _promotions_disponibles, key=_promo_key, disabled=mode_auto_jour)
with c4:
    dept_sel = st.selectbox("Département", _dept_options, key=_dept_key, on_change=_reset_promotion_filtre, disabled=mode_auto_jour, help="La liste Promotion se limite aux promotions du département sélectionné. Le mode automatique affiche toujours le département ELT. Décochez-le pour utiliser les filtres manuels.")
c5,c6,c7,c8=st.columns([1.45,1.15,1,1.35])
with c5: type_sel=st.selectbox("Type d’enseignement",CHOIX_TYPES_ENSEIGNEMENT,index=0,disabled=mode_auto_jour,help="Décochez le mode automatique pour choisir manuellement un type d’enseignement.")
if type_sel == "Sélection personnalisée":
    types_personnalises=st.multiselect("Types à afficher",TYPES_ENSEIGNEMENT,default=TYPES_ENSEIGNEMENT,key="types_enseignement_personnalises",disabled=mode_auto_jour,help="Choisissez un ou plusieurs types de séances.")
else:
    types_personnalises=[]
with c6: affichage=st.selectbox("Mode d’affichage",["Liste défilante","Grille","Par enseignant"],index=0,disabled=mode_auto_jour)
with c7: vitesse=st.slider("Vitesse du défilement",5,80,24,5,help="Pixels par seconde environ.")
with c8: lancer=st.button("📺 Afficher / actualiser la TV",type="primary",use_container_width=True)
if affichage == "Par enseignant":
    enseignants_sel = st.multiselect("👤 Choisir un ou plusieurs enseignants", enseignants, default=[], help="Sélectionnez un ou plusieurs noms. Les séances communes à plusieurs enseignants apparaîtront dans le planning de chaque enseignant sélectionné.")
else:
    enseignants_sel = []
filtered=df.copy()
if jour_sel != TOUS_JOURS: filtered=filtered[filtered["Jours"]==jour_sel]
if creneaux_sel and TOUS_CRENEAUX not in creneaux_sel:
    horaires_selectionnes = {h for h in creneaux_sel if h in horaires}
    for periode in CHOIX_PERIODES_HORAIRES:
        if periode in creneaux_sel:
            horaires_selectionnes.update(h for h in horaires if _horaire_dans_periode(h, periode))
    filtered=filtered[filtered["Horaire"].isin(horaires_selectionnes)]
if promo_sel != TOUTES_PROMOTIONS: filtered=filtered[filtered["Promotion"]==promo_sel]
if dept_sel != TOUS_DEPARTEMENTS: filtered=filtered[filtered["Département"]==dept_sel]
types_a_afficher=_types_a_afficher(type_sel,types_personnalises)
filtered=filtered[filtered["_type_enseignement"].isin(types_a_afficher)]
if affichage == "Par enseignant" and enseignants_sel:
    enseignants_sel_norm = {_cle_enseignant(nom) for nom in enseignants_sel}
    filtered = filtered[filtered["Enseignants"].map(lambda valeur: any(_cle_enseignant(nom) in enseignants_sel_norm for nom in _separer_enseignants(valeur)))]
filtered=filtered.sort_values(["_jour_tri","_heure_tri","_promo_tri","_ens_tri"],kind="stable")
filtered_records=_records(filtered)
a,b,c=st.columns(3)
a.metric("Séances correspondant au filtre",f"{len(filtered_records):,}".replace(","," "))
b.metric("Promotions dans la sélection",filtered["Promotion"].nunique())
c.metric("Créneaux dans la sélection",filtered["Horaire"].nunique())
type_txt=" + ".join(types_a_afficher) if type_sel == "Sélection personnalisée" or "+" in type_sel else type_sel
enseignants_txt = (" · Enseignant(s) : " + ", ".join(enseignants_sel)) if affichage == "Par enseignant" and enseignants_sel else ""
filtres_txt=f"{jour_sel} · "+(", ".join(creneaux_sel) if creneaux_sel and TOUS_CRENEAUX not in creneaux_sel else TOUS_CRENEAUX)+f" · {promo_sel} · {dept_sel} · {type_txt}"+enseignants_txt
st.markdown(f"**Sélection actuelle :** {filtres_txt}")
if "edt_tv_preview" not in st.session_state:
    st.session_state["edt_tv_preview"] = True
if lancer: st.session_state["edt_tv_preview"]=True
if st.session_state.get("edt_tv_preview",False):
    if mode_auto_jour:
        df_elt_auto = df[df["Département"].map(_sans_accents)=="elt"].copy()
        if df_elt_auto.empty:
            st.warning("Aucune séance trouvée pour le département ELT dans le fichier source.")
        else:
            st.subheader("📺 Aperçu TV — planning automatique du jour (ELT)")
            st.info("Le jour et l’heure sont détectés selon l’horloge du navigateur/PC. L’écran affiche tous les créneaux qui sont en cours ou qui restent à venir, dans l’ordre horaire, avec les séances de chaque créneau classées par promotion. À la fin réelle d’un créneau, ses séances disparaissent automatiquement ; les créneaux suivants restent affichés.")
            tv_html=_page_html(_records(df_elt_auto),"Emploi du temps — Département ELT · UDL-SBA","Planning automatique du jour · ELT · créneau actuel et tous les suivants · ordre par promotion",filters=False,speed=vitesse,autoplay=True,layout="Liste défilante",selected_teachers=[],publications=publications_actives,auto_daily=True,default_department="ELT")
            components.html(tv_html,height=760,scrolling=False)
            st.caption("Le changement de jour est automatique d’après l’horloge du PC. Les filtres manuels sont conservés : décochez le mode automatique pour afficher un autre jour ou un autre département.")
    elif affichage == "Par enseignant" and not enseignants_sel:
        st.info("Sélectionnez au moins un enseignant pour afficher son emploi du temps.")
    elif not filtered_records: st.warning("Aucune séance ne correspond aux filtres actuels.")
    else:
        st.subheader("📺 Aperçu de l’affichage TV")
        tv_html=_page_html(filtered_records,"Emploi du temps — Faculté de génie électrique - UDL-SBA",filtres_txt,filters=False,speed=vitesse,autoplay=True,layout=affichage,selected_teachers=enseignants_sel,publications=publications_actives,auto_daily=False)
        components.html(tv_html,height=760,scrolling=False)
        st.caption("Ouvrez cette application dans le navigateur de la TV puis passez en plein écran (F11). L’aperçu défile automatiquement.")
st.divider()
st.subheader("💾 Télécharger l’affichage pour TV")
st.write("Le fichier HTML autonome contient toutes les lignes du fichier Excel chargé et les publications actives au moment du téléchargement. Les annonces défilent dans un bandeau d'information. Les pièces jointes de petite taille sont intégrées pour une consultation hors ligne.")
usb_html=_page_html(records_all,"Emploi du temps — Faculté de génie électrique - UDL-SBA","Planning automatique ELT : créneau actuel et tous les créneaux à venir, classés par ordre horaire puis par promotion. Les créneaux terminés disparaissent automatiquement. Décochez le mode automatique pour utiliser les filtres manuels.",filters=True,speed=vitesse,autoplay=True,layout="Liste défilante",publications=publications_actives,auto_daily=True,default_department="ELT")
st.download_button("📥 Télécharger le fichier HTML autonome pour clé USB",data=usb_html.encode("utf-8"),file_name="EDT_TV_USB.html",mime="text/html",type="primary")
st.download_button("📥 Télécharger les données Excel chargées",data=data_bytes,file_name=Path(source_name or "EDT.xlsx").name,mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
with st.expander("ℹ️ Mise en ligne pour obtenir un lien web permanent"):
    st.markdown("""
1. Décompressez le pack de l’application.
2. Ajoutez `app_edt_tv.py`, `requirements.txt` et, si souhaité, le fichier Excel d’exemple à un dépôt GitHub.
3. Dans Streamlit Community Cloud, déployez ce dépôt et choisissez `app_edt_tv.py` comme fichier principal.
4. Ouvrez le lien fourni par Streamlit sur la TV, choisissez les filtres et cliquez sur **Afficher / actualiser la TV**, puis utilisez le plein écran.

**Mode USB :** téléchargez `EDT_TV_USB.html`, copiez-le sur une clé USB et ouvrez-le avec le navigateur de la TV. Certains téléviseurs n’ouvrent pas les fichiers HTML locaux ; dans ce cas, utilisez un ordinateur ou mini-PC connecté en HDMI.

**Publications :** sur un ordinateur/serveur local, elles sont enregistrées dans `publications_edt.json` et les pièces jointes dans `publications_medias/`, à côté du script. Pour une publication durable sur un hébergement cloud après redéploiement, utiliser un stockage persistant (par exemple Supabase ou un disque persistant).
""")
