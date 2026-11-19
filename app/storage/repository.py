# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SQLite + optional GCS snapshot repository for airline disruptions and recovery."""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core.config import get_settings

logger = logging.getLogger("skycare.storage")


class FlightRecord(BaseModel):
    """Scheduled or alternate flight segment."""

    flight_number: str
    origin: str
    destination: str
    departure_utc: str
    arrival_utc: str
    status: str
    delay_minutes: int = 0
    disruption_cause: str = "NONE"
    jurisdiction: str = "US_DOT"
    distance_km: int = 1200
    economy_seats: int = 0
    business_seats: int = 0
    base_fare_diff_usd: float = 0.0
    carrier: str = "SkyCare Airlines"


class PassengerReservation(BaseModel):
    """Passenger PNR record and disruption state."""

    pnr: str
    passenger_name: str
    loyalty_tier: str
    loyalty_id: str
    cabin_class: str
    origin: str
    destination: str
    segment_flights: list[str]
    disrupted_flight: str | None = None
    disruption_summary: str = ""
    preferences: dict[str, Any] = Field(default_factory=dict)
    rebooking_status: str = "NEEDS_RECOVERY"


class RebookingRecord(BaseModel):
    """Confirmed or pending passenger itinerary change."""

    rebooking_id: str
    pnr: str
    new_flights: list[str]
    cabin_class: str
    seat_assignment: str
    fare_difference_usd: float
    fare_waived: bool
    status: str
    created_at: str


class VoucherRecord(BaseModel):
    """Hotel, meal, or ground transport duty-of-care voucher."""

    voucher_id: str
    pnr: str
    voucher_type: str
    amount_usd: float
    currency: str = "USD"
    vendor_name: str
    status: str
    created_at: str


class ApprovalRecord(BaseModel):
    """Human-in-the-loop supervisor approval request for gated mutations."""

    approval_id: str
    pnr: str
    action_type: str
    summary: str
    payload: dict[str, Any]
    estimated_cost_usd: float = 0.0
    status: str = "PENDING"
    reviewer: str | None = None
    decision_reason: str | None = None
    created_at: str
    resolved_at: str | None = None


_SEED_FLIGHTS: list[tuple[Any, ...]] = [
    (
        "SC402",
        "JFK",
        "LHR",
        "2026-11-19T19:30:00Z",
        "2026-11-20T07:15:00Z",
        "CANCELLED",
        0,
        "CREW_TIMEOUT_WEATHER",
        "UK_EU261",
        5540,
        0,
        0,
        0.0,
        "SkyCare Airlines",
    ),
    (
        "SC810",
        "LHR",
        "ZRH",
        "2026-11-20T09:10:00Z",
        "2026-11-20T12:00:00Z",
        "ON_TIME",
        0,
        "NONE",
        "UK_EU261",
        780,
        18,
        4,
        0.0,
        "SkyCare Airlines",
    ),
    (
        "SC408",
        "JFK",
        "ZRH",
        "2026-11-19T21:40:00Z",
        "2026-11-20T11:25:00Z",
        "ON_TIME",
        0,
        "NONE",
        "UK_EU261",
        6310,
        4,
        1,
        580.0,
        "SkyCare Transatlantic",
    ),
    (
        "SC404",
        "JFK",
        "LHR",
        "2026-11-19T22:15:00Z",
        "2026-11-20T10:05:00Z",
        "ON_TIME",
        0,
        "NONE",
        "UK_EU261",
        5540,
        9,
        3,
        420.0,
        "SkyCare Airlines",
    ),
    (
        "SC814",
        "LHR",
        "ZRH",
        "2026-11-20T12:30:00Z",
        "2026-11-20T15:20:00Z",
        "ON_TIME",
        0,
        "NONE",
        "UK_EU261",
        780,
        14,
        5,
        0.0,
        "SkyCare Airlines",
    ),
    (
        "SC410",
        "JFK",
        "CDG",
        "2026-11-19T23:00:00Z",
        "2026-11-20T12:10:00Z",
        "ON_TIME",
        0,
        "NONE",
        "EU261",
        5835,
        16,
        6,
        260.0,
        "SkyCare Partner Alliance",
    ),
    (
        "SC822",
        "CDG",
        "ZRH",
        "2026-11-20T13:45:00Z",
        "2026-11-20T15:05:00Z",
        "ON_TIME",
        0,
        "NONE",
        "EU261",
        480,
        11,
        2,
        0.0,
        "SkyCare Partner Alliance",
    ),
    (
        "SC118",
        "ORD",
        "SFO",
        "2026-11-19T08:00:00Z",
        "2026-11-19T14:05:00Z",
        "DELAYED",
        200,
        "MECHANICAL_MAINTENANCE",
        "US_DOT",
        2960,
        0,
        0,
        0.0,
        "SkyCare Airlines",
    ),
    (
        "SC705",
        "SFO",
        "NRT",
        "2026-11-19T12:15:00Z",
        "2026-11-20T16:30:00Z",
        "ON_TIME",
        0,
        "NONE",
        "US_DOT",
        8220,
        0,
        0,
        0.0,
        "SkyCare Pacific",
    ),
    (
        "SC709",
        "SFO",
        "NRT",
        "2026-11-20T11:45:00Z",
        "2026-11-21T15:50:00Z",
        "ON_TIME",
        0,
        "NONE",
        "US_DOT",
        8220,
        11,
        4,
        310.0,
        "SkyCare Pacific",
    ),
    (
        "SC715",
        "ORD",
        "NRT",
        "2026-11-19T13:45:00Z",
        "2026-11-20T17:10:00Z",
        "ON_TIME",
        0,
        "NONE",
        "US_DOT",
        10080,
        2,
        0,
        490.0,
        "SkyCare Pacific",
    ),
    (
        "SC214",
        "ATL",
        "BOS",
        "2026-11-19T13:00:00Z",
        "2026-11-19T16:40:00Z",
        "DELAYED",
        75,
        "ATC_WEATHER",
        "US_DOT",
        1520,
        0,
        0,
        0.0,
        "SkyCare Airlines",
    ),
    (
        "SC218",
        "ATL",
        "BOS",
        "2026-11-19T14:10:00Z",
        "2026-11-19T16:35:00Z",
        "ON_TIME",
        0,
        "NONE",
        "US_DOT",
        1520,
        18,
        4,
        95.0,
        "SkyCare Airlines",
    ),
    (
        "SC510",
        "LAX",
        "SEA",
        "2026-11-22T15:00:00Z",
        "2026-11-22T17:50:00Z",
        "ON_TIME",
        0,
        "NONE",
        "US_DOT",
        1540,
        20,
        6,
        0.0,
        "SkyCare Airlines",
    ),
]

_SEED_RESERVATIONS: list[tuple[Any, ...]] = [
    (
        "SKY842",
        "Elena Rostova",
        "Gold",
        "SK-991042",
        "Economy",
        "JFK",
        "ZRH",
        json.dumps(["SC402", "SC810"]),
        "SC402",
        "SC402 (JFK-LHR) cancelled due to severe Atlantic storm and crew duty timeout, stranding connection SC810 to Zurich.",
        json.dumps(
            {
                "seat": "aisle",
                "meal": "vegetarian",
                "avoid_tight_connections": True,
                "notes": "Requires arrival in Zurich before 16:00Z for conference.",
            }
        ),
        "NEEDS_RECOVERY",
    ),
    (
        "SKY319",
        "Marcus Vance",
        "Silver",
        "SK-442910",
        "Economy",
        "ORD",
        "NRT",
        json.dumps(["SC118", "SC705"]),
        "SC118",
        "SC118 (ORD-SFO) delayed 200 minutes for mechanical APU valve replacement, causing misconnect with SC705 to Tokyo Narita.",
        json.dumps({"seat": "window", "meal": "standard"}),
        "NEEDS_RECOVERY",
    ),
    (
        "SKY905",
        "Priya Nair",
        "Platinum",
        "SK-778321",
        "Business",
        "ATL",
        "BOS",
        json.dumps(["SC214"]),
        "SC214",
        "SC214 (ATL-BOS) delayed 75 minutes by Atlanta ATC weather ground program.",
        json.dumps({"seat": "aisle", "meal": "vegan", "priority_direct": True}),
        "NEEDS_RECOVERY",
    ),
    (
        "SKY404",
        "David Kim",
        "Blue",
        "SK-110284",
        "Economy",
        "LAX",
        "SEA",
        json.dumps(["SC510"]),
        None,
        "No operational disruption; SC510 is scheduled on time.",
        json.dumps({"seat": "aisle", "meal": "standard"}),
        "ON_SCHEDULE",
    ),
]


class ReservationRepository:
    """Thread-safe SQLite repository with optional GCS snapshot backup."""

    def __init__(self, db_path: Path | str, *, gcs_bucket: str | None = None) -> None:
        self._db_path = Path(db_path)
        self._gcs_bucket = gcs_bucket
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._restore_from_gcs_if_missing()
        self._init_schema()
        self._ensure_seeded()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _restore_from_gcs_if_missing(self) -> None:
        settings = get_settings()
        if (
            self._db_path.exists()
            or not self._gcs_bucket
            or settings.dry_run
            or settings.use_in_memory_session
        ):
            return
        try:
            from google.cloud import storage

            client = storage.Client()
            blob = client.bucket(self._gcs_bucket).blob("state/skycare.db")
            if blob.exists():
                blob.download_to_filename(str(self._db_path))
                logger.info("Restored operational SQLite snapshot from GCS.")
        except Exception as exc:  # pragma: no cover - best-effort cloud restore
            logger.warning("Skipping GCS state restore: %s", exc)

    def sync_to_gcs(self) -> bool:
        """Persist a snapshot of the SQLite DB to GCS when configured."""
        settings = get_settings()
        if not self._gcs_bucket or settings.dry_run or settings.use_in_memory_session:
            return False
        try:
            from google.cloud import storage

            client = storage.Client()
            blob = client.bucket(self._gcs_bucket).blob("state/skycare.db")
            blob.upload_from_filename(str(self._db_path))
            return True
        except Exception as exc:  # pragma: no cover - best-effort cloud backup
            logger.warning("Skipping GCS state backup: %s", exc)
            return False

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS flights (
                    flight_number TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    departure_utc TEXT NOT NULL,
                    arrival_utc TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delay_minutes INTEGER NOT NULL,
                    disruption_cause TEXT NOT NULL,
                    jurisdiction TEXT NOT NULL,
                    distance_km INTEGER NOT NULL,
                    economy_seats INTEGER NOT NULL,
                    business_seats INTEGER NOT NULL,
                    base_fare_diff_usd REAL NOT NULL,
                    carrier TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reservations (
                    pnr TEXT PRIMARY KEY,
                    passenger_name TEXT NOT NULL,
                    loyalty_tier TEXT NOT NULL,
                    loyalty_id TEXT NOT NULL,
                    cabin_class TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    segment_flights TEXT NOT NULL,
                    disrupted_flight TEXT,
                    disruption_summary TEXT NOT NULL,
                    preferences TEXT NOT NULL,
                    rebooking_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rebookings (
                    rebooking_id TEXT PRIMARY KEY,
                    pnr TEXT NOT NULL,
                    new_flights TEXT NOT NULL,
                    cabin_class TEXT NOT NULL,
                    seat_assignment TEXT NOT NULL,
                    fare_difference_usd REAL NOT NULL,
                    fare_waived INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vouchers (
                    voucher_id TEXT PRIMARY KEY,
                    pnr TEXT NOT NULL,
                    voucher_type TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    currency TEXT NOT NULL,
                    vendor_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    pnr TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    status TEXT NOT NULL,
                    reviewer TEXT,
                    decision_reason TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                """
            )

    @staticmethod
    def _insert_seed_data(conn: sqlite3.Connection) -> None:
        conn.executemany(
            """
            INSERT OR IGNORE INTO flights VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _SEED_FLIGHTS,
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO reservations VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _SEED_RESERVATIONS,
        )

    def _ensure_seeded(self) -> None:
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
            if count > 0:
                return
            self._insert_seed_data(conn)

    def reset_demo_data(self) -> None:
        """Clear mutable operational tables and restore seeded demo flights and PNRs."""
        with self._lock, self._connect() as conn:
            for statement in (
                "DELETE FROM approvals",
                "DELETE FROM rebookings",
                "DELETE FROM vouchers",
                "DELETE FROM reservations",
                "DELETE FROM flights",
            ):
                conn.execute(statement)
            self._insert_seed_data(conn)
        self.sync_to_gcs()

    @staticmethod
    def _row_to_flight(row: sqlite3.Row) -> FlightRecord:
        return FlightRecord(**dict(row))

    @staticmethod
    def _row_to_reservation(row: sqlite3.Row) -> PassengerReservation:
        data = dict(row)
        data["segment_flights"] = json.loads(data["segment_flights"])
        data["preferences"] = json.loads(data["preferences"])
        return PassengerReservation(**data)

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        return ApprovalRecord(**data)

    def get_flight(self, flight_number: str) -> FlightRecord | None:
        code = flight_number.strip().upper()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM flights WHERE flight_number = ?", (code,)
            ).fetchone()
            return self._row_to_flight(row) if row else None

    def get_reservation_by_pnr(
        self, pnr: str
    ) -> tuple[PassengerReservation | None, list[FlightRecord]]:
        code = pnr.strip().upper()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE pnr = ?", (code,)
            ).fetchone()
            if not row:
                return None, []
            res = self._row_to_reservation(row)
            segments: list[FlightRecord] = []
            for fn in res.segment_flights:
                f_row = conn.execute(
                    "SELECT * FROM flights WHERE flight_number = ?", (fn,)
                ).fetchone()
                if f_row:
                    segments.append(self._row_to_flight(f_row))
            return res, segments

    def list_disruptions(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM reservations ORDER BY pnr").fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                res = self._row_to_reservation(row)
                vouchers = conn.execute(
                    "SELECT * FROM vouchers WHERE pnr = ? ORDER BY created_at DESC",
                    (res.pnr,),
                ).fetchall()
                rebookings = conn.execute(
                    "SELECT * FROM rebookings WHERE pnr = ? ORDER BY created_at DESC",
                    (res.pnr,),
                ).fetchall()
                out.append(
                    {
                        **res.model_dump(),
                        "vouchers": [dict(v) for v in vouchers],
                        "rebookings": [
                            {**dict(r), "new_flights": json.loads(r["new_flights"])}
                            for r in rebookings
                        ],
                    }
                )
            return out

    def search_alternate_itineraries(
        self, origin: str, destination: str, cabin_class: str = "Economy"
    ) -> list[dict[str, Any]]:
        """Query on-time direct and one-stop alternate itineraries matching cabin availability.

        Args:
            origin: 3-letter IATA departure airport code.
            destination: 3-letter IATA arrival airport code.
            cabin_class: Target cabin class (``Economy`` or ``Business``).

        Returns:
            List of candidate nonstop and one-stop itinerary dictionaries sorted by departure.
        """
        orig = origin.strip().upper()
        dest = destination.strip().upper()
        cabin = cabin_class.strip().lower()
        seat_col = "business_seats" if "bus" in cabin else "economy_seats"
        with self._lock, self._connect() as conn:
            direct = conn.execute(
                f"""
                SELECT * FROM flights
                WHERE origin = ? AND destination = ? AND status = 'ON_TIME' AND {seat_col} > 0
                ORDER BY departure_utc ASC
                """,
                (orig, dest),
            ).fetchall()
            options: list[dict[str, Any]] = []
            for r in direct:
                f = self._row_to_flight(r)
                options.append(
                    {
                        "itinerary_id": f"{f.flight_number}-NONSTOP",
                        "legs": [f.model_dump()],
                        "stops": 0,
                        "departure_utc": f.departure_utc,
                        "arrival_utc": f.arrival_utc,
                        "fare_difference_usd": f.base_fare_diff_usd,
                        "available_seats": (
                            f.business_seats if "bus" in cabin else f.economy_seats
                        ),
                    }
                )

            # One-stop connecting paths
            first_legs = conn.execute(
                f"""
                SELECT * FROM flights
                WHERE origin = ? AND destination != ? AND status = 'ON_TIME' AND {seat_col} > 0
                """,
                (orig, dest),
            ).fetchall()
            for r1 in first_legs:
                f1 = self._row_to_flight(r1)
                second_legs = conn.execute(
                    f"""
                    SELECT * FROM flights
                    WHERE origin = ? AND destination = ? AND status = 'ON_TIME'
                      AND departure_utc > ? AND {seat_col} > 0
                    ORDER BY departure_utc ASC
                    """,
                    (f1.destination, dest, f1.arrival_utc),
                ).fetchall()
                for r2 in second_legs:
                    f2 = self._row_to_flight(r2)
                    seats = min(
                        f1.business_seats if "bus" in cabin else f1.economy_seats,
                        f2.business_seats if "bus" in cabin else f2.economy_seats,
                    )
                    options.append(
                        {
                            "itinerary_id": f"{f1.flight_number}-{f2.flight_number}",
                            "legs": [f1.model_dump(), f2.model_dump()],
                            "stops": 1,
                            "departure_utc": f1.departure_utc,
                            "arrival_utc": f2.arrival_utc,
                            "fare_difference_usd": round(
                                f1.base_fare_diff_usd + f2.base_fare_diff_usd, 2
                            ),
                            "available_seats": seats,
                        }
                    )
            return options

    def record_rebooking(
        self,
        *,
        pnr: str,
        new_flights: list[str],
        cabin_class: str,
        seat_assignment: str,
        fare_difference_usd: float,
        fare_waived: bool = True,
    ) -> RebookingRecord:
        code = pnr.strip().upper()
        flight_codes = [f.strip().upper() for f in new_flights]
        seat_col = "business_seats" if "bus" in cabin_class.lower() else "economy_seats"
        rebooking_id = f"RBK-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(UTC).isoformat()

        with self._lock, self._connect() as conn:
            res_row = conn.execute(
                "SELECT pnr FROM reservations WHERE pnr = ?", (code,)
            ).fetchone()
            if not res_row:
                raise ValueError(f"Unknown PNR '{code}'")

            for fn in flight_codes:
                f_row = conn.execute(
                    f"SELECT {seat_col} FROM flights WHERE flight_number = ?", (fn,)
                ).fetchone()
                if not f_row:
                    raise ValueError(f"Flight '{fn}' not found in schedule")
                if f_row[seat_col] <= 0:
                    raise ValueError(
                        f"Flight '{fn}' has no seats left in {cabin_class}"
                    )

            for fn in flight_codes:
                conn.execute(
                    f"UPDATE flights SET {seat_col} = {seat_col} - 1 WHERE flight_number = ?",
                    (fn,),
                )

            conn.execute(
                """
                UPDATE reservations
                SET segment_flights = ?, rebooking_status = 'REBOOKED', cabin_class = ?
                WHERE pnr = ?
                """,
                (json.dumps(flight_codes), cabin_class, code),
            )
            conn.execute(
                """
                INSERT INTO rebookings VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?)
                """,
                (
                    rebooking_id,
                    code,
                    json.dumps(flight_codes),
                    cabin_class,
                    seat_assignment,
                    fare_difference_usd,
                    1 if fare_waived else 0,
                    now,
                ),
            )
        self.sync_to_gcs()
        return RebookingRecord(
            rebooking_id=rebooking_id,
            pnr=code,
            new_flights=flight_codes,
            cabin_class=cabin_class,
            seat_assignment=seat_assignment,
            fare_difference_usd=fare_difference_usd,
            fare_waived=fare_waived,
            status="CONFIRMED",
            created_at=now,
        )

    def record_voucher(
        self,
        *,
        pnr: str,
        voucher_type: str,
        amount_usd: float,
        vendor_name: str,
    ) -> VoucherRecord:
        code = pnr.strip().upper()
        voucher_id = f"VCH-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as conn:
            res_row = conn.execute(
                "SELECT pnr FROM reservations WHERE pnr = ?", (code,)
            ).fetchone()
            if not res_row:
                raise ValueError(f"Unknown PNR '{code}'")
            conn.execute(
                """
                INSERT INTO vouchers VALUES (?, ?, ?, ?, 'USD', ?, 'ISSUED', ?)
                """,
                (
                    voucher_id,
                    code,
                    voucher_type.upper(),
                    round(amount_usd, 2),
                    vendor_name,
                    now,
                ),
            )
        self.sync_to_gcs()
        return VoucherRecord(
            voucher_id=voucher_id,
            pnr=code,
            voucher_type=voucher_type.upper(),
            amount_usd=round(amount_usd, 2),
            vendor_name=vendor_name,
            status="ISSUED",
            created_at=now,
        )

    def create_approval_request(
        self,
        *,
        pnr: str,
        action_type: str,
        summary: str,
        payload: dict[str, Any],
        estimated_cost_usd: float = 0.0,
    ) -> ApprovalRecord:
        approval_id = f"APR-{uuid.uuid4().hex[:8].upper()}"
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, ?, NULL)
                """,
                (
                    approval_id,
                    pnr.strip().upper(),
                    action_type,
                    summary,
                    json.dumps(payload),
                    round(estimated_cost_usd, 2),
                    now,
                ),
            )
        self.sync_to_gcs()
        return ApprovalRecord(
            approval_id=approval_id,
            pnr=pnr.strip().upper(),
            action_type=action_type,
            summary=summary,
            payload=payload,
            estimated_cost_usd=round(estimated_cost_usd, 2),
            status="PENDING",
            created_at=now,
        )

    def get_approval_request(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return self._row_to_approval(row) if row else None

    def find_matching_approval(
        self, *, pnr: str, action_type: str
    ) -> ApprovalRecord | None:
        """Find the latest approval record for a PNR and action type."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM approvals
                WHERE pnr = ? AND action_type = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (pnr.strip().upper(), action_type),
            ).fetchone()
            return self._row_to_approval(row) if row else None

    def resolve_approval_request(
        self,
        approval_id: str,
        *,
        approved: bool,
        reviewer: str,
        decision_reason: str = "",
    ) -> ApprovalRecord | None:
        now = datetime.now(UTC).isoformat()
        status = "APPROVED" if approved else "REJECTED"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, reviewer = ?, decision_reason = ?, resolved_at = ?
                WHERE approval_id = ? AND status = 'PENDING'
                """,
                (status, reviewer, decision_reason, now, approval_id),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            record = self._row_to_approval(row) if row else None
        if record is not None:
            self.sync_to_gcs()
        return record

    def consume_approval_request(self, approval_id: str) -> ApprovalRecord | None:
        """Mark an APPROVED request as CONSUMED once executed to block replay."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = 'CONSUMED'
                WHERE approval_id = ? AND status = 'APPROVED'
                """,
                (approval_id,),
            )
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            record = self._row_to_approval(row) if row else None
        if record is not None:
            self.sync_to_gcs()
        return record

    def list_approvals(self, status: str | None = None) -> list[ApprovalRecord]:
        with self._lock, self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC",
                    (status.upper(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approvals ORDER BY created_at DESC"
                ).fetchall()
            return [self._row_to_approval(r) for r in rows]


@functools.cache
def get_repository() -> ReservationRepository:
    """Return the process-wide SQLite/GCS reservation repository."""
    settings = get_settings()
    return ReservationRepository(
        db_path=settings.db_path,
        gcs_bucket=settings.logs_bucket_name,
    )
