from flask import Flask, render_template, jsonify, request
import pymysql
import json
from datetime import datetime, timezone

app = Flask(__name__)

STATION_METRICS = {
    'Bottling': {'flow_ml_s': 'ml/s', 'fill_time_s': 's', 'pump_load_pct': '%', 'tank_level_ml': 'ml'},
    'Distributing': {'cycle_time_s': 's', 'actuator_response_time_s': 's'},
    'MPS-PA': {'pressure_bar': 'bar', 'flow_l_min': 'l/min', 'temperature_c': 'C', 'level_percent': '%'},
    'Pick_and_Place': {'cycle_time_s': 's', 'vacuum_pressure_bar': 'bar'},
    'Separating': {'measured_height_mm': 'mm', 'sensor_error_mm': 'mm', 'cycle_time_s': 's'},
    'Sorting': {'sorting_time_s': 's'},
}

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
                        ORDER BY timestamp DESC
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
                for row in reversed(rows)
            ],
        })
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Parametri invalizi: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500


@app.route('/api/summary')
def api_summary():
    try:
        now = datetime.now(timezone.utc)
        start = parse_datetime(request.args.get('from'), now.replace(hour=0, minute=0, second=0, microsecond=0))
        end = parse_datetime(request.args.get('to'), now)
        station = request.args.get('station')
        limit = min(max(int(request.args.get('limit', 10000)), 1), 20000)
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
                        ORDER BY timestamp DESC LIMIT %s""",
                    params,
                )
                rows = cursor.fetchall()
        finally:
            conn.close()

        summaries = {}
        for row in rows:
            payload = json.loads(row['payload_json'])
            name = row['station_name']
            summary = summaries.setdefault(name, {
                'station': name, 'samples': 0, 'first_seen': None, 'last_seen': None,
                'health': {'latest': None, 'average_degradation': 0, 'peak_degradation': 0},
                'metrics': {}, 'faults': {}, 'events': {}, 'process_states': {},
            })
            timestamp = row['timestamp'].isoformat() + 'Z'
            summary['samples'] += 1
            summary['last_seen'] = summary['last_seen'] or timestamp
            summary['first_seen'] = timestamp
            health = payload.get('health', {})
            degradation = float(health.get('degradation_score', 0) or 0)
            summary['health']['average_degradation'] += degradation
            summary['health']['peak_degradation'] = max(summary['health']['peak_degradation'], degradation)
            if summary['health']['latest'] is None:
                summary['health']['latest'] = degradation
            for fault, severity in (health.get('active_faults') or {}).items():
                summary['faults'][fault] = max(float(severity), summary['faults'].get(fault, 0))
            for event in payload.get('events', []):
                code = event.get('code', 'UNKNOWN')
                summary['events'][code] = summary['events'].get(code, 0) + 1
            process_state = payload.get('process', {}).get('state', 'UNKNOWN')
            summary['process_states'][process_state] = summary['process_states'].get(process_state, 0) + 1
            measurements = payload.get('measurements', {})
            baseline = payload.get('baseline', {})
            for metric, unit in STATION_METRICS.get(name, {}).items():
                value = measurements.get(metric)
                if not isinstance(value, (int, float)):
                    continue
                item = summary['metrics'].setdefault(metric, {'unit': unit, 'latest': value, 'minimum': value, 'maximum': value, 'average': 0, 'baseline': baseline.get(metric)})
                item['latest'] = value if summary['samples'] == 1 else item['latest']
                item['minimum'] = min(item['minimum'], value)
                item['maximum'] = max(item['maximum'], value)
                item['average'] += value

        for summary in summaries.values():
            summary['health']['average_degradation'] = round(summary['health']['average_degradation'] / summary['samples'], 4)
            summary['health']['peak_degradation'] = round(summary['health']['peak_degradation'], 4)
            summary['health']['latest'] = round(summary['health']['latest'], 4)
            for metric in summary['metrics'].values():
                metric['average'] = round(metric['average'] / summary['samples'], 4)
                if metric['baseline'] is not None:
                    metric['deviation_percent'] = round((metric['latest'] - metric['baseline']) / abs(metric['baseline']) * 100, 2) if metric['baseline'] else None

        return jsonify({'from': start.isoformat() + 'Z', 'to': end.isoformat() + 'Z', 'count': len(rows), 'stations': summaries})
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Parametri invalizi: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
