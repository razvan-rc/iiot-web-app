from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

import pymysql


@dataclass(frozen=True)
class DatabaseConfig:
    reader_host: str
    writer_host: str
    user: str
    password: str
    name: str
    connect_timeout: int = 5
    read_timeout: int = 20
    write_timeout: int = 10

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        values = os.environ if environment is None else environment
        return cls(
            reader_host=values.get('DB_READER_HOST', '172.31.45.5'),
            writer_host=values.get('DB_WRITER_HOST', '172.31.32.65'),
            user=values.get('DB_USER', 'sensor_app'),
            password=values.get('DB_PASSWORD', 'SenzorPass123!'),
            name=values.get('DB_NAME', 'industrial_db'),
        )


DATABASE_CONFIG = DatabaseConfig.from_environment()


def get_db_connection(write: bool = False, config: DatabaseConfig | None = None):
    """Open a primary connection for writes or a replica connection for reads."""
    selected = config or DATABASE_CONFIG
    return pymysql.connect(
        host=selected.writer_host if write else selected.reader_host,
        user=selected.user,
        password=selected.password,
        database=selected.name,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=write,
        connect_timeout=selected.connect_timeout,
        read_timeout=selected.read_timeout,
        write_timeout=selected.write_timeout,
        init_command='SET SESSION max_statement_time=15',
    )
