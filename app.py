from flask import Flask, render_template, jsonify
import pymysql

app = Flask(__name__)

# --- CONFIGURARE BAZA DE DATE (NODUL SLAVE) ---
DB_HOST = "172.31.45.5"  # IP PRIVAT DB SLAVE
DB_USER = "sensor_app"
DB_PASS = "SenzorPass123!"
DB_NAME = "industrial_db"

def get_db_connection():
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor # Returneaza datele ca dictionar
    )

# RUTA 1: Pagina principala (Dashboard-ul)
@app.route('/')
def index():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Extragem ultimele 15 inregistrari pentru tabelul istoric
    cursor.execute("""
        SELECT timestamp, container_id, station_name, container_color, air_pressure_bar, status 
        FROM festo_telemetry 
        ORDER BY timestamp DESC 
        LIMIT 15
    """)
    recent_data = cursor.fetchall()
    conn.close()
    
    return render_template('index.html', data=recent_data)

# RUTA 2: API pentru graficele Chart.js (Real-time data)
@app.route('/api/chart-data')
def chart_data():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Extragem ultimele 30 de citiri, dar le ordonam cronologic pentru grafic
    cursor.execute("""
        SELECT timestamp, air_pressure_bar
        FROM (
            SELECT timestamp, air_pressure_bar
            FROM festo_telemetry
            ORDER BY timestamp DESC
            LIMIT 30
        ) sub
        ORDER BY timestamp ASC
    """)
    data = cursor.fetchall()
    conn.close()

    # Formatam datele in liste separate pentru frontend (axele X si Y)
    labels = [row['timestamp'].strftime('%H:%M:%S') for row in data]
    pressures = [row['air_pressure_bar'] for row in data]

    return jsonify({'labels': labels, 'pressures': pressures})

if __name__ == '__main__':
    # Rulam pe portul 5000, vizibil pe toate interfetele
    app.run(host='0.0.0.0', port=5000)
