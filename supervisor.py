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
# CORS amplo para o domínio da Kluh Software
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Configurações de Ambiente
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
    """Inicializa o SQLite."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS escalas (
                    agente_id TEXT PRIMARY KEY, nome TEXT, ativo INTEGER DEFAULT 1,
                    segunda TEXT, terca TEXT, quarta TEXT, quinta TEXT, sexta TEXT, sabado TEXT, domingo TEXT)''')
    conn.close()

def sincronizar_com_chatwoot():
    """Auto-popula o banco com novos agentes do Chatwoot."""
    try:
        r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=10)
        r.raise_for_status()
        agentes_cw = r.json()
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        for ag in agentes_cw:
            c.execute("SELECT 1 FROM escalas WHERE agente_id = ?", (str(ag['id']),))
            if not c.fetchone():
                # Insere sem escala padrão para não forçar status indevidamente
                c.execute("INSERT INTO escalas (agente_id, nome, ativo) VALUES (?, ?, 1)", (str(ag['id']), ag['name']))
        conn.commit(); conn.close()
    except Exception as e: print(f"Erro Auto-Sync: {e}")

@app.route('/api/operadores', methods=['GET', 'POST', 'OPTIONS'])
def gerenciar_operadores():
    if request.method == 'OPTIONS': return jsonify({"status": "ok"}), 200
    
    if request.method == 'GET':
        sincronizar_com_chatwoot()
        # Captura o nome do agente enviado pelo Grafana
        nome_filtro = request.args.get('nome')
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        
        if nome_filtro:
            # Retorna apenas o agente selecionado no topo do dashboard
            row = conn.execute("SELECT * FROM escalas WHERE nome = ?", (nome_filtro,)).fetchone()
            conn.close()
            return jsonify(dict(row) if row else {})
        
        # Fallback: retorna tudo (útil para debug via curl)
        rows = [dict(r) for r in conn.execute("SELECT * FROM escalas").fetchall()]
        conn.close()
        return jsonify(rows)

def registrar_metrica(nome, user_id, status_det, status_for, evento):
    """Grava no InfluxDB. Note que o 'evento' agora é uma tag para histórico."""
    try:
        p = Point("status_agentes") \
            .tag("agente_id", str(user_id)) \
            .tag("nome", str(nome)) \
            .tag("evento", str(evento)) \
            .field("conformidade", 1 if status_det == status_for else 0) \
            .field("status_real", str(status_det))
        write_api.write(bucket=BUCKET, record=p)
    except Exception as e: print(f"Erro InfluxDB: {e}")

def get_status_esperado(user_id):
    """Define se o agente deve estar online. Retorna None se não houver regra."""
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM escalas WHERE agente_id = ? AND ativo = 1", (str(user_id),)).fetchone()
    conn.close()
    if not row: return None
    
    escala_dia = row[DIAS_MAP[datetime.now().weekday()]]
    if not escala_dia or escala_dia.strip() == "": return None
    
    hora_agora = datetime.now().strftime("%H:%M")
    for turno in escala_dia.split(','):
        try:
            inicio, fim = turno.strip().split('-')
            if inicio <= hora_agora < fim: return "online"
        except: continue
    return "offline"

def auditoria_loop():
    """Loop principal. Só corrige o status se houver escala definida."""
    while True:
        try:
            r = requests.get(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents", headers=HEADERS_CW, timeout=15)
            for ag in r.json():
                status_esp = get_status_esperado(ag['id'])
                
                if status_esp is not None:
                    if ag['availability_status'] != status_esp:
                        requests.put(f"{URL_CW}/api/v1/accounts/{ACC_ID}/agents/{ag['id']}", 
                                     json={"availability": status_esp}, headers=HEADERS_CW, timeout=10)
                        registrar_metrica(ag['name'], ag['id'], ag['availability_status'], status_esp, "CORRECAO")
                    else:
                        registrar_metrica(ag['name'], ag['id'], ag['availability_status'], status_esp, "ROTINA")
                else:
                    # Não altera o status original se não houver horário cadastrado
                    registrar_metrica(ag['name'], ag['id'], ag['availability_status'], ag['availability_status'], "OBSERVACAO")
        except Exception as e: print(f"Erro Auditoria: {e}")
        time.sleep(45)

if __name__ == '__main__':
    init_db()
    threading.Thread(target=auditoria_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))