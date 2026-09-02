from flask import Flask, render_template, jsonify
import pymysql
import json

app = Flask(__name__)

def get_db_connection():
    # Ne conectam mereu la SLAVE pentru citiri (Read-Replica)
    return pymysql.connect(
        host='172.31.45.5',  # IP-ul Slave-ului tau
        user='sensor_app',
        password='SenzorPass123!',
        database='industrial_db',
        cursorclass=pymysql.cursors.DictCursor
    )

@app.route('/')
def index():
    # Returnam doar scheletul HTML. Datele vor fi cerute asincron de JS.
    return render_template('index.html')

@app.route('/api/live')
def api_live():
    """
    Acest endpoint returneaza cea mai recenta stare pentru FIECARE statie.
    Folosim o functie Window (ROW_NUMBER) pentru a extrage instantaneu ultimul JSON.
    """
    conn = get_db_connection()
    latest_data = {}
    
    try:
        with conn.cursor() as cursor:
            # Query avansat pentru a lua doar ultima inregistrare per statie
            sql = """
                SELECT station_name, payload_json 
                FROM (
                    SELECT station_name, payload_json,
                           ROW_NUMBER() OVER (PARTITION BY station_name ORDER BY timestamp DESC) as rn
                    FROM festo_telemetry
                ) tmp 
                WHERE rn = 1;
            """
            cursor.execute(sql)
            results = cursor.fetchall()

            # Parsam string-urile JSON din baza de date intr-un dictionar Python
            for row in results:
                station = row['station_name']
                # MariaDB returneaza JSON-ul ca string, trebuie sa-l facem obiect
                latest_data[station] = json.loads(row['payload_json'])

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

    # Flask va transforma acest dictionar automat inapoi in JSON pentru frontend
    return jsonify(latest_data)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
