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

# Carrega variáveis de ambiente
load_dotenv()
app = Flask(__name__)
# CORS liberado para o domínio do Grafana
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Configurações de Caminho e API
DB_PATH = os.getenv('DATABASE_URL', '/app/database/configuracoes.db')
URL_CW = os.getenv('CHATWOOT_URL')
TOKEN_CW = os.getenv('CHATWOOT_ACCESS_TOKEN')
ACC_ID = os.getenv('CHATWOOT_ACCOUNT_ID', '1')
HEADERS_CW = {"api_access_token": TOKEN_CW}

# Configuração InfluxDB
client_influx = InfluxDBClient(
    url=os.getenv('INFLUXDB_URL'), 
    token=os.getenv('INFLUXDB_TOKEN'), 
    org=os.getenv('INFLUXDB_ORG')
)
write_api = client_influx.write_api(write_options=SYNCHRONOUS)
BUCKET = os.getenv('INFLUXDB_BUCKET', 'chatwoot_supervisor')

DIAS_MAP = {0: 'segunda', 1: 'terca', 2: 'quarta', 3: 'quinta', 4: 'sexta', 5: 'sabado', 6: 'domingo'}

def init_db():
    """Inicializa a tabela de escalas se não existir."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT)''')
    conn.close()

def sincronizar_com_chatwoot():
    """Busca agentes no Chatwoot e popula o SQLite automaticamente."""
    try:
        r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=10)
        r.raise_for_status()
        agentes_cw = r.json()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for ag in agentes_cw:
            # Verifica se o agente já existe para não sobrescrever escalas personalizadas
            c.execute("SELECT 1 FROM escalas WHERE agente_id = ?", (str(ag['id']),))
            if not c.fetchone():
                c.execute('''INSERT INTO escalas (agente_id, nome, ativo, segunda, terca, quarta, quinta, sexta) 
                             VALUES (?, ?, 1, '08:00-18:00', '08:00-18:00', '08:00-18:00', '08:00-18:00', '08:00-18:00')''', 
                          (str(ag['id']), ag['name']))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erro no Auto-Sync: {e}")

# --- API DE GESTÃO PARA O GRAFANA ---
@app.route('/api/operadores', methods=['GET', 'POST', 'OPTIONS'])
def gerenciar_operadores():
    if request.method == 'OPTIONS': 
        return jsonify({"status": "ok"}), 200
    
    if request.method == 'GET':
        # Sincroniza antes de entregar os dados para o painel de gestão
        sincronizar_com_chatwoot()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM escalas ORDER BY nome ASC").fetchall()]
        conn.close()
        return jsonify(rows)

    if request.method == 'POST':
        data = request.json
        campos = ['agente_id', 'nome', 'ativo', 'segunda', 'terca', 'quarta', 'quinta', 'sexta', 'sabado', 'domingo']
        # Trata campos nulos e converte booleano 'ativo' para inteiro
        valores = [str(data.get(c, "")) if c != 'ativo' else int(data.get(c, 1)) for c in campos]
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(f"INSERT OR REPLACE INTO escalas ({','.join(campos)}) VALUES ({','.join(['?']*10)})", valores)
        conn.commit()
        conn.close()
        return jsonify({"status": "sucesso"}), 200

# --- LÓGICA DE AUDITORIA E MÉTRICAS ---
def registrar_metrica(nome, user_id, status_det, status_for, evento):
    """Grava o status e a conformidade no InfluxDB."""
    try:
        p = Point("status_agentes") \
            .tag("agente_id", str(user_id)) \
            .tag("nome", str(nome)) \
            .tag("evento", str(evento)) \
            .field("conformidade", 1 if status_det == status_for else 0) \
            .field("status_real", str(status_det))
        write_api.write(bucket=BUCKET, record=p)
    except Exception as e:
        print(f"Erro InfluxDB: {e}")

def get_status_esperado(user_id):
    """Consulta a escala no SQLite para o horário atual."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),)).fetchone()
    conn.close()
    
    if not row: return None
    
    dia_semana = DIAS_MAP[datetime.now().weekday()]
    escala_dia = row[dia_semana]
    if not escala_dia: return "offline"
    
    hora_agora = datetime.now().strftime("%H:%M")
    # Suporta múltiplos turnos separados por vírgula
    for turno in escala_dia.split(','):
        try:
            inicio, fim = turno.strip().split('-')
            if inicio <= hora_agora < fim:
                return "online"
        except: continue
    return "offline"

def auditoria_loop():
    """Thread que monitora o Chatwoot a cada 45s."""
    while True:
        try:
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=15)
            for ag in r.json():
                status_esp = get_status_esperado(ag['id'])
                if status_esp and ag['availability_status'] != status_esp:
                    # Corrige o status no Chatwoot se estiver fora da escala
                    requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{ag['id']}", 
                                 json={"availability": status_esp}, headers=HEADERS_CW, timeout=10)
                    registrar_metrica(ag['name'], ag['id'], ag['availability_status'], status_esp, "CORRECAO")
                else:
                    registrar_metrica(ag['name'], ag['id'], ag['availability_status'], status_esp or ag['availability_status'], "ROTINA")
        except Exception as e:
            print(f"Erro Loop Auditoria: {e}")
        time.sleep(45)

if __name__ == '__main__':
    init_db()
    # Inicia auditoria em background
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))