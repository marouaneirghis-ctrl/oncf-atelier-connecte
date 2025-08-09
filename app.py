# app.py
import streamlit as st
import sqlite3, os, io, datetime
import pandas as pd
import plotly.express as px
from PIL import Image

# ----------------------------
# Helpers: DB init and helpers
# ----------------------------
DB_PATH = "oncf.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    # users (demo)
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY, password TEXT, role TEXT, fullname TEXT
    )""")
    # trains
    c.execute("""CREATE TABLE IF NOT EXISTS trains (
        id_train TEXT PRIMARY KEY, modele TEXT, date_mise_en_service TEXT,
        km_total INTEGER, etat_sante INTEGER, derniere_visite TEXT
    )""")
    # components (criticité_max)
    c.execute("""CREATE TABLE IF NOT EXISTS components (
        name TEXT PRIMARY KEY, criticite_max INTEGER
    )""")
    # parts
    c.execute("""CREATE TABLE IF NOT EXISTS parts (
        ref TEXT PRIMARY KEY, designation TEXT, qty INTEGER, seuil_min INTEGER, utilises TEXT
    )""")
    # anomalies
    c.execute("""CREATE TABLE IF NOT EXISTS anomalies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_train TEXT, technicien TEXT, date_signalement TEXT,
        categorie TEXT, composant TEXT, description TEXT, photo TEXT,
        immobilisation INTEGER, gravite TEXT, criticite_calc INTEGER, urgence TEXT, statut TEXT
    )""")
    # conformities
    c.execute("""CREATE TABLE IF NOT EXISTS conformities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_train TEXT, date_intervention TEXT, technicien TEXT,
        type_intervention TEXT, composant TEXT, piece_ref TEXT, resultat TEXT, observations TEXT
    )""")
    conn.commit()

    # seed demo users/trains/components/parts if not exist
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        users = [
            ("tech", "password", "technicien", "Technicien Demo"),
            ("responsable", "password", "responsable", "Responsable Demo")
        ]
        c.executemany("INSERT INTO users (username,password,role,fullname) VALUES (?,?,?,?)", users)
    # seed trains
    c.execute("SELECT COUNT(*) FROM trains")
    if c.fetchone()[0] == 0:
        trains = [
            ("Z2M-01","Z2M", "2010-05-20", 450000, 100, ""),
            ("Z2M-05","Z2M", "2011-07-12", 512000, 100, ""),
            ("Z2M-08","Z2M", "2009-03-03", 600000, 100, "")
        ]
        c.executemany("INSERT INTO trains (id_train,modele,date_mise_en_service,km_total,etat_sante,derniere_visite) VALUES (?,?,?,?,?,?)", trains)
    # seed components (AMDEC criticite)
    c.execute("SELECT COUNT(*) FROM components")
    if c.fetchone()[0] == 0:
        comps = [
            ("frein", 95),
            ("porte", 80),
            ("moteur", 90),
            ("climatisation", 40),
            ("compresseur", 85),
            ("batterie", 70),
            ("pantographe", 88)
        ]
        c.executemany("INSERT INTO components (name,criticite_max) VALUES (?,?)", comps)
    # seed parts
    c.execute("SELECT COUNT(*) FROM parts")
    if c.fetchone()[0] == 0:
        parts = [
            ("VP001","Valve de pression", 4, 2, "frein,hydraulique"),
            ("VR003","Vérin porte", 2, 1, "porte"),
            ("PLT10","Plaquette de frein", 20, 5, "frein")
        ]
        c.executemany("INSERT INTO parts (ref,designation,qty,seuil_min,utilises) VALUES (?,?,?,?,?)", parts)
    conn.commit()
    conn.close()

init_db()

# ----------------------------
# Business logic: criticity & health
# ----------------------------
def parse_dt(s):
    return datetime.datetime.fromisoformat(s)

def now_iso():
    return datetime.datetime.utcnow().isoformat()

def days_ago_iso(days):
    return (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()

def get_component_criticite(name):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT criticite_max FROM components WHERE name = ?", (name,))
    r = c.fetchone()
    conn.close()
    return int(r[0]) if r else 50

def count_similar_recent(train_id, composant, days=90):
    conn = get_conn()
    c = conn.cursor()
    since = days_ago_iso(days)
    c.execute("SELECT COUNT(*) FROM anomalies WHERE id_train=? AND composant=? AND date_signalement>=?", (train_id, composant, since))
    n = c.fetchone()[0]
    conn.close()
    return n

def compute_criticite_calc(train_id, composant, gravite, immobilisation):
    """
    Compute criticity calculation (0-100) using:
    - criticite_max from AMDEC
    - gravite multiplier (Urgent=1.0, Moyen=0.6, Faible=0.3)
    - frequency factor = min(1, occurrences_last_90 / 5)
    - immobilisation factor = 1.0 if immobilisation else 0.6
    Weighted sum -> criticite_calc
    """
    criticite_max = get_component_criticite(composant)
    grav_map = {"Urgent":1.0, "Moyen":0.6, "Faible":0.3}
    grav = grav_map.get(gravite, 0.6)
    occ = count_similar_recent(train_id, composant, days=90)
    freq_factor = min(1.0, occ / 5.0)  # saturates at 1 after 5 occurrences
    imm = 1.0 if immobilisation else 0.6
    # weights chosen: 0.5 for component criticality baseline, 0.3 for grav + imm, 0.2 for frequency
    # We'll compute a baseline influence: criticite_max * (0.5 + 0.3*grav + 0.2*freq_factor*imm)
    score = criticite_max * (0.5 + 0.3 * grav + 0.2 * freq_factor * imm)
    # clamp and round
    score = max(0, min(100, round(score)))
    return int(score)

def recalc_train_health(train_id, days_window=90):
    """
    Health formula:
    health = 100 - ((sum criticite_calc of anomalies in last days_window) / (n * 100)) * 100
    if n==0 -> health = 100
    return int rounded health between 0..100
    """
    conn = get_conn()
    c = conn.cursor()
    since = days_ago_iso(days_window)
    c.execute("SELECT criticite_calc FROM anomalies WHERE id_train=? AND date_signalement>=?", (train_id, since))
    rows = c.fetchall()
    conn.close()
    if not rows:
        health = 100
    else:
        n = len(rows)
        s = sum([r[0] for r in rows])
        max_possible = n * 100
        if max_possible == 0:
            health = 100
        else:
            fraction = s / max_possible
            health_f = 100.0 - (fraction * 100.0)
            health = int(round(max(0.0, min(100.0, health_f))))
    # update DB
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE trains SET etat_sante=? WHERE id_train=?", (health, train_id))
    conn.commit()
    conn.close()
    return health

# convenience: recalc all
def recalc_all_trains():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id_train FROM trains")
    trains = [r[0] for r in c.fetchall()]
    conn.close()
    for t in trains:
        recalc_train_health(t)

# ----------------------------
# Streamlit UI
# ----------------------------
st.set_page_config(page_title="ONCF Atelier Connecté", layout="wide")
st.title("ONCF - Atelier Connecté (Prototype)")

# Simple auth
if 'auth' not in st.session_state:
    st.session_state['auth'] = False
    st.session_state['user'] = None
    st.session_state['role'] = None

def login(username, password):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username=? AND password=?", (username, password))
    r = c.fetchone()
    conn.close()
    if r:
        st.session_state['auth'] = True
        st.session_state['user'] = username
        st.session_state['role'] = r[0]
        return True
    return False

def logout():
    st.session_state['auth'] = False
    st.session_state['user'] = None
    st.session_state['role'] = None

if not st.session_state['auth']:
    st.subheader("Login")
    col1, col2 = st.columns(2)
    with col1:
        username = st.text_input("Nom d'utilisateur (ex: tech or responsable)")
        password = st.text_input("Mot de passe", type="password")
        if st.button("Se connecter"):
            ok = login(username.strip(), password.strip())
            if not ok:
                st.error("Identifiants incorrects")
            else:
                st.success(f"Bienvenue {st.session_state['user']}")
                st.experimental_rerun()
    with col2:
        st.markdown("**Comptes de démonstration :**")
        st.markdown("- Technicien : `tech` / `password`")
        st.markdown("- Responsable : `responsable` / `password`")
    st.stop()

# common UI after login
st.sidebar.write(f"Connecté : **{st.session_state['user']}** ({st.session_state['role']})")
if st.sidebar.button("Se déconnecter"):
    logout()
    st.experimental_rerun()

role = st.session_state['role']
user = st.session_state['user']

# Navigation
if role == "technicien":
    st.sidebar.header("Actions Technicien")
    page = st.sidebar.selectbox("Aller à", ["Accueil", "Nouvelle anomalie", "Mes anomalies", "Fiche de conformité", "Historique train"])
else:
    st.sidebar.header("Actions Responsable")
    page = st.sidebar.selectbox("Aller à", ["Dashboard", "Liste anomalies", "Gestion pièces", "Trains & Santé"])

# small helper to fetch tables
def df_from_query(q, params=()):
    conn = get_conn()
    df = pd.read_sql_query(q, conn, params=params)
    conn.close()
    return df

# ----------------------------
# TECHNICIEN PAGES
# ----------------------------
if role == "technicien" and page == "Accueil":
    st.header("Accueil technicien")
    st.write("Actions rapides :")
    st.write("- Déclarer une nouvelle anomalie")
    st.write("- Consulter ses anomalies et fiches de conformité")
    st.write("Les 3 dernières anomalies (toutes techniciens):")
    df = df_from_query("SELECT id,id_train,technicien,date_signalement,categorie,composant,gravite,criticite_calc,statut FROM anomalies ORDER BY date_signalement DESC LIMIT 3")
    st.dataframe(df)

if role == "technicien" and page == "Nouvelle anomalie":
    st.header("Déclarer une nouvelle anomalie")
    # form
    conn = get_conn()
    trains = pd.read_sql_query("SELECT id_train FROM trains", conn)['id_train'].tolist()
    conn.close()
    with st.form("anomaly_form", clear_on_submit=True):
        sid = st.selectbox("Train", trains)
        cat = st.selectbox("Catégorie", ["mécanique", "électrique", "climatisation", "autre"])
        # components list from DB
        conn = get_conn()
        comps = pd.read_sql_query("SELECT name FROM components", conn)['name'].tolist()
        conn.close()
        comp = st.selectbox("Composant", comps)
        desc = st.text_area("Description")
        photo_file = st.file_uploader("Photo (optionnelle)", type=["png","jpg","jpeg"])
        immobil = st.checkbox("Immobilisation due à la panne (train immobilisé)?")
        grav = st.selectbox("Gravité perçue", ["Urgent","Moyen","Faible"])
        submitted = st.form_submit_button("Enregistrer anomalie")
        if submitted:
            # save photo
            photo_path = ""
            if photo_file:
                saved_path = os.path.join(UPLOAD_DIR, f"{int(datetime.datetime.utcnow().timestamp())}_{photo_file.name}")
                img = Image.open(photo_file)
                img.save(saved_path)
                photo_path = saved_path
            # compute criticity
            crit_calc = compute_criticite_calc(sid, comp, grav, immobil)
            urgence = "critique" if crit_calc >= 80 else ("moyenne" if crit_calc >= 50 else "faible")
            # insert
            conn = get_conn()
            c = conn.cursor()
            c.execute("""INSERT INTO anomalies (
                id_train, technicien, date_signalement, categorie, composant, description, photo,
                immobilisation, gravite, criticite_calc, urgence, statut
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sid, user, now_iso(), cat, comp, desc, photo_path, 1 if immobil else 0, grav, crit_calc, urgence, "à traiter"))
            conn.commit()
            conn.close()
            # recalc health immediately
            new_health = recalc_train_health(sid)
            st.success(f"Anomalie enregistrée — criticité calculée = {crit_calc}. État santé du train recalculé = {new_health}%")
            st.info("L'anomalie est visible dans la liste des anomalies.")

if role == "technicien" and page == "Mes anomalies":
    st.header("Mes anomalies")
    df = df_from_query("SELECT id, id_train, date_signalement, categorie, composant, gravite, criticite_calc, urgence, statut FROM anomalies WHERE technicien=? ORDER BY date_signalement DESC", (user,))
    st.dataframe(df)
    sel = st.number_input("Entrez id d'une anomalie pour modifier/clôturer (laisser vide pour ne rien faire)", min_value=0, value=0)
    if sel:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT id, id_train, description, statut FROM anomalies WHERE id=?", (sel,))
        r = c.fetchone()
        conn.close()
        if r:
            st.write("Anomalie sélectionnée:", r)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("Marquer comme en cours"):
                    conn = get_conn(); c = conn.cursor()
                    c.execute("UPDATE anomalies SET statut='en cours' WHERE id=?", (sel,)); conn.commit(); conn.close()
                    st.success("Statut mis à jour.")
            with col2:
                if st.button("Marquer comme résolu"):
                    conn = get_conn(); c = conn.cursor()
                    c.execute("UPDATE anomalies SET statut='résolu' WHERE id=?", (sel,)); conn.commit(); conn.close()
                    # optionally recalc health for train
                    # find train id
                    conn = get_conn(); c = conn.cursor()
                    c.execute("SELECT id_train FROM anomalies WHERE id=?", (sel,))
                    tid = c.fetchone()[0]
                    conn.close()
                    recalc_train_health(tid)
                    st.success("Anomalie marquée résolue et état santé recalculé.")

if role == "technicien" and page == "Fiche de conformité":
    st.header("Fiche de conformité (post-intervention)")
    conn = get_conn()
    trains = pd.read_sql_query("SELECT id_train FROM trains", conn)['id_train'].tolist()
    parts = pd.read_sql_query("SELECT ref, designation FROM parts", conn)
    conn.close()
    with st.form("conformity_form", clear_on_submit=True):
        train_sel = st.selectbox("Train", trains)
        typ = st.selectbox("Type d'intervention", ["préventive", "corrective"])
        comp = st.selectbox("Composant concerné", pd.read_sql_query("SELECT name FROM components", get_conn())['name'].tolist())
        piece = st.selectbox("Pièce remplacée (si applicable)", [""] + parts.apply(lambda r: f"{r['ref']} - {r['designation']}", axis=1).tolist())
        result = st.selectbox("Résultat", ["Conforme", "Non conforme"])
        obs = st.text_area("Observations")
        submitted = st.form_submit_button("Enregistrer la fiche")
        if submitted:
            piece_ref = piece.split(" - ")[0] if piece else ""
            conn = get_conn(); c = conn.cursor()
            c.execute("""INSERT INTO conformities (
                id_train, date_intervention, technicien, type_intervention, composant, piece_ref, resultat, observations
            ) VALUES (?,?,?,?,?,?,?,?)""", (train_sel, now_iso(), user, typ, comp, piece_ref, result, obs))
            conn.commit()
            conn.close()
            # if piece used, decrement stock
            if piece_ref:
                conn = get_conn(); c = conn.cursor()
                c.execute("UPDATE parts SET qty = qty - 1 WHERE ref = ?", (piece_ref,))
                conn.commit(); conn.close()
            # recalc health (fixes reduce penalty)
            new_health = recalc_train_health(train_sel)
            st.success(f"Fiche enregistrée. État santé recalculé = {new_health}%")

if role == "technicien" and page == "Historique train":
    st.header("Historique interventions par train")
    conn = get_conn()
    trains = pd.read_sql_query("SELECT id_train FROM trains", conn)['id_train'].tolist()
    conn.close()
    sel = st.selectbox("Choisir un train", trains)
    if sel:
        df_a = df_from_query("SELECT id, date_signalement, categorie, composant, gravite, criticite_calc, statut FROM anomalies WHERE id_train=? ORDER BY date_signalement DESC", (sel,))
        df_c = df_from_query("SELECT id, date_intervention, technicien, type_intervention, composant, piece_ref, resultat FROM conformities WHERE id_train=? ORDER BY date_intervention DESC", (sel,))
        st.subheader("Anomalies")
        st.dataframe(df_a)
        st.subheader("Conformités / interventions")
        st.dataframe(df_c)

# ----------------------------
# RESPONSABLE PAGES
# ----------------------------
if role == "responsable" and page == "Dashboard":
    st.header("Dashboard Responsable")
    recalc_all_trains()
    # KPIs
    df_trains = df_from_query("SELECT id_train, etat_sante FROM trains")
    total = len(df_trains)
    bad = len(df_trains[df_trains['etat_sante'] < 50])
    medium = len(df_trains[(df_trains['etat_sante'] >= 50) & (df_trains['etat_sante'] < 80)])
    good = len(df_trains[df_trains['etat_sante'] >= 80])
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Trains total", total)
    col2.metric("Mauvais état (<50%)", bad)
    col3.metric("État moyen (50-79%)", medium)
    col4.metric("Bon état (>=80%)", good)
    # anomalies KPIs
    df_anom = df_from_query("SELECT * FROM anomalies")
    open_count = len(df_anom[df_anom['statut'] != 'résolu'])
    st.write(f"Anomalies enregistrées : {len(df_anom)} — En cours/à traiter : {open_count}")
    # plot: distribution state
    fig = px.pie(df_trains, names='etat_sante', title="Distribution état santé (valeurs réelles)")
    st.plotly_chart(fig, use_container_width=True)
    # evolution health per train (simple historical approach: we have only current health; we will show last 90 days count)
    st.subheader("Anomalies par catégorie")
    df_cat = df_anom.groupby("categorie").size().reset_index(name="count")
    if not df_cat.empty:
        fig2 = px.bar(df_cat, x='categorie', y='count', title="Anomalies par catégorie")
        st.plotly_chart(fig2, use_container_width=True)
    # table of trains with health + quick filters
    st.subheader("Liste des trains")
    df_trains_disp = df_trains.copy()
    df_trains_disp['status_color'] = df_trains_disp['etat_sante'].apply(lambda v: "🔴" if v<50 else ("🟡" if v<80 else "🟢"))
    st.dataframe(df_trains_disp.rename(columns={"id_train":"Train","etat_sante":"État santé","status_color":"Statut"}))

if role == "responsable" and page == "Liste anomalies":
    st.header("Liste des anomalies")
    df = df_from_query("SELECT id, id_train, date_signalement, technicien, categorie, composant, gravite, criticite_calc, urgence, statut FROM anomalies ORDER BY date_signalement DESC")
    st.dataframe(df)
    # filters
    with st.expander("Filtres avancés"):
        urg = st.selectbox("Urgence", ["Toutes", "critique", "moyenne", "faible"])
        cat = st.selectbox("Catégorie", ["Toutes"] + df['categorie'].unique().tolist())
        if st.button("Appliquer"):
            q = "SELECT id, id_train, date_signalement, technicien, categorie, composant, gravite, criticite_calc, urgence, statut FROM anomalies WHERE 1=1"
            params = []
            if urg != "Toutes":
                q += " AND urgence = ?"; params.append(urg)
            if cat != "Toutes":
                q += " AND categorie = ?"; params.append(cat)
            df2 = pd.read_sql_query(q, get_conn(), params=params)
            st.dataframe(df2)

if role == "responsable" and page == "Gestion pièces":
    st.header("Gestion des pièces")
    df_parts = df_from_query("SELECT ref, designation, qty, seuil_min, utilises FROM parts")
    st.dataframe(df_parts)
    st.write("Alerte pièces en-dessous du seuil :")
    low = df_parts[df_parts['qty'] < df_parts['seuil_min']]
    st.dataframe(low)
    st.subheader("Modifier stock")
    ref = st.text_input("Référence à modifier")
    new_qty = st.number_input("Nouvelle quantité", min_value=0, value=0)
    if st.button("Mettre à jour"):
        conn = get_conn(); c = conn.cursor()
        c.execute("UPDATE parts SET qty=? WHERE ref=?", (new_qty, ref))
        conn.commit(); conn.close()
        st.success("Stock mis à jour")

if role == "responsable" and page == "Trains & Santé":
    st.header("Trains & État de santé")
    df_trains = df_from_query("SELECT id_train, modele, km_total, etat_sante, derniere_visite FROM trains")
    st.dataframe(df_trains)
    sel = st.selectbox("Sélectionner un train pour détails", df_trains['id_train'].tolist())
    if sel:
        st.subheader(f"Détails {sel}")
        df_a = df_from_query("SELECT id, date_signalement, categorie, composant, gravite, criticite_calc, statut FROM anomalies WHERE id_train=? ORDER BY date_signalement DESC", (sel,))
        st.write("Anomalies récentes (90 jours)")
        st.dataframe(df_a)
        st.write("Conformités / interventions")
        df_c = df_from_query("SELECT id, date_intervention, technicien, type_intervention, composant, piece_ref, resultat FROM conformities WHERE id_train=? ORDER BY date_intervention DESC", (sel,))
        st.dataframe(df_c)
        # show health
        conn = get_conn(); c = conn.cursor()
        c.execute("SELECT etat_sante FROM trains WHERE id_train=?", (sel,)); health = c.fetchone()[0]; conn.close()
        st.metric("État de santé actuel", f"{health}%")
        # simple color
        color = "🔴" if health<50 else ("🟡" if health<80 else "🟢")
        st.write("Statut:", color)

# End of file
