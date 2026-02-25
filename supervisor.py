import os
import sqlite3
import requests
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
app = Flask(__name__)
CORS(app)

# --- CONFIGURAÇÕES ---
DB_PATH = os.getenv('DATABASE_URL', '/app/database/configuracoes.db')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID', '1')
HEADERS_CW = {"api_access_token": TOKEN_CW}

# InfluxDB
client_influx = InfluxDBClient(
    url=os.getenv('INFLUXDB_URL'), 
    token=os.getenv('INFLUXDB_TOKEN'), 
    org=os.getenv('INFLUXDB_ORG')
)
write_api = client_influx.write_api(write_options=SYNCHRONOUS)
BUCKET = os.getenv('INFLUXDB_BUCKET')

DIAS_MAP = {0: 'segunda', 1: 'terca', 2: 'quarta', 3: 'quinta', 4: 'sexta', 5: 'sabado', 6: 'domingo'}

# --- BANCO DE DADOS (SQLite Persistente) ---
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY,
                    nome TEXT,
                    ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT
                )''')
    conn.commit()
    conn.close()

# --- API DE GESTÃO (Para Grafana Externo) ---
@app.route('/api/operadores', methods=['GET'])
def listar():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM escalas").fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/operadores', methods=['POST'])
def salvar():
    data = request.json
    campos = ['agente_id', 'nome', 'ativo', 'segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
    valores = [data.get(c) for c in campos]
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute(f"INSERT OR REPLACE INTO escalas ({','.join(campos)}) VALUES ({','.join(['?']*10)})", valores)
    conn.commit(); conn.close()
    return jsonify({"status": "sucesso"})

@app.route('/api/operadores/<id>', methods=['DELETE'])
def excluir(id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM escalas WHERE agente_id = ?", (id,))
    conn.commit(); conn.close()
    return jsonify({"status": "excluido"})

# --- LÓGICA SENTINELA ---
def get_status_esperado(user_id):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),)).fetchone()
    conn.close()
    if not row: return None
    
    escala_dia = row[DIAS_MAP[datetime.now().weekday()]]
    if not escala_dia: return "offline"
    
    hora_atual = datetime.now().strftime("%H:%M")
    for turno in escala_dia.split(','):
        try:
            inicio, fim = turno.split('-')
            if inicio.strip() <= hora_atual < fim.strip(): return "online"
        except: continue
    return "offline"

def registrar_metrica(nome, user_id, status_det, status_for, evento):
    try:
        p = Point("status_agentes").tag("agente_id", str(user_id)).tag("nome", nome).tag("evento", evento) \
            .field("conformidade", 1 if status_det == status_for else 0).field("status_real", status_det)
        write_api.write(bucket=BUCKET, record=p)
    except Exception as e: print(f"Erro Influx: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    uid, status_at, nome = data.get('id'), data.get('availability_status'), data.get('name')
    status_esp = get_status_esperado(uid)
    if status_esp and status_at != status_esp:
        requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{uid}", json={"availability": status_esp}, headers=HEADERS_CW)
        registrar_metrica(nome, uid, status_at, status_esp, "VIOLACAO_MANUAL")
    else:
        registrar_metrica(nome, uid, status_at, status_esp or "offline", "SINC_ROTINA")
    return jsonify({"status": "ok"})

def auditoria_loop():
    while True:
        try:
            agentes = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW).json()
            for ag in agentes:
                status_esp = get_status_esperado(ag['id'])
                if status_esp == "online" and ag['availability_status'] == "offline":
                    registrar_metrica(ag['name'], ag['id'], "offline", "online", "AUSENCIA_DETECTADA")
        except: pass
        time.sleep(300)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))