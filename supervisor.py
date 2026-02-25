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
# Configuração de CORS para permitir acesso do Grafana externo
CORS(app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "DELETE", "OPTIONS"]}})

# O banco deve estar no volume persistente para durar mais de 1 ano
DB_PATH = os.getenv('DATABASE_URL', '/app/database/configuracoes.db')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID', '1')
HEADERS_CW = {"api_access_token": TOKEN_CW}

# Configuração InfluxDB para métricas de 1 ano
client_influx = InfluxDBClient(url=os.getenv('INFLUXDB_URL'), token=os.getenv('INFLUXDB_TOKEN'), org=os.getenv('INFLUXDB_ORG'))
write_api = client_influx.write_api(write_options=SYNCHRONOUS)
BUCKET = os.getenv('INFLUXDB_BUCKET')

DIAS_MAP = {0: 'segunda', 1: 'terca', 2: 'quarta', 3: 'quinta', 4: 'sexta', 5: 'sabado', 6: 'domingo'}

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT)''')
    conn.close()

# --- API DE GESTÃO ---
# strict_slashes=False permite que /api/operadores e /api/operadores/ funcionem
@app.route('/api/operadores', methods=['GET', 'POST'], strict_slashes=False)
def gerenciar_operadores():
    if request.method == 'GET':
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM escalas").fetchall()]
        conn.close()
        return jsonify(rows)

    if request.method == 'POST':
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        campos = ['agente_id', 'nome', 'ativo', 'segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        valores = [data.get(c, "") if c != 'ativo' else data.get(c, 1) for c in campos]
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        query = f"INSERT OR REPLACE INTO escalas ({','.join(campos)}) VALUES ({','.join(['?']*10)})"
        c.execute(query, valores)
        conn.commit()
        conn.close()
        return jsonify({"status": "sucesso"}), 200

@app.route('/api/operadores/<id>', methods=['DELETE'], strict_slashes=False)
def excluir(id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM escalas WHERE agente_id = ?", (id,))
    conn.commit(); conn.close()
    return jsonify({"status": "excluido"})

# --- LÓGICA DE AUDITORIA ---
def get_status_esperado(user_id):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),)).fetchone()
    conn.close()
    if not row: return None
    
    escala_dia = row[DIAS_MAP[datetime.now().weekday()]]
    if not escala_dia or escala_dia.strip() == "": return "offline"
    
    hora_dt = datetime.now().strftime("%H:%M")
    for turno in escala_dia.split(','):
        try:
            inicio, fim = turno.split('-')
            if inicio.strip() <= hora_dt < fim.strip(): return "online"
        except: continue
    return "offline"

def registrar_metrica(nome, user_id, status_det, status_for, evento):
    try:
        p = Point("status_agentes").tag("agente_id", str(user_id)).tag("nome", nome).tag("evento", evento) \
            .field("conformidade", 1 if status_det == status_for else 0).field("status_real", status_det)
        write_api.write(bucket=BUCKET, record=p)
    except: pass

def auditoria_loop():
    while True:
        try:
            # Varredura ativa pois o Chatwoot não envia webhook de status
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=10)
            agentes = r.json()
            for ag in agentes:
                uid, nome, status_at = ag['id'], ag['name'], ag['availability_status']
                status_esp = get_status_esperado(uid)
                if status_esp and status_at != status_esp:
                    requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{uid}", 
                                 json={"availability": status_esp}, headers=HEADERS_CW, timeout=5)
                    registrar_metrica(nome, uid, status_at, status_esp, "CORRECAO_ATIVA")
                else:
                    registrar_metrica(nome, uid, status_at, status_esp or status_at, "SINC_ROTINA")
        except: pass
        time.sleep(45)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))