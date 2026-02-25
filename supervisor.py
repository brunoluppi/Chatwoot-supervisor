import os
import sqlite3
import requests
import threading
import time
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS # Necessário para o Grafana externo acessar a API
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

load_dotenv()
app = Flask(__name__)
CORS(app) # Libera o Grafana para fazer chamadas cross-origin

DB_PATH = "configuracoes.db"

# --- CONFIGURAÇÃO INFLUXDB ---
INFLUX_CLIENT = InfluxDBClient(url=os.getenv('INFLUXDB_URL'), token=os.getenv('INFLUXDB_TOKEN'), org=os.getenv('INFLUXDB_ORG'))
WRITE_API = INFLUX_CLIENT.write_api(write_options=SYNCHRONOUS)

# --- CONFIGURAÇÃO CHATWOOT ---
URL = os.getenv('CHATWOOT_URL')
TOKEN = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACCOUNT_ID = os.getenv('CHATWOOT_ACCOUNT_ID', '1')
HEADERS = {"api_access_token": TOKEN}

DIAS_MAP = {0: 'segunda', 1: 'terca', 2: 'quarta', 3: 'quinta', 4: 'sexta', 5: 'sabado', 6: 'domingo'}

# --- BANCO DE DADOS LOCAL (CONTROLE) ---
def init_db():
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

# --- ROTAS DE GESTÃO PARA O GRAFANA ---

@app.route('/api/operadores', methods=['GET'])
def listar_operadores():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM escalas")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)

@app.route('/api/operadores', methods=['POST'])
def salvar_operador():
    data = request.json
    # Espera JSON com todos os dias: {"agente_id": "1", "nome": "Luppi", "segunda": "08:00-18:00", ...}
    campos = ['agente_id', 'nome', 'ativo', 'segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
    valores = [data.get(c) for c in campos]
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(f"INSERT OR REPLACE INTO escalas ({','.join(campos)}) VALUES ({','.join(['?']*10)})", valores)
    conn.commit()
    conn.close()
    return jsonify({"status": "sucesso"})

@app.route('/api/operadores/<id>', methods=['DELETE'])
def excluir_operador(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM escalas WHERE agente_id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "excluido"})

# --- LÓGICA DE SENTINELA ---

def get_status_esperado(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),))
    row = c.fetchone()
    conn.close()
    
    if not row: return None

    agora = datetime.now()
    hora_atual = agora.strftime("%H:%M")
    dia_semana = DIAS_MAP[agora.weekday()]
    
    escala_dia = row[dia_semana] # Formato: "08:00-12:00,13:00-18:00"
    if not escala_dia or escala_dia.strip() == "": return "offline"

    try:
        turnos = escala_dia.split(',')
        for turno in turnos:
            inicio, fim = turno.split('-')
            if inicio.strip() <= hora_atual < fim.strip():
                return "online"
    except: pass
    
    return "offline"

def registrar_influx(nome, user_id, status_det, status_for, evento):
    try:
        point = Point("status_agentes") \
            .tag("agente_id", str(user_id)).tag("nome", nome).tag("evento", evento) \
            .field("conformidade", 1 if status_det == status_for else 0) \
            .field("status_real", status_det) \
            .time(datetime.utcnow(), WritePrecision.NS)
        WRITE_API.write(bucket=os.getenv('INFLUXDB_BUCKET'), record=point)
    except Exception as e:
        print(f"Erro Influx: {e}")

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    user_id = data.get('id')
    status_atual = data.get('availability_status')
    nome = data.get('name', 'Desconhecido')
    
    status_correto = get_status_esperado(user_id)
    
    if status_correto and status_atual != status_correto:
        requests.put(f"{URL}/api/v1/accounts/{ACCOUNT_ID}/agents/{user_id}", 
                     json={"availability": status_correto}, headers=HEADERS, timeout=5)
        registrar_influx(nome, user_id, status_atual, status_correto, "VIOLACAO_MANUAL")
    else:
        registrar_influx(nome, user_id, status_atual, status_atual or "offline", "SINC_ROTINA")
        
    return jsonify({"status": "ok"}), 200

# --- AUDITORIA DE PRESENÇA (THREADS) ---
def auditoria_loop():
    while True:
        try:
            r = requests.get(f"{URL}/api/v1/accounts/{ACCOUNT_ID}/agents", headers=HEADERS, timeout=10)
            agentes = r.json()
            for ag in agentes:
                status_esp = get_status_esperado(ag['id'])
                if status_esp == "online" and ag['availability_status'] == "offline":
                    registrar_influx(ag['name'], ag['id'], "offline", "online", "AUSENCIA_DETECTADA")
        except: pass
        time.sleep(300)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))