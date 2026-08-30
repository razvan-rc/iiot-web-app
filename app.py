from flask import Flask, render_template
import pymysql

app = Flask(__name__)

def get_db_connection():
    # Nginx-ul si Gunicorn-ul ruleaza pe Web Server, deci se conecteaza la DB Slave
    return pymysql.connect(
        host='172.31.45.5', # IP-ul Privat al DB Slave
        user='sensor_app',
        password='SenzorPass123!',
        database='industrial_db',
        cursorclass=pymysql.cursors.DictCursor
    )

@app.route('/')
def index():
    conn = get_db_connection()
    with conn.cursor() as cursor:
        # Luam ultimele 15 inregistrari pentru grafic si tabel
        cursor.execute("SELECT * FROM festo_telemetry ORDER BY timestamp DESC LIMIT 15")
        readings = cursor.fetchall()
        
        # Luam un indicator rapid: statusul ultimei citiri
        cursor.execute("SELECT status, air_pressure_bar FROM festo_telemetry ORDER BY timestamp DESC LIMIT 1")
        latest = cursor.fetchone()
    conn.close()
    
    # Inversam lista pentru ca graficul sa arate cronologic (de la stanga la dreapta)
    readings = readings[::-1]
    
    return render_template('index.html', readings=readings, latest=latest)

if __name__ == '__main__':
    app.run(debug=True)
