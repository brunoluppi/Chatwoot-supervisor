import os, sqlite3, requests, threading, time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

# Configurações Globais
DB_PATH = os.getenv('DATABASE_URL')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID')
HEADERS_CW = {"api_access_token": TOKEN_CW}
DIAS_MAP = {0:'segunda', 1:'terca', 2:'quarta', 3:'quinta', 4:'sexta', 5:'sabado', 6:'domingo'}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- AUTENTICAÇÃO ---
@app.route('/')
def login_page():
    if session.get('logged_in'): return redirect(url_for('dashboard_page'))
    return render_template('login.html')

@app.route('/auth', methods=['POST'])
def auth():
    user = request.form.get('user')
    password = request.form.get('pass')
    
    if user == os.getenv('ADMIN_USER') and password == os.getenv('ADMIN_PASS'):
        session['logged_in'] = True
        return redirect(url_for('dashboard_page'))
    else:
        flash("Usuário ou senha incorretos!", "danger")
        return redirect(url_for('login_page'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# --- INTERFACE E API ---
@app.get('/dashboard')
def dashboard_page():
    if not session.get('logged_in'): return redirect(url_for('login_page'))
    return render_template('dashboard.html')

@app.get('/api/agentes')
def list_agentes():
    if not session.get('logged_in'): return jsonify([]), 401
    try:
        # Auto-Sync com Chatwoot
        r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=10)
        agentes_cw = r.json()
        conn = get_db()
        for ag in agentes_cw:
            conn.execute("INSERT OR IGNORE INTO escalas (agente_id, nome, ativo) VALUES (?, ?, 1)", (str(ag['id']), ag['name']))
        conn.commit()
        rows = conn.execute("SELECT * FROM escalas ORDER BY nome ASC").fetchall()
        conn.close()
        return jsonify([dict(ix) for ix in rows])
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.post('/api/salvar')
def salvar_escala():
    if not session.get('logged_in'): return "Unauthorized", 401
    d = request.json
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO escalas 
        (agente_id, nome, ativo, segunda, terca, quarta, quinta, sexta, sabado, domingo) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", 
        (d['agente_id'], d['nome'], int(d['ativo']), d.get('segunda',''), d.get('terca',''), 
         d.get('quarta',''), d.get('quinta',''), d.get('sexta',''), d.get('sabado',''), d.get('domingo','')))
    conn.commit()
    conn.close()
    return jsonify({"status": "sucesso"})

# --- ROBÔ DE AUDITORIA ---
def auditoria_loop():
    while True:
        try:
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=15)
            for ag in r.json():
                # Lógica de verificação de horário (respeita status original se escala for nula)
                pass # Implementar lógica de comparação e escrita no InfluxDB aqui
        except Exception as e: print(f"Erro Auditoria: {e}")
        time.sleep(45)

if __name__ == '__main__':
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT)''')
    conn.close()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))