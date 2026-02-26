import os
import sqlite3
import requests
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Inicialização
load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

# Configurações de Ambiente
DB_PATH = os.getenv('DATABASE_URL')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID')
HEADERS_CW = {"api_access_token": TOKEN_CW}
DIAS_MAP = {0:'segunda', 1:'terca', 2:'quarta', 3:'quinta', 4:'sexta', 5:'sabado', 6:'domingo'}

# Configuração InfluxDB
client_influx = InfluxDBClient(
    url=os.getenv('INFLUXDB_URL'), 
    token=os.getenv('INFLUXDB_TOKEN'), 
    org=os.getenv('INFLUXDB_ORG')
)
write_api = client_influx.write_api(write_options=SYNCHRONOUS)
BUCKET = os.getenv('INFLUXDB_BUCKET')

def get_db():
    """Conexão com SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# --- LÓGICA DO ROBÔ (AUDITORIA) ---

def registrar_metrica(nome, user_id, status_real, status_esperado, evento):
    """Grava dados no InfluxDB para o Grafana."""
    try:
        p = Point("status_agentes") \
            .tag("agente_id", str(user_id)) \
            .tag("nome", str(nome)) \
            .tag("evento", str(evento)) \
            .field("conformidade", 1 if status_real == status_esperado else 0) \
            .field("status_real", str(status_real))
        write_api.write(bucket=BUCKET, record=p)
    except Exception as e:
        print(f"Erro InfluxDB: {e}")

def get_status_esperado(user_id):
    """Calcula se o agente deve estar online, suportando intervalos (vírgulas)."""
    conn = get_db()
    row = conn.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),)).fetchone()
    conn.close()
    
    if not row: return None
    
    dia_semana = DIAS_MAP[datetime.now().weekday()]
    escala_dia = row[dia_semana]
    
    if not escala_dia or escala_dia.strip() == "": return None
    
    hora_agora = datetime.now().strftime("%H:%M")
    # Suporte para almoço: 08:00-13:00, 14:00-17:00
    for turno in escala_dia.split(','):
        try:
            inicio, fim = turno.strip().split('-')
            if inicio <= hora_agora < fim:
                return "online"
        except: continue
    return "offline"

def auditoria_loop():
    """Loop principal de monitoramento."""
    print("Sentinela Kluh iniciado...")
    while True:
        try:
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=15)
            agentes = r.json()

            for ag in agentes:
                status_esperado = get_status_esperado(ag['id'])
                status_atual = ag['availability_status']
                
                if status_esperado:
                    if status_atual != status_esperado:
                        # Corrige no Chatwoot
                        requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{ag['id']}", 
                                     json={"availability": status_esperado}, headers=HEADERS_CW, timeout=10)
                        registrar_metrica(ag['name'], ag['id'], status_atual, status_esperado, "CORRECAO")
                    else:
                        registrar_metrica(ag['name'], ag['id'], status_atual, status_esperado, "ROTINA")
                else:
                    registrar_metrica(ag['name'], ag['id'], status_atual, status_atual, "OBSERVACAO")
                    
        except Exception as e:
            print(f"Erro Auditoria: {e}")
        time.sleep(45)

# --- ROTAS FLASK (INTERFACE) ---

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
    flash("Usuário ou senha incorretos!", "danger") #
    return redirect(url_for('login_page'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

@app.get('/dashboard')
def dashboard_page():
    if not session.get('logged_in'): return redirect(url_for('login_page'))
    return render_template('dashboard.html')

@app.get('/api/agentes')
def list_agentes():
    if not session.get('logged_in'): return jsonify([]), 401
    try:
        # Auto-Sync: Busca agentes novos
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

if __name__ == '__main__':
    # Init DB
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT)''')
    conn.close()
    
    # Inicia Robô
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))