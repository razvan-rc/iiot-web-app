from flask import Flask, render_template, jsonify, request
import pymysql
import json
import math
import os
import hmac
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

DB_READER_HOST = os.getenv('DB_READER_HOST', '172.31.45.5')
DB_WRITER_HOST = os.getenv('DB_WRITER_HOST', '172.31.32.65')
DB_USER = os.getenv('DB_USER', 'sensor_app')
DB_PASSWORD = os.getenv('DB_PASSWORD', 'SenzorPass123!')
DB_NAME = os.getenv('DB_NAME', 'industrial_db')
MAINTENANCE_CONTROL_TOKEN = os.getenv('MAINTENANCE_CONTROL_TOKEN', '')

MAINTENANCE_POLICIES = {
    'Distributing': {'action': 'Inspectează cilindrul, presiunea pneumatică și capul vacuum.', 'duration_minutes': 15,
                     'impact': 'Alimentarea se oprește; modulele din aval consumă temporar WIP-ul existent.'},
    'Separating': {'action': 'Curăță și calibrează senzorul de culoare; verifică mecanismul de deviere.', 'duration_minutes': 20,
                   'impact': 'Fluxul de intrare este blocat, iar modulele din aval pot rămâne fără recipiente.'},
    'Bottling': {'action': 'Inspectează vana, debitul și opritorul; verifică etanșarea circuitului.', 'duration_minutes': 25,
                 'impact': 'Umplerea este indisponibilă; se recomandă golirea controlată a bufferelor.'},
    'Pick_and_Place': {'action': 'Verifică vacuumul, axele pneumatice și cuplul capului de înfiletare.', 'duration_minutes': 25,
                       'impact': 'Aplicarea capacelor se oprește; Bottling va fi blocat după umplerea bufferului.'},
    'Sorting': {'action': 'Curăță și calibrează senzorii de material/culoare; inspectează opritoarele.', 'duration_minutes': 20,
                'impact': 'Evacuarea finală se oprește, iar linia va fi blocată progresiv din aval.'},
}


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

def get_db_connection(write=False):
    # Telemetria și istoricul se citesc din replică; comenzile se scriu numai pe master.
    return pymysql.connect(
        host=DB_WRITER_HOST if write else DB_READER_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=write,
    )


def maintenance_projection(name, summary, points):
    latest = summary.get('latest') or {}
    current = float(summary.get('health', {}).get('current_degradation') or 0)
    state = latest.get('health', {}).get('state') or latest.get('state') or 'UNKNOWN'
    faults = list((latest.get('health', {}).get('active_faults') or {}).keys())
    components = latest.get('health', {}).get('components') or {}
    component = min(components, key=components.get) if components else 'station'
    policy = MAINTENANCE_POLICIES[name]
    segment_start = 0
    for index in range(1, len(points)):
        profile_changed = points[index][2:] != points[index - 1][2:]
        if profile_changed or points[index][1] < points[index - 1][1] - .03:
            segment_start = index
    segment = points[segment_start:]
    slope = None
    r_squared = None
    span_hours = 0.0
    if len(segment) >= 6:
        origin = segment[0][0]
        xs = [(point[0] - origin).total_seconds() / 3600.0 for point in segment]
        ys = [point[1] for point in segment]
        span_hours = xs[-1] - xs[0]
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denominator = sum((value - x_mean) ** 2 for value in xs)
        if denominator > 0 and span_hours >= 5 / 60:
            slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
            predicted = [y_mean + slope * (x - x_mean) for x in xs]
            residual = sum((actual - estimate) ** 2 for actual, estimate in zip(ys, predicted))
            total = sum((actual - y_mean) ** 2 for actual in ys)
            r_squared = max(0.0, min(1.0, 1.0 - residual / total)) if total > 1e-12 else 1.0

    target = .45 if current < .45 else (.82 if current < .82 else .90)
    reliable_slope = slope is not None and slope > .0001 and r_squared is not None and r_squared >= .25
    rul_hours = max(0.0, (target - current) / slope) if reliable_slope else None
    if rul_hours is not None:
        rul_hours = min(rul_hours, 9999.0)
    requires_stop = state in ('FAULT', 'FAILURE') or current >= .90
    if requires_stop:
        priority, window = 'CRITICAL', 'Acum — oprire controlată a modulului'
    elif current >= .65 or faults or (rul_hours is not None and rul_hours <= 4):
        priority = 'HIGH'
        window = 'La următoarea oprire planificată' if rul_hours is None else f'În maximum {max(1, math.ceil(rul_hours))} h'
    elif current >= .45 or (rul_hours is not None and rul_hours <= 24):
        priority, window = 'MEDIUM', 'Planifică intervenția în următoarea fereastră de producție'
    elif rul_hours is not None and rul_hours <= 168:
        priority, window = 'MEDIUM', f'În aproximativ {max(1, math.ceil(rul_hours / 24))} zile'
    else:
        priority, window = 'LOW', 'Monitorizare; fără intervenție în următoarele 7 zile'
    if slope is None:
        confidence = 'INSUFFICIENT_DATA'
    elif r_squared >= .75 and span_hours >= .5:
        confidence = 'HIGH'
    elif r_squared >= .4:
        confidence = 'MEDIUM'
    else:
        confidence = 'LOW'
    evidence = (
        'Sunt necesare cel puțin 5 minute și 6 probe pentru estimarea trendului.'
        if slope is None else
        f'Trend {slope * 100:+.2f} pp/oră pe {span_hours:.1f} h; R²={r_squared:.2f}. Degradare curentă {current * 100:.1f}%.'
    )
    return {
        'station': name, 'component': component, 'action': policy['action'],
        'duration_minutes': policy['duration_minutes'], 'production_impact': policy['impact'],
        'priority': priority, 'recommended_window': window, 'requires_stop': requires_stop,
        'current_degradation': round(current, 4), 'threshold': target,
        'slope_per_hour': round(slope, 6) if slope is not None else None,
        'rul_hours': round(rul_hours, 1) if rul_hours is not None else None,
        'confidence': confidence, 'evidence': evidence, 'faults': faults,
    }


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
        degradation_points = {}
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
            degradation_points.setdefault(name, []).append((
                row['timestamp'], score,
                health.get('wear_cycle_hours'),
                bool(health.get('demo_mode')),
            ))
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
        maintenance = sorted(
            (maintenance_projection(name, summary, degradation_points.get(name, [])) for name, summary in summaries.items()),
            key=lambda item: ({'CRITICAL': 3, 'HIGH': 2, 'MEDIUM': 1, 'LOW': 0}[item['priority']], item['current_degradation']),
            reverse=True,
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
            'maintenance': maintenance,
            'events': events[:50],
        }
        return jsonify(result)
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Parametri invalizi: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500

@app.route('/api/maintenance', methods=['GET', 'POST'])
def api_maintenance():
    try:
        if request.method == 'GET':
            limit = min(max(int(request.args.get('limit', 30)), 1), 100)
            conn = get_db_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """SELECT id, created_at, station_name, action_type, component, status,
                                  requested_by, notes, applied_at, parameters_json, result_json
                           FROM maintenance_commands ORDER BY id DESC LIMIT %s""",
                        (limit,),
                    )
                    rows = cursor.fetchall()
            finally:
                conn.close()
            for row in rows:
                for field in ('created_at', 'applied_at'):
                    if row[field]:
                        row[field] = row[field].isoformat() + 'Z'
                for field in ('parameters_json', 'result_json'):
                    if isinstance(row[field], str):
                        row[field] = json.loads(row[field])
            return jsonify({'commands': rows})

        if not MAINTENANCE_CONTROL_TOKEN:
            return jsonify({'error': 'Comenzile de mentenanță nu sunt configurate pe server.'}), 503
        provided_token = request.headers.get('X-Control-Token', '')
        if not hmac.compare_digest(provided_token, MAINTENANCE_CONTROL_TOKEN):
            return jsonify({'error': 'Codul de operator este invalid.'}), 403

        payload = request.get_json(silent=True) or {}
        public_action = payload.get('action')
        actions = {
            'accelerate_demo': 'DEMO_ACCELERATE',
            'stop_demo': 'DEMO_RESET',
            'perform_maintenance': 'PERFORM_MAINTENANCE',
        }
        if public_action not in actions:
            raise ValueError('acțiune necunoscută')
        station = payload.get('station')
        if station not in ACTIVE_STATIONS:
            raise ValueError('modul necunoscut')
        component = str(payload.get('component') or 'station')[:64]
        notes = str(payload.get('notes') or '')[:500]
        parameters = {}
        if public_action == 'accelerate_demo':
            parameters = {
                'target': min(max(float(payload.get('target', .68)), .45), .78),
                'duration_seconds': min(max(int(payload.get('duration_seconds', 600)), 60), 900),
            }
        elif public_action == 'perform_maintenance':
            parameters = {'hold_seconds': 12}

        conn = get_db_connection(write=True)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO maintenance_commands
                       (station_name, action_type, component, status, requested_by, notes, parameters_json)
                       VALUES (%s, %s, %s, 'PENDING', 'dashboard', %s, %s)""",
                    (station, actions[public_action], component, notes, json.dumps(parameters)),
                )
                command_id = cursor.lastrowid
        finally:
            conn.close()
        return jsonify({'id': command_id, 'status': 'PENDING'}), 202
    except (ValueError, TypeError) as e:
        return jsonify({'error': f'Cerere invalidă: {e}'}), 400
    except Exception as e:
        return jsonify({'error': str(e), 'tip_eroare': str(type(e))}), 500


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0')
