from flask import Flask, render_template, jsonify, request
import pymysql
import json
from datetime import datetime, timezone

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


def parse_datetime(value, default):
    if not value:
        return default
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)

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


@app.route('/api/history')
def api_history():
    try:
        now = datetime.now(timezone.utc)
        start = parse_datetime(request.args.get('from'), now.replace(hour=0, minute=0, second=0, microsecond=0))
        end = parse_datetime(request.args.get('to'), now)
        station = request.args.get('station')
        limit = min(max(int(request.args.get('limit', 2000)), 1), 10000)

        clauses = ['timestamp >= %s', 'timestamp <= %s']
        params = [start, end]
        if station:
            clauses.append('station_name = %s')
            params.append(station)
        params.append(limit)

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT timestamp, station_name, payload_json
                        FROM festo_telemetry
                        WHERE {' AND '.join(clauses)}
                        ORDER BY timestamp ASC
                        LIMIT %s""",
                    params,
                )
                rows = cursor.fetchall()
        finally:
            conn.close()

        return jsonify({
            'from': start.isoformat() + 'Z',
            'to': end.isoformat() + 'Z',
            'count': len(rows),
            'rows': [
                {
                    'timestamp': row['timestamp'].isoformat() + 'Z',
                    'station': row['station_name'],
                    'payload': json.loads(row['payload_json']),
                }
                for row in rows
            ],
        })
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Parametri invalizi: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
