# -*- coding: utf-8 -*-
"""
Application d'affichage des emplois du temps sur TV, navigateur et clé USB.
Lancement local : streamlit run app_edt_tv.py
"""
from __future__ import annotations
import io, json, re, unicodedata, html as htmlmod
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

def _jour_index(value: Any) -> int:
    v = _sans_accents(value)
    for i, day in enumerate(JOURS_ORDRE):
        if _sans_accents(day) == v:
            return i
    return 100

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
    df["_jour_tri"] = df["Jours"].map(_jour_index)
    df["_heure_tri"] = df["Horaire"].map(_minutes_debut)
    df["_promo_tri"] = df["Promotion"].str.casefold()
    df["_ens_tri"] = df["Enseignements"].str.casefold()
    return df.sort_values(["_jour_tri","_heure_tri","_promo_tri","_ens_tri"],kind="stable").reset_index(drop=True)

@st.cache_data(show_spinner=False)
def _charger_excel(data: bytes) -> pd.DataFrame:
    return _nettoyer_dataframe(pd.read_excel(io.BytesIO(data), dtype=str))

def _records(df: pd.DataFrame) -> list[dict]:
    return [{k:(str(v).strip() if v is not None else "") for k,v in row.items()} for row in df[COLONNES].fillna("").to_dict(orient="records")]

def _json_safe(records: list[dict]) -> str:
    return json.dumps(records, ensure_ascii=False, separators=(",",":")).replace("</","<\\/")

def _page_html(records: list[dict], title: str, subtitle: str, *, filters: bool, speed: int = 24, autoplay: bool = True, layout: str = "Liste défilante") -> str:
    data_js = _json_safe(records)
    filter_markup = ""
    if filters:
        filter_markup = """
        <div class="filters">
          <label>Jour<select id="f-day"></select></label>
          <label>Créneau horaire<select id="f-time"></select></label>
          <label>Promotion<select id="f-promo"></select></label>
          <label>Département<select id="f-dept"></select></label>
          <button id="apply">Afficher l'EDT</button>
        </div>"""
    initial_layout = "grid" if layout.lower().startswith("grille") else "list"
    controls = """
      <button id="pauseBtn" type="button">⏸ Pause</button>
      <button id="fullscreenBtn" type="button">⛶ Plein écran</button>
      <label class="speed-label">Vitesse <input id="speed" type="range" min="5" max="80" step="5" value="24"></label>
      <label class="view-label">Vue <select id="view"><option value="list">Liste défilante</option><option value="grid">Grille</option></select></label>"""
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="application-name" content="EDT TV UDL-SBA"><title>{htmlmod.escape(title)}</title>
<style>
*{{box-sizing:border-box}} html,body{{margin:0;width:100%;height:100%;font-family:Arial,Helvetica,sans-serif;background:#07111f;color:#f8fafc}}
body{{display:flex;flex-direction:column;overflow:hidden}}
.top{{padding:16px 24px;background:linear-gradient(115deg,#0f172a,#1e40af 70%,#0891b2);display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:3px solid #38bdf8}}
.brand h1{{margin:0;font-size:clamp(22px,2.1vw,38px);letter-spacing:.2px}} .brand p{{margin:5px 0 0;color:#dbeafe;font-size:clamp(13px,1vw,18px)}}
.clock{{font-size:clamp(18px,1.8vw,30px);font-weight:800;white-space:nowrap;text-align:right}} .clock small{{display:block;font-size:.55em;color:#dbeafe;margin-top:3px}}
.filters{{display:flex;flex-wrap:wrap;gap:12px;align-items:end;padding:12px 22px;background:#0b1728;border-bottom:1px solid #29405b}}
label{{display:flex;flex-direction:column;gap:5px;color:#bfdbfe;font-size:13px;font-weight:700}} select,button,input{{font:inherit}} select{{min-width:180px;max-width:300px;padding:10px;background:#fff;color:#0f172a;border-radius:8px;border:1px solid #93c5fd}}
button{{background:#0ea5e9;color:#001a2a;border:none;border-radius:8px;padding:10px 14px;font-weight:800;cursor:pointer}} button:hover{{background:#7dd3fc}}
.tools{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:8px 22px;background:#081322;border-bottom:1px solid #263b54}}
.tools button{{padding:7px 11px;font-size:13px}} .speed-label,.view-label{{flex-direction:row;align-items:center}} input[type=range]{{width:120px}}
.status{{margin-left:auto;color:#cbd5e1;font-weight:700;font-size:14px}}
#board{{position:relative;flex:1;overflow-y:auto;overscroll-behavior:contain;padding:20px 22px 34px;scrollbar-color:#38bdf8 #0f172a;scrollbar-width:thin}}
.day-heading{{font-size:clamp(18px,1.5vw,28px);font-weight:900;color:#7dd3fc;border-bottom:1px solid #33506f;padding:0 0 8px;margin:12px 0 14px}}
.hour-heading{{color:#fde68a;font-weight:900;font-size:clamp(15px,1.15vw,22px);margin:18px 0 8px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(450px,100%),1fr));gap:12px}}
.card{{border:1px solid #344d6c;border-left:7px solid #38bdf8;border-radius:12px;background:linear-gradient(120deg,#102038,#142944);padding:15px 17px;box-shadow:0 5px 15px #0003;break-inside:avoid}}
.card.tp{{border-left-color:#f87171}} .card.td{{border-left-color:#4ade80}} .card.cours{{border-left-color:#60a5fa}}
.subject{{font-size:clamp(18px,1.55vw,28px);line-height:1.22;font-weight:900;color:#fff;margin-bottom:10px}}
.badges{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:9px}} .badge{{border-radius:6px;padding:4px 8px;font-size:12px;font-weight:900;background:#1d4ed8;color:white}} .badge.tp{{background:#991b1b}} .badge.td{{background:#166534}} .badge.cours{{background:#1e40af}}
.details{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:7px;color:#dbeafe;font-size:clamp(13px,1.05vw,18px)}} .details b{{color:#7dd3fc}}
.grid-table{{width:100%;border-collapse:separate;border-spacing:5px;table-layout:fixed}} .grid-table th{{background:#1e40af;color:white;padding:12px 6px;border-radius:7px;font-size:clamp(14px,1.15vw,20px)}} .grid-table td{{vertical-align:top;background:#0e1d30;border:1px solid #2f4968;border-radius:8px;padding:7px;overflow-wrap:anywhere}}
.grid-item{{background:#142944;border-left:4px solid #38bdf8;padding:8px;margin:5px 0;border-radius:5px;font-size:clamp(12px,.95vw,17px)}} .grid-item strong{{display:block;color:white;margin-bottom:4px}} .grid-item small{{color:#bfdbfe}}
.empty{{text-align:center;color:#cbd5e1;padding:60px 20px;font-size:clamp(20px,2vw,32px)}} .foot{{padding:7px 20px;background:#06101d;color:#8fa8c4;font-size:12px;display:flex;justify-content:space-between;gap:12px}}
@media(max-width:800px){{.top{{align-items:flex-start;flex-direction:column}} .clock{{text-align:left}} .filters label{{width:100%}} .filters select{{max-width:none;width:100%}} .status{{margin-left:0}} .grid-table{{table-layout:auto}}}}
</style></head>
<body>
<header class="top"><div class="brand"><h1>{htmlmod.escape(title)}</h1><p>{htmlmod.escape(subtitle)}</p></div><div class="clock"><span id="clock">--:--:--</span><small id="date"></small></div></header>
{filter_markup}
<div class="tools">{controls}<span class="status" id="status">Chargement des emplois du temps…</span></div>
<main id="board" aria-live="polite"></main><footer class="foot"><span>Université Djillali Liabès · Faculté de Génie Électrique</span><span>Affichage TV — actualisé dans cette page</span></footer>
<script>
const DATA={data_js};
const DAYS={json.dumps(JOURS_ORDRE, ensure_ascii=False)};
const INITIAL_SPEED={int(speed)};
let autoplay={str(bool(autoplay)).lower()}, stopped=false, lastFrame=0, speed=INITIAL_SPEED;
let activeLayout={json.dumps(initial_layout)};
const board=document.getElementById('board'), statusEl=document.getElementById('status');
function esc(s){{return String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
function dayIndex(s){{let n=String(s||'').toLowerCase();return DAYS.findIndex(d=>d.toLowerCase()===n);}}
function timeStart(s){{let m=String(s||'').match(/(\\d{{1,2}})\\s*h\\s*(\\d{{0,2}})/i);return m?Number(m[1])*60+Number(m[2]||0):99999;}}
function natural(a,b){{return String(a).localeCompare(String(b),'fr',{{numeric:true,sensitivity:'base'}});}}
function uniqueSorted(key, order){{let vals=[...new Set(DATA.map(r=>String(r[key]||'').trim()).filter(Boolean))];if(order)vals.sort((a,b)=>timeStart(a)-timeStart(b)||natural(a,b));else if(key==='Jours')vals.sort((a,b)=>(dayIndex(a)+100)-(dayIndex(b)+100)||natural(a,b));else vals.sort(natural);return vals;}}
function fillSelect(id, first, vals){{let el=document.getElementById(id);if(!el)return;el.innerHTML='';let opt=document.createElement('option');opt.value='';opt.textContent=first;el.appendChild(opt);vals.forEach(v=>{{let o=document.createElement('option');o.value=v;o.textContent=v;el.appendChild(o);}});}}
function kind(r){{let s=(r.Code||r.Enseignements||'').toLowerCase();if(s.includes('tp'))return 'tp';if(s.includes('td'))return 'td';return 'cours';}}
function card(r){{let k=kind(r);return `<article class="card ${{k}}"><div class="subject">${{esc(r.Enseignements||'Enseignement non renseigné')}}</div><div class="badges"><span class="badge ${{k}}">${{esc(r.Code||'Type non renseigné')}}</span><span class="badge">${{esc(r.Horaire||'Horaire non renseigné')}}</span><span class="badge">${{esc(r.Jours||'Jour non renseigné')}}</span></div><div class="details"><span><b>Enseignant(s) :</b> ${{esc(r.Enseignants||'Non renseigné')}}</span><span><b>Lieu :</b> ${{esc(r.Lieu||'Non renseigné')}}</span><span><b>Promotion :</b> ${{esc(r.Promotion||'Non renseignée')}}</span><span><b>Département :</b> ${{esc(r.Département||'Non renseigné')}}</span></div></article>`;}}
function renderList(rows){{if(!rows.length){{board.innerHTML='<div class="empty">Aucun enseignement ne correspond aux filtres sélectionnés.</div>';return;}}let html='',lastDay='',lastHour='';rows.forEach((r,i)=>{{if(r.Jours!==lastDay){{lastDay=r.Jours;lastHour='';html+=`<h2 class="day-heading">${{esc(lastDay)}}</h2>`;}}if(r.Horaire!==lastHour){{lastHour=r.Horaire;html+=`<h3 class="hour-heading">⏰ ${{esc(lastHour)}}</h3><div class="cards">`;}}html+=card(r);let next=rows[i+1];if(!next||next.Jours!==lastDay||next.Horaire!==lastHour)html+='</div>';}});board.innerHTML=html;}}
function renderGrid(rows){{if(!rows.length){{board.innerHTML='<div class="empty">Aucun enseignement ne correspond aux filtres sélectionnés.</div>';return;}}let days=[...new Set(rows.map(r=>r.Jours))].sort((a,b)=>(dayIndex(a)+100)-(dayIndex(b)+100));let hours=[...new Set(rows.map(r=>r.Horaire))].sort((a,b)=>timeStart(a)-timeStart(b)||natural(a,b));let html='<table class="grid-table"><thead><tr><th>Créneau</th>'+days.map(d=>`<th>${{esc(d)}}</th>`).join('')+'</tr></thead><tbody>';hours.forEach(h=>{{html+=`<tr><th>${{esc(h)}}</th>`;days.forEach(d=>{{let cell=rows.filter(r=>r.Horaire===h&&r.Jours===d);html+='<td>'+cell.map(r=>`<div class="grid-item"><strong>${{esc(r.Enseignements)}}</strong><small>${{esc(r.Code)}} · ${{esc(r.Promotion)}}<br>${{esc(r.Enseignants)}}<br>${{esc(r.Lieu)}} · ${{esc(r.Département)}}</small></div>`).join('')+'</td>';}});html+='</tr>';}});html+='</tbody></table>';board.innerHTML=html;}}
function getFilteredRows(){{let day=document.getElementById('f-day')?.value||'',tm=document.getElementById('f-time')?.value||'',pr=document.getElementById('f-promo')?.value||'',de=document.getElementById('f-dept')?.value||'';return DATA.filter(r=>(!day||r.Jours===day)&&(!tm||r.Horaire===tm)&&(!pr||r.Promotion===pr)&&(!de||r.Département===de)).sort((a,b)=>(dayIndex(a.Jours)+100)-(dayIndex(b.Jours)+100)||timeStart(a.Horaire)-timeStart(b.Horaire)||natural(a.Promotion,b.Promotion)||natural(a.Enseignements,b.Enseignements));}}
function updateRender(){{let rows=getFilteredRows();if(activeLayout==='grid')renderGrid(rows);else renderList(rows);statusEl.textContent=rows.length.toLocaleString('fr-FR')+' séance(s) affichée(s)';board.scrollTop=0;}}
if(document.getElementById('f-day')){{fillSelect('f-day','Tous les jours',uniqueSorted('Jours'));fillSelect('f-time','Tous les créneaux',uniqueSorted('Horaire',true));fillSelect('f-promo','Toutes les promotions',uniqueSorted('Promotion'));fillSelect('f-dept','Tous les départements',uniqueSorted('Département'));document.getElementById('apply').addEventListener('click',updateRender);['f-day','f-time','f-promo','f-dept'].forEach(id=>document.getElementById(id).addEventListener('change',updateRender));}}
let speedEl=document.getElementById('speed');if(speedEl){{speedEl.value=INITIAL_SPEED;speedEl.addEventListener('input',()=>{{speed=Number(speedEl.value);}});}}
let viewEl=document.getElementById('view');if(viewEl){{viewEl.value=activeLayout;viewEl.addEventListener('change',()=>{{activeLayout=viewEl.value;updateRender();}});}}
let pauseBtn=document.getElementById('pauseBtn');if(pauseBtn)pauseBtn.addEventListener('click',()=>{{stopped=!stopped;pauseBtn.textContent=stopped?'▶ Reprendre':'⏸ Pause';}});
let fsBtn=document.getElementById('fullscreenBtn');if(fsBtn)fsBtn.addEventListener('click',()=>{{if(!document.fullscreenElement)document.documentElement.requestFullscreen?.();else document.exitFullscreen?.();}});
function tickClock(){{const now=new Date();document.getElementById('clock').textContent=now.toLocaleTimeString('fr-FR');document.getElementById('date').textContent=now.toLocaleDateString('fr-FR',{{weekday:'long',year:'numeric',month:'long',day:'numeric'}});}}
setInterval(tickClock,1000);tickClock();
function scrollFrame(ts){{if(!lastFrame)lastFrame=ts;let dt=Math.min(50,ts-lastFrame);lastFrame=ts;if(autoplay&&!stopped&&board.scrollHeight>board.clientHeight+10){{board.scrollTop+=speed*dt/1000;if(board.scrollTop+board.clientHeight>=board.scrollHeight-2)board.scrollTop=0;}}requestAnimationFrame(scrollFrame);}}
updateRender();requestAnimationFrame(scrollFrame);
</script></body></html>"""

st.markdown(f"""<div class="tv-intro"><h1>📺 {APP_TITLE}</h1><p>Affichage plein écran, défilement automatique et version HTML autonome pour clé USB.</p></div>""", unsafe_allow_html=True)
uploaded = st.file_uploader("📂 Téléverser le fichier Excel des emplois du temps (.xlsx)", type=["xlsx"])
data_bytes, source_name = None, None
if uploaded is not None:
    data_bytes, source_name = uploaded.getvalue(), uploaded.name
else:
    default_path = Path(__file__).with_name("EDT_Edition (96).xlsx")
    if default_path.exists():
        data_bytes, source_name = default_path.read_bytes(), default_path.name
        st.info(f"Fichier d'exemple chargé automatiquement : **{source_name}**. Téléversez un autre fichier Excel pour le remplacer.")
    else:
        st.warning("Téléversez un fichier Excel. Colonnes minimales : Enseignements, Horaire, Jours et Promotion.")
        st.stop()
try:
    df = _charger_excel(data_bytes)
except Exception as e:
    st.error(f"Impossible de lire ce fichier Excel : {e}")
    st.stop()
records_all = _records(df)
promotions = sorted([x for x in df["Promotion"].unique() if x], key=str.casefold)
departements = sorted([x for x in df["Département"].unique() if x], key=str.casefold)
jours = [d for d in JOURS_ORDRE if d in set(df["Jours"])]
jours += sorted([d for d in df["Jours"].unique() if d and d not in jours], key=str.casefold)
horaires = sorted([h for h in df["Horaire"].unique() if h], key=lambda x: (_minutes_debut(x), x.casefold()))

m1,m2,m3,m4=st.columns(4)
m1.metric("Séances chargées",f"{len(df):,}".replace(","," "))
m2.metric("Promotions",len(promotions))
m3.metric("Départements",len(departements))
m4.metric("Jours détectés",len(jours))
st.caption(f"Fichier source : {source_name} · Le classeur original n'est jamais modifié.")
st.subheader("🎛️ Choisir l’EDT à afficher")
c1,c2,c3,c4=st.columns([1,1.45,1.35,1])
with c1: jour_sel=st.selectbox("Jour",[TOUS_JOURS]+jours,index=0)
with c2: creneaux_sel=st.multiselect("Créneau(x) horaire(s)",[TOUS_CRENEAUX]+horaires,default=[TOUS_CRENEAUX],help="Choisissez un ou plusieurs créneaux, ou laissez « Tous les créneaux ».")
with c3: promo_sel=st.selectbox("Promotion",[TOUTES_PROMOTIONS]+promotions,index=0)
with c4: dept_sel=st.selectbox("Département",[TOUS_DEPARTEMENTS]+departements,index=0)
c5,c6,c7=st.columns([1.2,1,1])
with c5: affichage=st.radio("Présentation sur TV",["Liste défilante","Grille"],horizontal=True)
with c6: vitesse=st.slider("Vitesse du défilement",5,80,24,5,help="Pixels par seconde environ.")
with c7: lancer=st.button("📺 Afficher / actualiser la TV",type="primary",use_container_width=True)
filtered=df.copy()
if jour_sel != TOUS_JOURS: filtered=filtered[filtered["Jours"]==jour_sel]
if creneaux_sel and TOUS_CRENEAUX not in creneaux_sel: filtered=filtered[filtered["Horaire"].isin(creneaux_sel)]
if promo_sel != TOUTES_PROMOTIONS: filtered=filtered[filtered["Promotion"]==promo_sel]
if dept_sel != TOUS_DEPARTEMENTS: filtered=filtered[filtered["Département"]==dept_sel]
filtered=filtered.sort_values(["_jour_tri","_heure_tri","_promo_tri","_ens_tri"],kind="stable")
filtered_records=_records(filtered)
a,b,c=st.columns(3)
a.metric("Séances correspondant au filtre",f"{len(filtered_records):,}".replace(","," "))
b.metric("Promotions dans la sélection",filtered["Promotion"].nunique())
c.metric("Créneaux dans la sélection",filtered["Horaire"].nunique())
filtres_txt=f"{jour_sel} · "+(", ".join(creneaux_sel) if creneaux_sel and TOUS_CRENEAUX not in creneaux_sel else TOUS_CRENEAUX)+f" · {promo_sel} · {dept_sel}"
st.markdown(f"**Sélection actuelle :** {filtres_txt}")
if lancer: st.session_state["edt_tv_preview"]=True
if st.session_state.get("edt_tv_preview",False):
    if not filtered_records: st.warning("Aucune séance ne correspond aux filtres actuels.")
    else:
        st.subheader("📺 Aperçu de l’affichage TV")
        tv_html=_page_html(filtered_records,"Emploi du temps — UDL-SBA",filtres_txt,filters=False,speed=vitesse,autoplay=True,layout=affichage)
        components.html(tv_html,height=760,scrolling=False)
        st.caption("Ouvrez cette application dans le navigateur de la TV puis passez en plein écran (F11). L’aperçu défile automatiquement.")
st.divider()
st.subheader("💾 Télécharger l’affichage pour TV")
st.write("Le fichier HTML autonome contient toutes les lignes du fichier Excel chargé. Copiez-le sur une clé USB et ouvrez-le dans le navigateur de la TV ou d’un ordinateur relié à la TV. Il fonctionne hors ligne.")
usb_html=_page_html(records_all,"Emploi du temps — UDL-SBA","Sélectionnez le jour, l’horaire, la promotion et le département sur cet écran TV.",filters=True,speed=vitesse,autoplay=True,layout="Liste défilante")
st.download_button("📥 Télécharger le fichier HTML autonome pour clé USB",data=usb_html.encode("utf-8"),file_name="EDT_TV_USB.html",mime="text/html",type="primary")
st.download_button("📥 Télécharger les données Excel chargées",data=data_bytes,file_name=Path(source_name or "EDT.xlsx").name,mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
with st.expander("ℹ️ Mise en ligne pour obtenir un lien web permanent"):
    st.markdown("""
1. Décompressez le pack de l’application.
2. Ajoutez `app_edt_tv.py`, `requirements.txt` et, si souhaité, le fichier Excel d’exemple à un dépôt GitHub.
3. Dans Streamlit Community Cloud, déployez ce dépôt et choisissez `app_edt_tv.py` comme fichier principal.
4. Ouvrez le lien fourni par Streamlit sur la TV, choisissez les filtres et cliquez sur **Afficher / actualiser la TV**, puis utilisez le plein écran.

**Mode USB :** téléchargez `EDT_TV_USB.html`, copiez-le sur une clé USB et ouvrez-le avec le navigateur de la TV. Certains téléviseurs n’ouvrent pas les fichiers HTML locaux ; dans ce cas, utilisez un ordinateur ou mini-PC connecté en HDMI.
""")
