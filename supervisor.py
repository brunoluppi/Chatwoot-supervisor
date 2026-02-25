import os
import sqlite3
import requests
import threading
import time
from datetime import datetime
from flask import Flask, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

DB_PATH = os.getenv('DATABASE_URL', '/app/database/configuracoes.db')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID', '1')
HEADERS_CW = {"api_access_token": TOKEN_CW}

# InfluxDB
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

@app.route('/api/operadores', methods=['GET'])
def listar():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM escalas").fetchall()]
    conn.close()
    return jsonify(rows)

# Mantenha as rotas POST e DELETE iguais para a gestão via Grafana...

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
    except: pass

# --- CORE: Auditoria de Alta Frequência ---
def auditoria_loop():
    print("🚀 Sentinela iniciado em modo de varredura ativa.")
    while True:
        try:
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=10)
            agentes = r.json()
            
            for ag in agentes:
                uid, nome, status_atual = ag['id'], ag['name'], ag['availability_status']
                status_esp = get_status_esperado(uid)
                
                if status_esp and status_atual != status_esp:
                    # Correção Ativa
                    requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{uid}", 
                                 json={"availability": status_esp}, headers=HEADERS_CW, timeout=5)
                    
                    evento = "VIOLACAO_MANUAL" if status_atual != "offline" else "AUSENCIA_DETECTADA"
                    registrar_metrica(nome, uid, status_atual, status_esp, evento)
                    print(f"⚖️ Agente {nome} corrigido para {status_esp}.")
                else:
                    # Telemetria de Rotina
                    registrar_metrica(nome, uid, status_atual, status_esp or status_atual, "SINC_ROTINA")
                    
        except Exception as e:
            print(f"Erro na varredura: {e}")
        
        time.sleep(45) # Intervalo de varredura (ajustável)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))