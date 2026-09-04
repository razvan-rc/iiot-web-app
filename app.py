from flask import Flask, render_template, jsonify, request
import pymysql
import json
import math
from datetime import datetime, timezone

app = Flask(__name__)

STATION_METRICS = {
    'Bottling': {'flow_ml_s': 'ml/s', 'fill_volume_ml': 'ml', 'fill_error_ml': 'ml', 'valve_open_time_s': 's', 'tank_level_ml': 'ml', 'tank_level_pct': '%'},
    'Distributing': {'cycle_time_s': 's', 'actuator_response_time_s': 's', 'vacuum_pressure_bar': 'bar'},
    'Pick_and_Place': {'cycle_time_s': 's', 'vacuum_pressure_bar': 'bar', 'cap_torque_nm': 'Nm'},
    'Separating': {'color_confidence_pct': '%', 'cycle_time_s': 's'},
    'Sorting': {'sorting_time_s': 's', 'classification_confidence_pct': '%'},
}
ACTIVE_STATIONS = tuple(STATION_METRICS)


def read_latest_payloads():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            latest_queries = [
                '(SELECT station_name, payload_json FROM festo_telemetry WHERE station_name=%s ORDER BY id DESC LIMIT 1)'
                for _ in ACTIVE_STATIONS
            ]
            cursor.execute(' UNION ALL '.join(latest_queries), ACTIVE_STATIONS)
            return {row['station_name']: json.loads(row['payload_json']) for row in cursor.fetchall()}
    finally:
        conn.close()

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


def query_range():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = parse_datetime(request.args.get('from'), now.replace(hour=0, minute=0, second=0, microsecond=0))
    end = parse_datetime(request.args.get('to'), now)
    if start >= end:
        raise ValueError('„from” trebuie să fie înainte de „to”')
    station = request.args.get('station') or None
    if station and station not in ACTIVE_STATIONS:
        raise ValueError('modul necunoscut')
    return start, end, station


def range_where(start, end, station):
    clauses = [
        'timestamp >= %s',
        'timestamp <= %s',
        'station_name IN ({})'.format(','.join(['%s'] * len(ACTIVE_STATIONS))),
    ]
    params = [start, end, *ACTIVE_STATIONS]
    if station:
        clauses.append('station_name = %s')
        params.append(station)
    return ' AND '.join(clauses), params


def read_sampled_telemetry(start, end, station, resolution=300):
    """Return evenly distributed samples and each sample's represented row count."""
    where, params = range_where(start, end, station)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""SELECT MIN(timestamp) AS first_timestamp, MAX(timestamp) AS last_timestamp
                    FROM festo_telemetry
                    WHERE {where}""",
                params,
            )
            bounds = cursor.fetchone()
            if not bounds['first_timestamp']:
                return []
            available_span = max(
                (bounds['last_timestamp'] - bounds['first_timestamp']).total_seconds(),
                .000001,
            )
            bucket_microseconds = max(1, math.ceil(available_span * 1_000_000 / resolution))
            cursor.execute(
                f"""SELECT telemetry.timestamp, telemetry.station_name, telemetry.payload_json,
                           sampled.sample_count
                    FROM festo_telemetry telemetry
                    INNER JOIN (
                        SELECT station_name,
                               FLOOR(TIMESTAMPDIFF(MICROSECOND, %s, timestamp) / %s) AS bucket_number,
                               MAX(id) AS sample_id,
                               COUNT(*) AS sample_count
                        FROM festo_telemetry
                        WHERE {where}
                        GROUP BY station_name, bucket_number
                    ) sampled ON sampled.sample_id = telemetry.id
                    ORDER BY telemetry.timestamp ASC""",
                [bounds['first_timestamp'], bucket_microseconds, *params],
            )
            return cursor.fetchall()
    finally:
        conn.close()

@app.route('/')
def index():
    # Returnam doar scheletul HTML. Datele vor fi cerute asincron de JS.
    return render_template('index.html')

@app.route('/api/live')
def api_live():
    try:
        return jsonify(read_latest_payloads())
        
    except Exception as e:
        # Acum vom vedea eroarea clara pe ecran in format JSON
        return jsonify({"error": str(e), "tip_eroare": str(type(e))}), 500


@app.route('/api/line-summary')
def api_line_summary():
    try:
        latest = read_latest_payloads()
        latest_payload = max(latest.values(), key=lambda payload: payload.get('timestamp', '')) if latest else {}
        production = latest_payload.get('line', {}).get('production', {})
        wip = next((payload.get('line', {}).get('wip') for payload in latest.values() if payload.get('line', {}).get('wip')), {})
        alarms = sum(len(payload.get('health', {}).get('active_faults', {})) for payload in latest.values())
        stale = sum(1 for payload in latest.values() if payload.get('operational', {}).get('data_quality') not in (None, 'GOOD'))
        cycle_rates = [float(payload.get('operational', {}).get('cycle_rate_per_min', 0) or 0) for payload in latest.values()]
        timestamps = [datetime.fromisoformat(payload['timestamp'].replace('Z', '+00:00')) for payload in latest.values() if payload.get('timestamp')]
        freshest = max(timestamps) if timestamps else None
        freshness_seconds = round((datetime.now(timezone.utc) - freshest).total_seconds(), 1) if freshest else None
        return jsonify({
            'stations': len(latest),
            'production': production,
            'wip': wip,
            'active_alarms': alarms,
            'data_quality': 'DEGRADED' if stale else 'GOOD',
            'throughput_per_min': round(min(cycle_rates), 2) if cycle_rates else 0,
            'bottleneck': min(latest, key=lambda name: latest[name].get('operational', {}).get('cycle_rate_per_min', 0)) if latest else None,
            'freshness_seconds': freshness_seconds,
        })
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500


@app.route('/api/history')
def api_history():
    try:
        start, end, station = query_range()
        resolution = min(max(int(request.args.get('resolution', 240)), 20), 1000)
        rows = read_sampled_telemetry(start, end, station, resolution)

        return jsonify({
            'from': start.isoformat() + 'Z',
            'to': end.isoformat() + 'Z',
            'count': sum(row['sample_count'] for row in rows),
            'sampled_count': len(rows),
            'resolution': resolution,
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


@app.route('/api/summary')
def api_summary():
    try:
        start, end, station = query_range()
        sampled_rows = read_sampled_telemetry(start, end, station, resolution=720)
        where, params = range_where(start, end, station)

        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT timestamp, station_name, payload_json
                        FROM festo_telemetry
                        WHERE {where}
                          AND COALESCE(JSON_LENGTH(JSON_EXTRACT(payload_json, '$.events')), 0) > 0
                        ORDER BY timestamp DESC
                        LIMIT 50""",
                    params,
                )
                event_rows = cursor.fetchall()
        finally:
            conn.close()

        def counter_delta(values):
            values = [float(value) for value in values if isinstance(value, (int, float))]
            if len(values) < 2:
                return 0.0
            total = 0.0
            previous = values[0]
            for current in values[1:]:
                total += current - previous if current >= previous else current
                previous = current
            return max(0, total)

        summaries = {}
        payloads_by_station = {}
        for row in sampled_rows:
            payload = json.loads(row['payload_json'])
            name = row['station_name']
            weight = int(row['sample_count'])
            payloads_by_station.setdefault(name, []).append(payload)
            summary = summaries.setdefault(name, {
                'station': name,
                'samples': 0,
                'first_seen': row['timestamp'].isoformat() + 'Z',
                'last_seen': row['timestamp'].isoformat() + 'Z',
                'latest': payload,
                'health': {'current_degradation': 0, 'average_degradation': 0, 'peak_degradation': 0, 'change_in_range': 0},
                'operational': {
                    'cycles_in_range': 0,
                    'average_cycle_rate_per_min': 0,
                    'average_availability_pct': 0,
                    'peak_fault_count': 0,
                },
                '_rate_weight': 0,
                '_availability_weight': 0,
                '_first_degradation': None,
            })
            summary['samples'] += weight
            summary['last_seen'] = row['timestamp'].isoformat() + 'Z'
            summary['latest'] = payload

            health = payload.get('health') or {}
            score = float(health.get('degradation_score', 0) or 0)
            summary['health']['average_degradation'] += score * weight
            summary['health']['peak_degradation'] = max(summary['health']['peak_degradation'], score)
            if summary['_first_degradation'] is None:
                summary['_first_degradation'] = score
            summary['health']['current_degradation'] = score

            operational = payload.get('operational') or {}
            rate = operational.get('cycle_rate_per_min')
            if isinstance(rate, (int, float)):
                summary['operational']['average_cycle_rate_per_min'] += float(rate) * weight
                summary['_rate_weight'] += weight
            availability = operational.get('availability_pct')
            if isinstance(availability, (int, float)):
                summary['operational']['average_availability_pct'] += float(availability) * weight
                summary['_availability_weight'] += weight
            summary['operational']['peak_fault_count'] = max(
                summary['operational']['peak_fault_count'],
                int(operational.get('fault_count', 0) or 0),
            )

        for name, summary in summaries.items():
            station_payloads = payloads_by_station[name]
            summary['health']['average_degradation'] = round(
                summary['health']['average_degradation'] / summary['samples'], 4
            )
            summary['health']['peak_degradation'] = round(summary['health']['peak_degradation'], 4)
            rate_weight = summary.pop('_rate_weight')
            first_degradation = summary.pop('_first_degradation')
            summary['health']['current_degradation'] = round(summary['health']['current_degradation'], 4)
            summary['health']['change_in_range'] = round(
                summary['health']['current_degradation'] - (first_degradation or 0), 4)
            availability_weight = summary.pop('_availability_weight')
            summary['operational']['average_cycle_rate_per_min'] = round(
                summary['operational']['average_cycle_rate_per_min'] / rate_weight, 2
            ) if rate_weight else 0
            summary['operational']['average_availability_pct'] = round(
                summary['operational']['average_availability_pct'] / availability_weight, 2
            ) if availability_weight else 0
            summary['operational']['cycles_in_range'] = round(counter_delta([
                payload.get('operational', {}).get('cycle_count')
                for payload in station_payloads
            ]))

            state_deltas = {
                state: counter_delta([
                    payload.get('operational', {}).get('state_seconds', {}).get(state)
                    for payload in station_payloads
                ])
                for state in ('RUN', 'DEGRADED', 'FAULT', 'FAILURE', 'MAINTENANCE')
            }
            planned = sum(state_deltas[state] for state in ('RUN', 'DEGRADED', 'FAULT', 'FAILURE'))
            downtime = state_deltas['FAULT'] + state_deltas['FAILURE']
            if planned:
                summary['operational']['average_availability_pct'] = round(
                    max(0.0, (planned - downtime) / planned * 100.0), 2
                )

        line_payloads = payloads_by_station[sorted(payloads_by_station)[0]] if payloads_by_station else []
        production_good = round(counter_delta([
            payload.get('line', {}).get('production', {}).get('good')
            for payload in line_payloads
        ]))
        production_rejects = round(counter_delta([
            payload.get('line', {}).get('production', {}).get('rejects')
            for payload in line_payloads
        ]))
        production_diverted = round(counter_delta([
            payload.get('line', {}).get('production', {}).get('diverted')
            for payload in line_payloads
        ]))
        production_total = production_good + production_rejects
        latest = {name: summary['latest'] for name, summary in summaries.items()}
        freshest_payload = max(latest.values(), key=lambda payload: payload.get('timestamp', '')) if latest else {}
        average_rates = {
            name: summary['operational']['average_cycle_rate_per_min']
            for name, summary in summaries.items()
        }
        first_timestamp = min((row['timestamp'] for row in sampled_rows), default=None)
        last_timestamp = max((row['timestamp'] for row in sampled_rows), default=None)
        observed_minutes = max((last_timestamp - first_timestamp).total_seconds() / 60.0, 1 / 60) if first_timestamp else 0
        output_station = 'Sorting' if 'Sorting' in summaries else None
        output_station = output_station or (min(summaries, key=lambda name: summaries[name]['operational']['cycles_in_range']) if summaries else None)
        throughput_cycles = summaries[output_station]['operational']['cycles_in_range'] if output_station else 0
        throughput = round(throughput_cycles / observed_minutes, 2) if observed_minutes else 0
        availability_pct = min(
            (summary['operational']['average_availability_pct'] for summary in summaries.values()),
            default=0,
        )
        ideal_line_rate = 18.0
        performance_pct = min(100.0, throughput / ideal_line_rate * 100.0)
        quality_pct = production_good / production_total * 100.0 if production_total else 0
        oee_pct = availability_pct / 100.0 * performance_pct / 100.0 * quality_pct / 100.0 * 100.0
        freshest_timestamp = max((row['timestamp'] for row in sampled_rows), default=None)
        reference_time = min(end, datetime.now(timezone.utc).replace(tzinfo=None))
        freshness_seconds = max(0, round((reference_time - freshest_timestamp).total_seconds(), 1)) if freshest_timestamp else None
        active_alarms = sum(len(payload.get('health', {}).get('active_faults') or {}) for payload in latest.values())

        events = []
        for row in event_rows:
            payload = json.loads(row['payload_json'])
            event_time = row['timestamp'].isoformat() + 'Z'
            for event in payload.get('events') or []:
                events.append({'station': row['station_name'], 'timestamp': event_time, **event})

        worst_current = max((summary['health']['current_degradation'] for summary in summaries.values()), default=0)
        line_state = (
            'FĂRĂ DATE' if not summaries
            else 'CRITICAL' if worst_current >= .82
            else 'DEGRADED' if worst_current >= .45
            else 'ONLINE'
        )
        result = {
            'from': start.isoformat() + 'Z',
            'to': end.isoformat() + 'Z',
            'count': sum(row['sample_count'] for row in sampled_rows),
            'stations': summaries,
            'line': {
                'state': line_state,
                'production': {
                    'good': production_good,
                    'rejects': production_rejects,
                    'total': production_total,
                    'diverted': production_diverted,
                    'quality_rate': round(production_good / production_total, 4) if production_total else None,
                },
                'wip': freshest_payload.get('line', {}).get('wip', {}),
                'active_alarms': active_alarms,
                'throughput_per_min': throughput,
                'bottleneck': min(average_rates, key=average_rates.get) if average_rates else None,
                'freshness_seconds': freshness_seconds,
                'availability_pct': round(availability_pct, 2),
                'performance_pct': round(performance_pct, 2),
                'quality_pct': round(quality_pct, 2),
                'oee_pct': round(oee_pct, 2),
                'operational_state': freshest_payload.get('line', {}).get('operational', {}).get('state'),
            },
            'events': events[:50],
        }
        return jsonify(result)
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Parametri invalizi: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
