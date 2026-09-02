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
    try:
        conn = get_db_connection()
        latest_data = {}
        with conn.cursor() as cursor:
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

            for row in results:
                latest_data[row['station_name']] = json.loads(row['payload_json'])
        
        conn.close()
        return jsonify(latest_data)
        
    except Exception as e:
        # Acum vom vedea eroarea clara pe ecran in format JSON
        return jsonify({"error": str(e), "tip_eroare": str(type(e))}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
