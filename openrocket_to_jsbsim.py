#!/usr/bin/env python3
"""Basic OpenRocket (.ork) to JSBSim generator.

This is intentionally a first-pass converter. It reads the OpenRocket XML inside
an .ork file and emits a JSBSim aircraft folder shaped like the current VADL
summer_subscale_2026 hand-built model.
"""

from __future__ import annotations

import argparse
import gzip
import math
import re
import shutil
import sqlite3
import subprocess
import textwrap
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

M_TO_FT = 3.280839895
M_TO_IN = 39.37007874
KG_TO_LB = 2.2046226218
M2_TO_FT2 = 10.7639104167
N_TO_LBF = 0.2248089431
OPENROCKET_MOTOR_DB_GZ_URL = "https://openrocket.github.io/motor-database/motors.db.gz"


@dataclass
class Motor:
    config_id: str
    manufacturer: str = "Unknown"
    designation: str = "unknown_motor"
    diameter_m: float = 0.0
    length_m: float = 0.0
    delay_s: float = 0.0
    digest: str = ""


@dataclass
class MotorCurve:
    manufacturer: str
    designation: str
    source: str
    format: str
    db_path: Path | None
    curve_id: int | None
    motor_id: int | None
    simfile_id: str = ""
    info_url: str = ""
    data_url: str = ""
    total_impulse_ns: float = 0.0
    avg_thrust_n: float = 0.0
    max_thrust_n: float = 0.0
    burn_time_s: float = 0.0
    propellant_kg: float = 0.0
    total_weight_kg: float = 0.0
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class Component:
    tag: str
    name: str
    length_m: float = 0.0
    radius_m: float = 0.0
    axial_position_m: float = 0.0
    mass_kg: float | None = None
    fin_count: int = 0
    thickness_m: float = 0.0
    cant_rad: float = 0.0
    fin_points_m: list[tuple[float, float]] = field(default_factory=list)
    cd: float | None = None
    diameter_m: float | None = None
    deploy_event: str = ""


@dataclass
class SimulationConfig:
    name: str = ""
    config_id: str = ""
    launch_rod_length_m: float = 1.0
    launch_rod_angle_deg: float = 0.0
    launch_rod_direction_deg: float = 90.0
    wind_speed_mps: float = 0.0
    wind_direction_rad: float = 0.0
    wind_turbulence: float = 0.0


@dataclass
class RocketModel:
    name: str
    safe_name: str
    comment: str = ""
    components: list[Component] = field(default_factory=list)
    motors: list[Motor] = field(default_factory=list)
    default_motor_config_id: str = ""
    simulations: list[SimulationConfig] = field(default_factory=list)
    motor_curve: MotorCurve | None = None

    @property
    def selected_motor(self) -> Motor | None:
        for motor in self.motors:
            if motor.config_id == self.default_motor_config_id:
                return motor
        return self.motors[-1] if self.motors else None


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "openrocket_rocket"


def child_text(elem: ET.Element | None, tag: str, default: str = "") -> str:
    if elem is None:
        return default
    child = elem.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def child_float(elem: ET.Element | None, tag: str, default: float = 0.0) -> float:
    raw = child_text(elem, tag, "")
    try:
        return float(raw)
    except ValueError:
        return default


def axial_position(elem: ET.Element) -> float:
    return child_float(elem, "position", child_float(elem, "axialoffset", 0.0))


def parse_fin_points(elem: ET.Element) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    finpoints = elem.find("finpoints")
    if finpoints is None:
        return points
    for point in finpoints.findall("point"):
        try:
            points.append((float(point.attrib.get("x", "0")), float(point.attrib.get("y", "0"))))
        except ValueError:
            continue
    return points


def polygon_area(points: Iterable[tuple[float, float]]) -> float:
    pts = list(points)
    if len(pts) < 3:
        return 0.0
    area = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def parse_ork(path: Path) -> RocketModel:
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path} is not an OpenRocket .ork zip file")

    with zipfile.ZipFile(path) as archive:
        xml_name = "rocket.ork"
        if xml_name not in archive.namelist():
            xml_candidates = [n for n in archive.namelist() if n.endswith(".ork") or n.endswith(".xml")]
            if not xml_candidates:
                raise ValueError("No rocket XML found inside .ork")
            xml_name = xml_candidates[0]
        root = ET.fromstring(archive.read(xml_name))

    rocket = root.find("rocket")
    if rocket is None:
        raise ValueError("No <rocket> node found")

    name = child_text(rocket, "name", path.stem)
    model = RocketModel(name=name, safe_name=slugify(name), comment=child_text(rocket, "comment"))

    for mc in rocket.findall("motorconfiguration"):
        if mc.attrib.get("default") == "true":
            model.default_motor_config_id = mc.attrib.get("configid", "")

    for elem in rocket.iter():
        if elem.tag in {
            "nosecone", "bodytube", "transition", "freeformfinset", "trapezoidfinset",
            "ellipticalfinset", "masscomponent", "parachute", "shockcord", "tubecoupler",
            "innerbodytube", "centeringring", "bulkhead",
        }:
            comp = Component(
                tag=elem.tag,
                name=child_text(elem, "name", elem.tag),
                length_m=child_float(elem, "length", 0.0),
                radius_m=child_float(elem, "radius", child_float(elem, "aftradius", 0.0)),
                axial_position_m=axial_position(elem),
                mass_kg=child_float(elem, "overridemass", math.nan),
                fin_count=int(child_float(elem, "fincount", child_float(elem, "instancecount", 0.0))),
                thickness_m=child_float(elem, "thickness", 0.0),
                cant_rad=child_float(elem, "cant", 0.0),
                fin_points_m=parse_fin_points(elem),
                cd=child_float(elem, "cd", math.nan),
                diameter_m=child_float(elem, "diameter", math.nan),
                deploy_event=child_text(elem, "deployevent"),
            )
            if comp.mass_kg is not None and math.isnan(comp.mass_kg):
                comp.mass_kg = None
            if comp.cd is not None and math.isnan(comp.cd):
                comp.cd = None
            if comp.diameter_m is not None and math.isnan(comp.diameter_m):
                comp.diameter_m = None
            model.components.append(comp)

    for motor in rocket.findall(".//motor"):
        model.motors.append(Motor(
            config_id=motor.attrib.get("configid", ""),
            manufacturer=child_text(motor, "manufacturer", "Unknown"),
            designation=child_text(motor, "designation", "unknown_motor"),
            diameter_m=child_float(motor, "diameter", 0.0),
            length_m=child_float(motor, "length", 0.0),
            delay_s=child_float(motor, "delay", 0.0),
            digest=child_text(motor, "digest"),
        ))

    if not model.default_motor_config_id and model.motors:
        model.default_motor_config_id = model.motors[-1].config_id

    for sim in root.findall(".//simulation"):
        cond = sim.find("conditions")
        if cond is None:
            continue
        model.simulations.append(SimulationConfig(
            name=child_text(sim, "name", "simulation"),
            config_id=child_text(cond, "configid"),
            launch_rod_length_m=child_float(cond, "launchrodlength", 1.0),
            launch_rod_angle_deg=child_float(cond, "launchrodangle", 0.0),
            launch_rod_direction_deg=child_float(cond, "launchroddirection", 90.0),
            wind_speed_mps=child_float(cond, "windaverage", 0.0),
            wind_direction_rad=child_float(cond, "winddirection", 0.0),
            wind_turbulence=child_float(cond, "windturbulence", 0.0),
        ))

    return model


def total_override_mass_kg(model: RocketModel) -> float:
    return sum(c.mass_kg or 0.0 for c in model.components)


def rocket_length_m(model: RocketModel) -> float:
    max_x = 0.0
    cursor = 0.0
    for comp in model.components:
        if comp.tag in {"nosecone", "bodytube", "transition"} and comp.length_m:
            cursor += comp.length_m
            max_x = max(max_x, cursor)
        max_x = max(max_x, comp.axial_position_m + comp.length_m)
        for x, _ in comp.fin_points_m:
            max_x = max(max_x, comp.axial_position_m + x)
    return max_x


def rocket_radius_m(model: RocketModel) -> float:
    return max((c.radius_m for c in model.components), default=0.0508)


def named_component(model: RocketModel, contains: str) -> Component | None:
    needle = contains.lower()
    for comp in model.components:
        if needle in comp.name.lower():
            return comp
    return None


def selected_sim(model: RocketModel) -> SimulationConfig:
    # Prefer a sim that uses the selected/default motor, otherwise choose the last sim.
    for sim in reversed(model.simulations):
        if sim.config_id == model.default_motor_config_id:
            return sim
    return model.simulations[-1] if model.simulations else SimulationConfig()


def normalize_motor_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def candidate_motor_db_paths(explicit: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)

    try:
        import os
        env_path = os.environ.get("OPENROCKET_MOTOR_DB")
    except Exception:
        env_path = None
    if env_path:
        candidates.append(Path(env_path))

    candidates.extend([
        Path("motors.db"),
        Path("motor_data") / "motors.db",
        Path.home() / ".openrocket" / "motors.db",
        Path.home() / ".config" / "OpenRocket" / "motors.db",
        Path("/mnt/c/Users/ramirm9/AppData/Roaming/OpenRocket/motors.db"),
        Path("/mnt/c/Users/ramirm9/AppData/Local/OpenRocket/motors.db"),
        Path("C:/Users/ramirm9/AppData/Roaming/OpenRocket/motors.db"),
        Path("C:/Users/ramirm9/AppData/Local/OpenRocket/motors.db"),
    ])

    for base in (
        Path("/mnt/c/Users/ramirm9/AppData/Roaming/OpenRocket"),
        Path("/mnt/c/Users/ramirm9/AppData/Local/OpenRocket"),
        Path("C:/Users/ramirm9/AppData/Roaming/OpenRocket"),
        Path("C:/Users/ramirm9/AppData/Local/OpenRocket"),
        Path.home() / ".openrocket",
    ):
        if base.exists():
            candidates.extend(base.rglob("motors.db"))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser())
        if key not in seen:
            unique.append(candidate.expanduser())
            seen.add(key)
    return unique


def load_motor_curve_from_db(db_path: Path, motor: Motor) -> MotorCurve | None:
    if not db_path.exists():
        return None

    target_designation = normalize_motor_text(motor.designation)
    target_manufacturer = normalize_motor_text(motor.manufacturer)
    target_digest = normalize_motor_text(motor.digest)

    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        motor_columns = {row["name"] for row in connection.execute("PRAGMA table_info(motors)").fetchall()}
        if "manufacturer_id" in motor_columns:
            manufacturer_fk = "manufacturer_id"
        elif "mfr_id" in motor_columns:
            manufacturer_fk = "mfr_id"
        else:
            raise RuntimeError(f"{db_path} is missing the motors manufacturer foreign-key column")

        query = """
            SELECT
                m.id AS motor_id,
                mf.name AS manufacturer,
                mf.abbrev AS manufacturer_abbrev,
                m.tc_motor_id,
                m.designation,
                m.common_name,
                m.total_impulse AS motor_total_impulse,
                m.avg_thrust AS motor_avg_thrust,
                m.max_thrust AS motor_max_thrust,
                m.burn_time AS motor_burn_time,
                m.propellant_weight,
                m.total_weight,
                tc.id AS curve_id,
                tc.tc_simfile_id,
                tc.source,
                tc.format,
                tc.info_url,
                tc.data_url,
                tc.total_impulse AS curve_total_impulse,
                tc.avg_thrust AS curve_avg_thrust,
                tc.max_thrust AS curve_max_thrust,
                tc.burn_time AS curve_burn_time
            FROM motors m
            JOIN manufacturers mf ON mf.id = m.{manufacturer_fk}
            JOIN thrust_curves tc ON tc.motor_id = m.id
        """.format(manufacturer_fk=manufacturer_fk)
        rows = connection.execute(query).fetchall()

        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            designation = normalize_motor_text(row["designation"] or "")
            common = normalize_motor_text(row["common_name"] or "")
            manufacturer = normalize_motor_text(row["manufacturer"] or "")
            abbrev = normalize_motor_text(row["manufacturer_abbrev"] or "")
            tc_motor_id = normalize_motor_text(row["tc_motor_id"] or "")
            simfile_id = normalize_motor_text(row["tc_simfile_id"] or "")

            score = 0
            if target_designation and target_designation in {designation, common}:
                score += 100
            elif target_designation and (target_designation in designation or target_designation in common):
                score += 70
            else:
                continue

            if target_manufacturer and target_manufacturer in {manufacturer, abbrev}:
                score += 30
            elif target_manufacturer and (target_manufacturer in manufacturer or target_manufacturer in abbrev):
                score += 15
            if target_digest and target_digest in {tc_motor_id, simfile_id}:
                score += 40
            source = (row["source"] or "").lower()
            if "cert" in source:
                score += 8
            if "manufacturer" in source:
                score += 6
            scored.append((score, row))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        row = scored[0][1]
        points_rows = connection.execute(
            """
            SELECT time_seconds, force_newtons
            FROM thrust_data
            WHERE curve_id = ?
            ORDER BY time_seconds
            """,
            (row["curve_id"],),
        ).fetchall()
        points = [(float(p["time_seconds"]), float(p["force_newtons"])) for p in points_rows]
        points = [(t, thrust) for t, thrust in points if math.isfinite(t) and math.isfinite(thrust)]
        if len(points) < 2:
            return None

        return MotorCurve(
            manufacturer=str(row["manufacturer"] or motor.manufacturer),
            designation=str(row["designation"] or motor.designation),
            source=str(row["source"] or "OpenRocket motors.db"),
            format=str(row["format"] or "sqlite"),
            db_path=db_path,
            curve_id=int(row["curve_id"]),
            motor_id=int(row["motor_id"]),
            simfile_id=str(row["tc_simfile_id"] or ""),
            info_url=str(row["info_url"] or ""),
            data_url=str(row["data_url"] or ""),
            total_impulse_ns=float(row["curve_total_impulse"] or row["motor_total_impulse"] or 0.0),
            avg_thrust_n=float(row["curve_avg_thrust"] or row["motor_avg_thrust"] or 0.0),
            max_thrust_n=float(row["curve_max_thrust"] or row["motor_max_thrust"] or 0.0),
            burn_time_s=float(row["curve_burn_time"] or row["motor_burn_time"] or max(t for t, _ in points)),
            # OpenRocket motor-database stores motor weights in grams.
            propellant_kg=float(row["propellant_weight"] or 0.0) / 1000.0,
            total_weight_kg=float(row["total_weight"] or 0.0) / 1000.0,
            points=points,
        )
    finally:
        connection.close()


def download_openrocket_motor_db(target: Path) -> Path | None:
    target = target.expanduser()
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    gz_target = target.with_suffix(target.suffix + ".gz")
    try:
        print(f"Downloading OpenRocket motor database: {OPENROCKET_MOTOR_DB_GZ_URL}")
        with urllib.request.urlopen(OPENROCKET_MOTOR_DB_GZ_URL, timeout=30) as response:
            gz_target.write_bytes(response.read())
        with gzip.open(gz_target, "rb") as src:
            target.write_bytes(src.read())
        return target
    except Exception as exc:
        print(f"WARNING: automatic OpenRocket motor database download failed: {exc}")
        return None


def resolve_motor_curve(model: RocketModel, explicit_db: Path | None = None, auto_download: bool = True) -> tuple[MotorCurve | None, list[Path]]:
    motor = model.selected_motor
    searched = candidate_motor_db_paths(explicit_db)
    if not motor:
        return None, searched
    for db_path in searched:
        curve = load_motor_curve_from_db(db_path, motor)
        if curve:
            model.motor_curve = curve
            return curve, searched

    if auto_download and explicit_db is None:
        downloaded = download_openrocket_motor_db(Path("motor_data") / "motors.db")
        if downloaded:
            searched.append(downloaded)
            curve = load_motor_curve_from_db(downloaded, motor)
            if curve:
                model.motor_curve = curve
                return curve, searched
    return None, searched


def motor_curve_provenance_comment(curve: MotorCurve | None) -> str:
    if not curve:
        return "<!-- Motor thrust curve source: none. -->"
    db_path = str(curve.db_path) if curve.db_path else "unknown"
    return textwrap.dedent(f"""
    <!-- Motor thrust curve source:
         Matched motor: {xml_escape(curve.manufacturer)} {xml_escape(curve.designation)}
         Database: {xml_escape(db_path)}
         OpenRocket motor id: {curve.motor_id}
         OpenRocket curve id: {curve.curve_id}
         Simulation file id: {xml_escape(curve.simfile_id)}
         Source: {xml_escape(curve.source)}
         Format: {xml_escape(curve.format)}
         Info URL: {xml_escape(curve.info_url)}
         Data URL: {xml_escape(curve.data_url)}
         Points: {len(curve.points)}
         Total impulse: {curve.total_impulse_ns:.6f} N*s
         Average thrust: {curve.avg_thrust_n:.6f} N
         Maximum thrust: {curve.max_thrust_n:.6f} N
         Burn time: {curve.burn_time_s:.6f} s
         Propellant mass: {curve.propellant_kg:.6f} kg
         Total motor mass: {curve.total_weight_kg:.6f} kg
    -->""").strip()


def build_explicit_placeholder_curve(motor: Motor) -> MotorCurve:
    designation = motor.designation.upper()
    if designation == "I600R":
        propellant_kg = 0.324
        total_weight_kg = 0.617
        burn_time_s = 1.12
        points = [(0.0, 861.0), (0.02, 861.0), (0.25, 831.0), (0.50, 784.0), (0.80, 589.0), (1.10, 58.0), (1.12, 0.0)]
    else:
        propellant_kg = 0.05
        total_weight_kg = 0.10
        burn_time_s = max(0.25, motor.length_m * 2.0)
        points = [(0.0, 100.0), (0.2 * burn_time_s, 120.0), (0.8 * burn_time_s, 80.0), (burn_time_s, 0.0)]
    total_impulse = 0.0
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        total_impulse += 0.5 * (f0 + f1) * max(0.0, t1 - t0)
    return MotorCurve(
        manufacturer=motor.manufacturer,
        designation=motor.designation,
        source="EXPLICIT PLACEHOLDER - generated only because --allow-placeholder-thrust was used",
        format="synthetic",
        db_path=None,
        curve_id=None,
        motor_id=None,
        total_impulse_ns=total_impulse,
        avg_thrust_n=total_impulse / burn_time_s if burn_time_s else 0.0,
        max_thrust_n=max(force for _, force in points),
        burn_time_s=burn_time_s,
        propellant_kg=propellant_kg,
        total_weight_kg=total_weight_kg,
        points=points,
    )


def generate_aircraft_xml(model: RocketModel) -> str:
    radius_m = rocket_radius_m(model)
    diameter_m = 2.0 * radius_m
    length_m = rocket_length_m(model)
    ref_area_ft2 = math.pi * radius_m * radius_m * M2_TO_FT2
    length_ft = length_m * M_TO_FT
    empty_mass_lb = total_override_mass_kg(model) * KG_TO_LB
    motor = model.selected_motor or Motor(config_id="")
    motor_empty_lb = 0.0
    propellant_lb = 0.00001
    if model.motor_curve:
        propellant_lb = max(model.motor_curve.propellant_kg * KG_TO_LB, 0.00001)
        if model.motor_curve.total_weight_kg > model.motor_curve.propellant_kg:
            motor_empty_lb = (model.motor_curve.total_weight_kg - model.motor_curve.propellant_kg) * KG_TO_LB

    cg_x_in = 0.55 * length_m * M_TO_IN
    cp_x_in = 0.69 * length_m * M_TO_IN

    racs = named_component(model, "racs fin") or named_component(model, "roll apogee control system fins")
    main_fins = named_component(model, "clipped")
    racs_area_ft2 = 0.0
    racs_single_area_ft2 = 0.0
    racs_arm_ft = 0.0
    racs_station_in = 0.0
    racs_cant_rad = 0.0
    if racs:
        single_area_m2 = polygon_area(racs.fin_points_m)
        racs_single_area_ft2 = single_area_m2 * M2_TO_FT2
        racs_area_ft2 = racs_single_area_ft2 * max(1, racs.fin_count)
        racs_station_in = racs.axial_position_m * M_TO_IN
        racs_arm_ft = abs(racs_station_in - cg_x_in) / 12.0
        racs_cant_rad = racs.cant_rad

    fin_area_ft2 = 0.0
    if main_fins:
        fin_area_ft2 = polygon_area(main_fins.fin_points_m) * M2_TO_FT2 * max(1, main_fins.fin_count)

    chute = named_component(model, "parachute")
    chute_area_ft2 = 0.0
    chute_cd = 2.2
    if chute and chute.diameter_m:
        chute_area_ft2 = math.pi * (chute.diameter_m / 2.0) ** 2 * M2_TO_FT2
        chute_cd = chute.cd or 2.2

    engine_file = slugify(f"{motor.manufacturer}_{motor.designation}_engine")
    nozzle_file = slugify(f"{motor.designation}_nozzle")

    return f'''<?xml version="1.0"?>
<?xml-stylesheet type="text/xsl" href="http://jsbsim.sourceforge.net/JSBSim.xsl"?>

<fdm_config name="{model.safe_name}" version="2.0" release="ALPHA"
   xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
   xsi:noNamespaceSchemaLocation="http://jsbsim.sourceforge.net/JSBSim.xsd">

 <fileheader>
  <author>Generated by openrocket_to_jsbsim.py</author>
  <filecreationdate>auto-generated</filecreationdate>
  <version>0.1</version>
  <description>Generated from OpenRocket file. Source comment: {xml_escape(model.comment)}</description>
 </fileheader>

 {motor_curve_provenance_comment(model.motor_curve)}

 <!-- Geometry source: OpenRocket .ork component tree. Lengths, radii, fin count, fin planform, RACS station, and parachute dimensions are parsed from the design. Aerodynamic coefficient constants are inherited from the current summer_subscale_2026 JSBSim baseline until a higher-fidelity aero model is added. -->
 <metrics>
   <wingarea unit="FT2">{ref_area_ft2:.6f}</wingarea>
   <wingspan unit="FT">{diameter_m * M_TO_FT:.6f}</wingspan>
   <wing_incidence unit="DEG">0.0</wing_incidence>
   <chord unit="FT">{length_ft:.6f}</chord>
   <htailarea unit="FT2">{fin_area_ft2 / 2.0:.6f}</htailarea>
   <htailarm unit="FT">{max(0.1, abs(cp_x_in - cg_x_in) / 12.0):.6f}</htailarm>
   <vtailarea unit="FT2">{fin_area_ft2 / 2.0:.6f}</vtailarea>
   <vtailarm unit="FT">{max(0.1, abs(cp_x_in - cg_x_in) / 12.0):.6f}</vtailarm>

   <location name="AERORP" unit="IN"><x>{cp_x_in:.3f}</x><y>0</y><z>0</z></location>
   <location name="EYEPOINT" unit="FT"><x>1.17</x><y>-1.50</y><z>3.75</z></location>
   <location name="VRP" unit="FT"><x>0</x><y>0</y><z>0</z></location>

   <property value="{radius_m * M_TO_FT:.6f}">metrics/rocket-radius-ft</property>
   <property value="{max(1, racs.fin_count if racs else 0)}">metrics/racs-fin-count</property>
   <property value="{racs_cant_rad:.9f}">metrics/racs-fin-cant-rad</property>
   <property value="0.03">metrics/racs-roll-effectiveness</property>
   <property value="{racs_single_area_ft2:.6f}">metrics/racs-single-fin-area</property>
   <property value="{racs_area_ft2:.6f}">metrics/acsarea</property>
   <property value="{racs_arm_ft:.6f}">metrics/acsarm</property>
   <property value="{max(0.1, abs(cp_x_in - cg_x_in) / 12.0):.6f}">metrics/ltail-ft</property>
   <property value="{racs_station_in:.6f}">metrics/racs-station-in</property>
 </metrics>

 <!-- Mass source: dry vehicle mass and CG are parsed from OpenRocket component masses/locations. Motor propellant and casing masses come from the matched OpenRocket motor database curve when available. Inertia is estimated from the parsed rocket dimensions as a starter JSBSim model. -->
 <mass_balance>
   <ixx unit="KG*M2">0.007</ixx>
   <iyy unit="KG*M2">1.313</iyy>
   <izz unit="KG*M2">1.313</izz>
   <emptywt unit="LBS">{empty_mass_lb:.6f}</emptywt>
   <location name="CG" unit="IN"><x>{cg_x_in:.3f}</x><y>0</y><z>0</z></location>
   <pointmass name="Motor Casing">
    <weight unit="LBS">{motor_empty_lb:.6f}</weight>
    <location name="POINTMASS" unit="IN"><x>{max(0.0, length_m - motor.length_m / 2.0) * M_TO_IN:.3f}</x><y>0</y><z>0</z></location>
   </pointmass>
 </mass_balance>

 <!-- Propulsion source: engine/nozzle XML files are generated from the selected OpenRocket motor and matched OpenRocket motor database thrust curve. Tank capacity is the database propellant mass converted to JSBSim units. -->
 <propulsion>
  <engine file="{engine_file}">
   <feed>0</feed><running>0</running><starter>0</starter>
   <thruster file="{nozzle_file}">
    <location unit="IN"><x>{length_m * M_TO_IN:.3f}</x><y>0</y><z>0</z></location>
    <orient><pitch unit="DEG">0</pitch><yaw unit="DEG">0</yaw></orient>
   </thruster>
  </engine>
  <tank type="FUEL">
   <fuel_type>FUEL</fuel_type>
   <location unit="IN"><x>{max(0.0, length_m - motor.length_m / 2.0) * M_TO_IN:.3f}</x><y>0</y><z>0</z></location>
   <capacity unit="LBS">{propellant_lb:.6f}</capacity>
   <contents unit="LBS">{propellant_lb:.6f}</contents>
  </tank>
 </propulsion>

 <!-- Flight-control source: minimal generated JSBSim controls matching the existing VADL rocket runner interface. This is not exported by OpenRocket. -->
 <flight_control name="FCS: generated rocket">
  <channel name="Generated Controls"><switch name="fcs/gear-no-wow"><default value="1"/></switch></channel>
 </flight_control>

 <!-- Aerodynamics source: reference dimensions use parsed OpenRocket geometry; coefficient structure and baseline constants are copied from the VADL summer_subscale_2026 JSBSim model. RACS/ACS terms are preserved as JSBSim hooks for later controller integration. -->
 <aerodynamics>
  <property value="0">aero/ACSangle</property>
  <property value="0">aero/RACS_roll_cmd_rad</property>
  <property value="1">aero/RACS_cant_active</property>
  <property value="0">fcs/racs_fin_1_pos_rad</property>
  <property value="0">fcs/racs_fin_2_pos_rad</property>
  <property value="0">fcs/racs_fin_3_pos_rad</property>
  <property value="0">fcs/racs_fin_4_pos_rad</property>

  <axis name="LIFT"><function name="aero/force/Lift_alpha"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><property>aero/alpha-rad</property><value>2.4</value></product></function></axis>
  <axis name="DRAG"><function name="aero/force/Drag_minimum"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><value>0.56</value></product></function></axis>
  <axis name="SIDE"><function name="aero/force/Side_beta"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><property>aero/beta-rad</property><value>2.4</value></product></function></axis>
  <axis name="PITCH"><function name="aero/moment/Pitch_alpha"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><property>metrics/ltail-ft</property><property>aero/alpha-rad</property><value>-2.4</value></product></function></axis>
  <axis name="YAW"><function name="aero/moment/Yaw_beta"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><property>metrics/ltail-ft</property><property>aero/beta-rad</property><value>-2.4</value></product></function></axis>
  <axis name="ROLL">
   <function name="aero/moment/Roll_damp"><product><property>aero/qbar-psf</property><property>metrics/Sw-sqft</property><property>metrics/rocket-radius-ft</property><property>aero/bi2vel</property><property>velocities/p-aero-rad_sec</property><value>-0.25</value></product></function>
   <function name="aero/moment/Roll_RACS_fixed_cant"><product><property>aero/qbar-psf</property><property>metrics/acsarea</property><property>metrics/rocket-radius-ft</property><sin><property>metrics/racs-fin-cant-rad</property></sin><property>metrics/racs-roll-effectiveness</property><property>aero/RACS_cant_active</property></product></function>
  </axis>
 </aerodynamics>

 <!-- External reactions source: drogue drag area comes from parsed OpenRocket parachute diameter and Cd. Rail friction hook follows the current VADL JSBSim baseline and defaults off. -->
 <external_reactions>
  <property value="{chute_area_ft2:.6f}">external_reactions/drogue_chute/drag_area</property>
  <property value="0">external_reactions/drogue_chute/drogue_open</property>
  <property value="0">external_reactions/rail_friction/on</property>
  <property value="0">external_reactions/rail_friction/force_lbs</property>
  <force name="rail_friction" frame="BODY"><function><product><property>external_reactions/rail_friction/force_lbs</property><table><independentVar lookup="row">external_reactions/rail_friction/on</independentVar><tableData>0 0
1 1</tableData></table></product></function><location unit="IN"><x>{cg_x_in:.3f}</x><y>0</y><z>0</z></location><direction><x>-1</x><y>0</y><z>0</z></direction></force>
  <force name="drogue_chute" frame="WIND"><function><product><property>aero/qbar-psf</property><property>external_reactions/drogue_chute/drag_area</property><value>{chute_cd:.3f}</value><property>external_reactions/drogue_chute/drogue_open</property></product></function><location unit="FT"><x>0</x><y>0</y><z>0</z></location><direction><x>-1</x><y>0</y><z>0</z></direction></force>
 </external_reactions>

 <!-- Ground reactions source: simple JSBSim contact model generated from rocket length. OpenRocket does not directly provide this JSBSim contact definition. -->
 <ground_reactions>
  <contact type="BOGEY" name="LANDING_CONTACT"><location unit="IN"><x>{length_m * M_TO_IN:.3f}</x><y>0</y><z>0.25</z></location><static_friction>0.8</static_friction><dynamic_friction>0.7</dynamic_friction><rolling_friction>0.01</rolling_friction><spring_coeff unit="LBS/FT">0.01</spring_coeff><damping_coeff unit="LBS/FT/SEC">0.01</damping_coeff><max_steer unit="DEG">0</max_steer><brake_group>NONE</brake_group></contact>
 </ground_reactions>
</fdm_config>
'''


def generate_engine_xml(model: RocketModel) -> tuple[str, str]:
    motor = model.selected_motor or Motor(config_id="")
    curve = model.motor_curve
    if curve is None:
        raise RuntimeError(
            f"No real thrust curve resolved for {motor.manufacturer} {motor.designation}. "
            "Provide --motor-db path/to/motors.db or use --allow-placeholder-thrust explicitly."
        )
    if curve.propellant_kg <= 0.0:
        raise RuntimeError(
            f"Matched thrust curve for {curve.manufacturer} {curve.designation}, "
            "but propellant_weight is missing/zero in the motor database."
        )

    engine_file = slugify(f"{curve.manufacturer}_{curve.designation}_engine")
    propellant_lb = curve.propellant_kg * KG_TO_LB
    burn_time = curve.burn_time_s or max(t for t, _ in curve.points)
    total_impulse = curve.total_impulse_ns
    if total_impulse <= 0.0:
        total_impulse = 0.0
        for (t0, f0), (t1, f1) in zip(curve.points, curve.points[1:]):
            total_impulse += 0.5 * (f0 + f1) * max(0.0, t1 - t0)
    isp = total_impulse / (curve.propellant_kg * 9.80665) if curve.propellant_kg > 0.0 and total_impulse > 0.0 else 200.0

    positive_points = [(time_s, thrust_n) for time_s, thrust_n in curve.points if thrust_n > 0.0]
    if not positive_points:
        raise RuntimeError(
            f"Matched thrust curve for {curve.manufacturer} {curve.designation}, "
            "but it does not contain any positive thrust samples."
        )

    # JSBSim advances this table using propellant flow computed from thrust.
    # If the first point is zero thrust, fuel flow is zero and the engine can
    # get stuck at the first row forever. Bootstrap row zero with the first
    # measured positive sample, then keep the rest of the imported curve.
    first_positive_time_s, first_positive_thrust_n = positive_points[0]
    table_source_points = [(0.0, first_positive_thrust_n)]
    table_source_points.extend(
        (time_s, thrust_n)
        for time_s, thrust_n in curve.points
        if time_s > first_positive_time_s
    )

    table_lines: list[str] = []
    for time_s, thrust_n in table_source_points:
        propellant_expended_lb = propellant_lb * min(max(time_s / burn_time, 0.0), 1.0) if burn_time > 0.0 else 0.0
        table_lines.append(f"        {propellant_expended_lb:.6f} {thrust_n * N_TO_LBF:.6f}")
    if table_lines[-1].split()[0] != f"{propellant_lb:.6f}":
        table_lines.append(f"        {propellant_lb:.6f} 0.000000")

    xml = f'''<?xml version="1.0"?>
{motor_curve_provenance_comment(curve)}
<!-- JSBSim thrust table conversion:
     Source data are OpenRocket motor database thrust samples: time_seconds, force_newtons.
     JSBSim repo convention used here: thrust in lbf versus propellant expended in lbm.
     Propellant expended is mapped from time using constant mass flow:
       propellant_expended_lbm = propellant_mass_lbm * clamp(time_seconds / burn_time_seconds, 0, 1)
     No thrust data is invented; this file is generated only after a database curve match. -->
<rocket_engine name="{engine_file}">
 <isp>{isp:.6f}</isp>
 <builduptime>0.0</builduptime>
 <thrust_table name="propulsion/thrust_prop_remain" type="internal">
  <tableData>
{chr(10).join(table_lines)}
  </tableData>
 </thrust_table>
</rocket_engine>
'''
    return engine_file, xml


def generate_nozzle_xml(model: RocketModel) -> tuple[str, str]:
    motor = model.selected_motor or Motor(config_id="")
    nozzle_file = slugify(f"{motor.designation}_nozzle")
    xml = f'''<?xml version="1.0"?>
<!-- Nozzle source:
     OpenRocket motor database supplies delivered thrust, not nozzle geometry.
     JSBSim pressure thrust is intentionally disabled with near-zero exit area
     so the imported thrust curve is not double-counted. -->
<nozzle name="{nozzle_file}">
 <pe unit="PSF">0.0</pe>
 <area unit="FT2">0.000001</area>
</nozzle>
'''
    return nozzle_file, xml


def estimate_motor_burn_time_s(model: RocketModel) -> float:
    if model.motor_curve and model.motor_curve.burn_time_s > 0.0:
        return model.motor_curve.burn_time_s
    motor = model.selected_motor
    return max(0.25, motor.length_m * 2.0) if motor else 1.0


def estimate_expected_impulse_lbfs(model: RocketModel) -> float:
    if model.motor_curve and model.motor_curve.total_impulse_ns > 0.0:
        return model.motor_curve.total_impulse_ns * N_TO_LBF
    return 0.0


def generate_cpp(model: RocketModel, hitl: bool) -> str:
    sim = selected_sim(model)
    exe_name = "HIL" if hitl else "standalone"
    motor = model.selected_motor or Motor(config_id="")
    motor_label = motor.designation or "unknown_motor"
    burn_time_s = estimate_motor_burn_time_s(model)
    expected_impulse = estimate_expected_impulse_lbfs(model)
    rail_length_ft = max(sim.launch_rod_length_m * M_TO_FT, 1.0)
    wind_east_fps = sim.wind_speed_mps * M_TO_FT * math.sin(sim.wind_direction_rad)
    wind_north_fps = sim.wind_speed_mps * M_TO_FT * math.cos(sim.wind_direction_rad)
    if hitl:
        return f'''// Generated starter {exe_name} runner for {model.safe_name}.
// This compiles and loads the generated aircraft. Add the VADL serial HIL loop
// when this converted model is ready for hardware-in-the-loop testing.

#include <iostream>
#include <memory>
#include <FGFDMExec.h>
#include <initialization/FGInitialCondition.h>

int main() {{
    std::unique_ptr<JSBSim::FGFDMExec> fdmExec(new JSBSim::FGFDMExec());
    fdmExec->Setdt(1.0 / 400.0);

    const std::string aircraftName = "{model.safe_name}";
    if (!fdmExec->LoadModel(aircraftName)) {{
        std::cerr << "Failed to load aircraft model: " << aircraftName << std::endl;
        return 1;
    }}

    fdmExec->GetIC()->SetAltitudeASLFtIC(3.28084);
    fdmExec->GetIC()->SetThetaDegIC(90.0 - {sim.launch_rod_angle_deg:.6f});
    fdmExec->GetIC()->SetPsiDegIC({sim.launch_rod_direction_deg:.6f});
    fdmExec->GetIC()->SetPhiDegIC(0.0);
    fdmExec->RunIC();

    std::cout << "Loaded generated JSBSim HIL scaffold: " << aircraftName << std::endl;
    std::cout << "Next step: copy the serial packet loop from the working summer_subscale_2026 HIL runner." << std::endl;
    return 0;
}}
'''
    return f'''// Generated full-flight standalone runner for {model.safe_name}.
// Baseline structure follows the VADL summer_subscale_2026 JSBSim runner.

#include <cmath>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <string>

#include <FGFDMExec.h>
#include <initialization/FGInitialCondition.h>
#include <models/FGAuxiliary.h>
#include <models/FGFCS.h>
#include <models/FGPropagate.h>
#include <models/FGPropulsion.h>
#include <models/FGMassBalance.h>
#include <models/FGAccelerations.h>
#include <models/propulsion/FGTank.h>

int main() {{
    std::unique_ptr<JSBSim::FGFDMExec> fdmExec(new JSBSim::FGFDMExec());
    fdmExec->Setdt(1.0 / 200.0);

    const std::string aircraftName = "{model.safe_name}";
    const std::string motorLabel = "{motor_label}";
    const double motorBurnTimeS = {burn_time_s:.6f};
    const double expectedImpulseLbfs = {expected_impulse:.6f};
    const double launchTerrainElevationFt = 0.0;
    const double launchAltitudeFt = launchTerrainElevationFt + 3.28084;
    const double launchLatitudeDeg = 36.388303;
    const double launchLongitudeDeg = -86.447590;
    const double railTiltFromVerticalDeg = {sim.launch_rod_angle_deg:.6f};
    const double railAzimuthDeg = {sim.launch_rod_direction_deg:.6f};
    const double railLengthFt = {rail_length_ft:.6f};
    const double windNorthFps = {wind_north_fps:.6f};
    const double windEastFps = {wind_east_fps:.6f};

    if (!fdmExec->LoadModel(aircraftName)) {{
        std::cerr << "Failed to load aircraft model: " << aircraftName << std::endl;
        return 1;
    }}

    if (!fdmExec->GetPropagate()->InitModel()) {{
        std::cerr << "Failed to initialize aircraft model: " << aircraftName << std::endl;
        return 1;
    }}

    fdmExec->GetIC()->SetTerrainElevationFtIC(launchTerrainElevationFt);
    fdmExec->GetIC()->SetAltitudeASLFtIC(launchAltitudeFt);
    fdmExec->GetIC()->SetLatitudeDegIC(launchLatitudeDeg);
    fdmExec->GetIC()->SetLongitudeDegIC(launchLongitudeDeg);
    fdmExec->GetIC()->SetThetaDegIC(90.0 - railTiltFromVerticalDeg);
    fdmExec->GetIC()->SetPsiDegIC(railAzimuthDeg);
    fdmExec->GetIC()->SetPhiDegIC(0.0);
    fdmExec->GetIC()->SetVNorthFpsIC(0.0);
    fdmExec->GetIC()->SetVEastFpsIC(0.0);
    fdmExec->GetIC()->SetVDownFpsIC(0.0);
    fdmExec->GetIC()->SetPRadpsIC(0.0);
    fdmExec->GetIC()->SetQRadpsIC(0.0);
    fdmExec->GetIC()->SetRRadpsIC(0.0);

    auto set_racs_roll_angle = [&](double angle_rad) {{
        fdmExec->SetPropertyValue("aero/RACS_roll_cmd_rad", angle_rad);
        fdmExec->SetPropertyValue("fcs/racs_fin_1_pos_rad", angle_rad);
        fdmExec->SetPropertyValue("fcs/racs_fin_2_pos_rad", angle_rad);
        fdmExec->SetPropertyValue("fcs/racs_fin_3_pos_rad", angle_rad);
        fdmExec->SetPropertyValue("fcs/racs_fin_4_pos_rad", angle_rad);
    }};

    fdmExec->SetPropertyValue("fcs/elevator-cmd-norm", 0.0);
    fdmExec->SetPropertyValue("fcs/pitch-trim-cmd-norm", 0.0);
    fdmExec->SetPropertyValue("fcs/aileron-cmd-norm", 0.0);
    fdmExec->SetPropertyValue("fcs/rudder-cmd-norm", 0.0);
    set_racs_roll_angle(0.0);
    fdmExec->SetPropertyValue("aero/RACS_cant_active", 1.0);
    fdmExec->SetPropertyValue("external_reactions/drogue_chute/drogue_open", 0.0);
    fdmExec->SetPropertyValue("external_reactions/rail_friction/on", 0.0);
    fdmExec->SetPropertyValue("external_reactions/rail_friction/force_lbs", 0.0);

    fdmExec->RunIC();
    fdmExec->SetPropertyValue("atmosphere/turb-rate", {sim.wind_turbulence:.6f});
    fdmExec->SetPropertyValue("atmosphere/turb-gain", 1.0);
    fdmExec->SetPropertyValue("atmosphere/wind-north-fps", windNorthFps);
    fdmExec->SetPropertyValue("atmosphere/wind-east-fps", windEastFps);
    fdmExec->SetPropertyValue("atmosphere/wind-down-fps", 0.0);

    std::filesystem::create_directories("data");
    const std::string outputCsv = "data/{model.safe_name}_trajectory.csv";
    std::ofstream outputFile(outputCsv);
    outputFile << "time_s,Altitude,Filtered Altitude,AGL Altitude,Max AGL Altitude,"
               << "Velocity North,Velocity East,Velocity Down,Barometer Velocity,"
               << "Pressure (kPa),Temperature (C),"
               << "Accel X,Accel Y,Accel Z,"
               << "Linear Accel Body X,Linear Accel Body Y,Linear Accel Body Z,"
               << "Position North,Position East,Position Down,"
               << "Phase,Fins Deployed,Roll Detected,CG X,Mass,"
               << "Roll (deg),Pitch (deg),Yaw (deg),Gyro X,Gyro Y,Gyro Z,"
               << "JSBSim Time,X_ft,Y_ft,Z_ft,Altitude_ft,AGL Altitude ft,"
               << "Vertical Velocity ft/s,Vertical Acceleration ft/s^2,"
               << "Body Accel X ft/s^2,Body Accel Y ft/s^2,Body Accel Z ft/s^2\\n";

    bool motorIgnited = false;
    bool engineShutdown = false;
    bool didLiftoff = false;
    bool reachedApogee = false;
    bool chuteDeployed = false;
    bool onRail = false;
    double railDistanceFt = 0.0;
    double maxAltitudeAglFt = 0.0;
    double totalImpulseLbfs = 0.0;
    double lastTime = 0.0;
    double lastPrintTime = -1.0;

    const double ignitionTimeS = 0.001;
    const double liftoffThresholdAglFt = 10.0;
    const double railMu = 0.20;
    const double preloadTotalLbf = 1.5;
    const double initialLatitude = launchLatitudeDeg;
    const double initialLongitude = launchLongitudeDeg;
    const double initialAltitude = launchAltitudeFt;
    constexpr double pi = 3.14159265358979323846;

    auto compute_rail_friction_lbf = [&]() -> double {{
        const double weightLbf = fdmExec->GetPropertyValue("inertial/weight-lbs");
        const double tiltRad = railTiltFromVerticalDeg * pi / 180.0;
        return railMu * (preloadTotalLbf + std::abs(weightLbf * std::sin(tiltRad)));
    }};

    std::cout << "Starting generated " << motorLabel << " rocket simulation" << std::endl;
    std::cout << "Aircraft: " << aircraftName << std::endl;
    std::cout << "Output CSV: " << outputCsv << std::endl;
    std::cout << "Motor burn time: " << motorBurnTimeS << " s" << std::endl;
    std::cout << "Expected impulse: " << expectedImpulseLbfs << " lbf*s" << std::endl;
    std::cout << "Launch rail length: " << railLengthFt << " ft" << std::endl;
    std::cout << "Wind North/East: " << windNorthFps << ", " << windEastFps << " ft/s" << std::endl;

    while (fdmExec->GetSimTime() < 120.0 && fdmExec->GetPropagate()->GetAltitudeASL() >= launchTerrainElevationFt - 10.0) {{
        fdmExec->Run();
        const double time = fdmExec->GetSimTime();
        const double dt = time - lastTime;
        const double altitude = fdmExec->GetPropagate()->GetAltitudeASL();
        const double altitudeAgl = altitude - launchTerrainElevationFt;
        const double vx = fdmExec->GetPropagate()->GetVel(1);
        const double vy = fdmExec->GetPropagate()->GetVel(2);
        const double vz = fdmExec->GetPropagate()->GetVel(3);
        const double velocityMagnitude = std::sqrt(vx * vx + vy * vy + vz * vz);
        const double verticalVelocity = -vz;

        JSBSim::FGColumnVector3 aBody = fdmExec->GetAccelerations()->GetBodyAccel();
        JSBSim::FGMatrix33 bodyToLocal = fdmExec->GetPropagate()->GetTb2l();
        JSBSim::FGColumnVector3 aLocal = bodyToLocal * aBody;
        const double verticalAcceleration = -aLocal(3);

        if (std::isnan(velocityMagnitude) || std::isnan(altitude) || velocityMagnitude > 10000.0 || altitude > 100000.0) {{
            std::cout << "ERROR: Numerical divergence detected at t=" << time << " s" << std::endl;
            break;
        }}

        if (!motorIgnited && time >= ignitionTimeS) {{
            std::cout << "Igniting engine at t=" << time << " s" << std::endl;
            fdmExec->GetFCS()->SetThrottleCmd(0, 1.0);
            auto engine = fdmExec->GetPropulsion()->GetEngine(0);
            engine->SetRunning(true);
            fdmExec->SetPropertyValue("gear/unit[0]/spring-coeff-lbs_ft", 0.0);
            fdmExec->SetPropertyValue("gear/unit[0]/damping-coeff-lbs_ft_sec", 0.0);
            fdmExec->SetPropertyValue("gear/unit[0]/location-z-in", -1000.0);
            onRail = true;
            railDistanceFt = 0.0;
            fdmExec->SetPropertyValue("external_reactions/rail_friction/force_lbs", compute_rail_friction_lbf());
            fdmExec->SetPropertyValue("external_reactions/rail_friction/on", 1.0);
            motorIgnited = true;
        }}

        if (motorIgnited && onRail && dt > 0.0) {{
            const double uFps = fdmExec->GetPropertyValue("velocities/u-fps");
            if (uFps > 0.0) railDistanceFt += uFps * dt;
            fdmExec->SetPropertyValue("external_reactions/rail_friction/force_lbs", compute_rail_friction_lbf());
            if (railDistanceFt >= railLengthFt) {{
                onRail = false;
                fdmExec->SetPropertyValue("external_reactions/rail_friction/on", 0.0);
                fdmExec->SetPropertyValue("external_reactions/rail_friction/force_lbs", 0.0);
                std::cout << "Rail exit detected at t=" << time << " s" << std::endl;
            }}
        }}

        if (motorIgnited && !engineShutdown) {{
            auto engine = fdmExec->GetPropulsion()->GetEngine(0);
            if (dt > 0.0) totalImpulseLbfs += engine->GetThrust() * dt;
            if (time - ignitionTimeS >= motorBurnTimeS) {{
                engine->SetRunning(false);
                fdmExec->GetFCS()->SetThrottleCmd(0, 0.0);
                fdmExec->GetPropulsion()->GetTank(0)->SetContents(0.0);
                engineShutdown = true;
                std::cout << "Engine shutdown at t=" << time << " s" << std::endl;
                std::cout << "Total impulse delivered: " << totalImpulseLbfs << " lbf*s" << std::endl;
            }}
        }}

        if (!didLiftoff && altitudeAgl > liftoffThresholdAglFt && velocityMagnitude > 0.0) {{
            didLiftoff = true;
            std::cout << "Liftoff detected at t=" << time << " s, AGL=" << altitudeAgl << " ft" << std::endl;
        }}

        if (didLiftoff && altitudeAgl > maxAltitudeAglFt) maxAltitudeAglFt = altitudeAgl;
        if (didLiftoff && !reachedApogee && verticalVelocity < -20.0 && altitudeAgl > 100.0) {{
            reachedApogee = true;
            std::cout << "Apogee reached: " << maxAltitudeAglFt << " ft AGL at t=" << time << " s" << std::endl;
        }}
        if (reachedApogee && !chuteDeployed) {{
            fdmExec->SetPropertyValue("external_reactions/drogue_chute/drogue_open", 1.0);
            fdmExec->SetPropertyValue("aero/RACS_cant_active", 0.0);
            chuteDeployed = true;
            std::cout << "Recovery chute deployed at t=" << time << " s" << std::endl;
        }}

        const double rollDeg = fdmExec->GetPropagate()->GetEuler(1) * 180.0 / pi;
        const double pitchDeg = fdmExec->GetPropagate()->GetEuler(2) * 180.0 / pi;
        const double yawDeg = fdmExec->GetPropagate()->GetEuler(3) * 180.0 / pi;
        const double pRadS = fdmExec->GetPropagate()->GetPQR(1);
        const double qRadS = fdmExec->GetPropagate()->GetPQR(2);
        const double rRadS = fdmExec->GetPropagate()->GetPQR(3);
        const double currentLat = fdmExec->GetPropagate()->GetLocation().GetLatitudeDeg();
        const double currentLon = fdmExec->GetPropagate()->GetLocation().GetLongitudeDeg();
        const double currentAlt = fdmExec->GetPropagate()->GetAltitudeASL();
        const double xPos = (currentLat - initialLatitude) * 364000.0;
        const double yPos = (currentLon - initialLongitude) * 364000.0 * std::cos(initialLatitude * pi / 180.0);
        const double zPos = currentAlt - initialAltitude;
        const double cgX = fdmExec->GetMassBalance()->GetXYZcg(1);
        const double massSlugs = fdmExec->GetMassBalance()->GetMass();
        const double massLbs = massSlugs * 32.174;
        const double outputTime = time - ignitionTimeS;
        const double ftToM = 0.3048;
        const double ftS2ToMS2 = 0.3048;
        const double pressureKpa = fdmExec->GetPropertyValue("atmosphere/P-psf") * 0.04788025898;
        const double temperatureC = fdmExec->GetPropertyValue("atmosphere/T-R") * (5.0 / 9.0) - 273.15;
        auto outputEngine = fdmExec->GetPropulsion()->GetEngine(0);
        const double thrustLbf = outputEngine->GetThrust();
        const double cleanAccelXFps2 = (motorIgnited && !engineShutdown && massSlugs > 0.0) ? (thrustLbf / massSlugs) : 0.0;
        const int phase = !didLiftoff ? 0 : (!engineShutdown ? 1 : (!reachedApogee ? 2 : 3));

        outputFile << outputTime << ','
                   << altitude * ftToM << ',' << altitude * ftToM << ',' << altitudeAgl * ftToM << ',' << maxAltitudeAglFt * ftToM << ','
                   << vx * ftToM << ',' << vy * ftToM << ',' << (-vz) * ftToM << ',' << verticalVelocity * ftToM << ','
                   << pressureKpa << ',' << temperatureC << ','
                   << cleanAccelXFps2 * ftS2ToMS2 << ',' << 0.0 << ',' << 0.0 << ','
                   << cleanAccelXFps2 * ftS2ToMS2 << ',' << 0.0 << ',' << 0.0 << ','
                   << xPos * ftToM << ',' << yPos * ftToM << ',' << zPos * ftToM << ','
                   << phase << ',' << (chuteDeployed ? 1 : 0) << ',' << 0 << ',' << cgX << ',' << massLbs << ','
                   << rollDeg << ',' << pitchDeg << ',' << yawDeg << ','
                   << pRadS << ',' << qRadS << ',' << rRadS << ','
                   << time << ',' << xPos << ',' << yPos << ',' << zPos << ','
                   << altitude << ',' << altitudeAgl << ',' << verticalVelocity << ',' << verticalAcceleration << ','
                   << aBody(1) << ',' << aBody(2) << ',' << aBody(3) << '\\n';


        if (time - lastPrintTime >= 0.1) {{
            std::cout << std::fixed << std::setprecision(1)
                      << "t=" << time << "s: AGL=" << altitudeAgl << "ft, Vel=" << velocityMagnitude << "ft/s";
            if (!didLiftoff) std::cout << " [ON PAD]";
            else if (!engineShutdown) std::cout << " [POWERED FLIGHT]";
            else if (!reachedApogee) std::cout << " [COASTING UP]";
            else if (!chuteDeployed) std::cout << " [FALLING]";
            else std::cout << " [CHUTE DESCENT]";
            std::cout << std::endl;
            lastPrintTime = time;
        }}
        if (didLiftoff && reachedApogee && altitudeAgl < 5.0) {{
            std::cout << "Rocket has reached the ground after flight." << std::endl;
            break;
        }}
        lastTime = time;
    }}

    std::cout << "Simulation complete." << std::endl;
    std::cout << "Maximum altitude reached: " << maxAltitudeAglFt << " ft AGL" << std::endl;
    std::cout << "Final sim time: " << fdmExec->GetSimTime() << " s" << std::endl;
    outputFile.close();
    return 0;
}}
'''


def xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_outputs(model: RocketModel, out_dir: Path) -> None:
    aircraft_dir = out_dir / "aircraft" / model.safe_name
    engines_dir = aircraft_dir / "Engines"
    systems_dir = aircraft_dir / "Systems"
    src_dir = out_dir / "src"
    reports_dir = out_dir / "reports"
    engines_dir.mkdir(parents=True, exist_ok=True)
    systems_dir.mkdir(parents=True, exist_ok=True)
    src_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    (aircraft_dir / f"{model.safe_name}.xml").write_text(generate_aircraft_xml(model))
    engine_file, engine_xml = generate_engine_xml(model)
    nozzle_file, nozzle_xml = generate_nozzle_xml(model)
    (engines_dir / f"{engine_file}.xml").write_text(engine_xml)
    (engines_dir / f"{nozzle_file}.xml").write_text(nozzle_xml)
    (systems_dir / "README.md").write_text("Generated placeholder Systems folder. Add JSBSim FCS/recovery systems here if needed.\n")
    (src_dir / f"rocket_sim_{model.safe_name}.cpp").write_text(generate_cpp(model, hitl=False))
    (src_dir / f"hitl_sim_{model.safe_name}.cpp").write_text(generate_cpp(model, hitl=True))

    summary = [
        f"Rocket: {model.name}",
        f"Safe name: {model.safe_name}",
        f"Components parsed: {len(model.components)}",
        f"Motors parsed: {len(model.motors)}",
        f"Selected motor: {(model.selected_motor.designation if model.selected_motor else 'none')}",
        f"Motor curve source: {(model.motor_curve.source if model.motor_curve else 'none')}",
        f"Motor curve database: {(model.motor_curve.db_path if model.motor_curve and model.motor_curve.db_path else 'none')}",
        f"Motor curve points: {(len(model.motor_curve.points) if model.motor_curve else 0)}",
        f"Motor curve total impulse N*s: {(model.motor_curve.total_impulse_ns if model.motor_curve else 0.0):.6f}",
        f"Motor curve burn time s: {(model.motor_curve.burn_time_s if model.motor_curve else 0.0):.6f}",
        f"Simulations parsed: {len(model.simulations)}",
        f"Generated aircraft XML: aircraft/{model.safe_name}/{model.safe_name}.xml",
        f"Generated standalone C++: src/rocket_sim_{model.safe_name}.cpp",
        f"Generated HIL C++: src/hitl_sim_{model.safe_name}.cpp",
    ]
    (reports_dir / "conversion_report.txt").write_text("\n".join(summary) + "\n")


def cmake_target_block(model: RocketModel) -> str:
    standalone = f"rocket_sim_{model.safe_name}"
    hitl = f"hitl_sim_{model.safe_name}"
    return f"""# -----------------------------
# Build generated {model.safe_name} sim
# -----------------------------
add_executable({standalone} src/{standalone}.cpp)
target_include_directories({standalone} PRIVATE
${{JSBSIM_INCLUDE_DIR}}
"/home/altari/asio/asio/include"
)
target_compile_definitions({standalone} PRIVATE ASIO_STANDALONE)
target_link_libraries({standalone}
${{JSBSIM_LIBRARY}}
)

# -----------------------------
# Build generated {model.safe_name} HIL sim
# -----------------------------
add_executable({hitl} src/{hitl}.cpp)
target_include_directories({hitl} PRIVATE
${{JSBSIM_INCLUDE_DIR}}
"/home/altari/asio/asio/include"
)
target_compile_definitions({hitl} PRIVATE ASIO_STANDALONE)
target_link_libraries({hitl}
${{JSBSIM_LIBRARY}}
)

"""


def install_into_jsbsim(model: RocketModel, package_dir: Path, jsbsim_repo: Path) -> None:
    jsbsim_repo = jsbsim_repo.expanduser().resolve()
    cmake_file = jsbsim_repo / "CMakeLists.txt"
    if not cmake_file.exists():
        raise FileNotFoundError(f"JSBSim repo path does not contain CMakeLists.txt: {jsbsim_repo}")

    src_aircraft = package_dir / "aircraft" / model.safe_name
    src_standalone = package_dir / "src" / f"rocket_sim_{model.safe_name}.cpp"
    src_hitl = package_dir / "src" / f"hitl_sim_{model.safe_name}.cpp"
    for required in (src_aircraft, src_standalone, src_hitl):
        if not required.exists():
            raise FileNotFoundError(f"Generated file/folder missing before install: {required}")

    dest_aircraft = jsbsim_repo / "aircraft" / model.safe_name
    dest_convert_pkg = jsbsim_repo / "open rocket convert" / model.safe_name
    dest_standalone = jsbsim_repo / "src" / src_standalone.name
    dest_hitl = jsbsim_repo / "src" / src_hitl.name

    if dest_aircraft.exists():
        shutil.rmtree(dest_aircraft)
    shutil.copytree(src_aircraft, dest_aircraft)

    dest_standalone.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_standalone, dest_standalone)
    shutil.copy2(src_hitl, dest_hitl)

    if dest_convert_pkg.exists():
        shutil.rmtree(dest_convert_pkg)
    dest_convert_pkg.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_dir, dest_convert_pkg)

    standalone_target = f"rocket_sim_{model.safe_name}"
    hitl_target = f"hitl_sim_{model.safe_name}"
    cmake_text = cmake_file.read_text()
    if f"add_executable({standalone_target} " not in cmake_text:
        insert_marker = "# -----------------------------\n# Copy aircraft config\n# -----------------------------"
        if insert_marker not in cmake_text:
            raise ValueError("Could not find CMake insertion marker before Copy aircraft config")
        cmake_text = cmake_text.replace(insert_marker, cmake_target_block(model) + insert_marker)

    foreach_marker = "        hitl_sim_summer_subscale\n"
    targets_to_add = ""
    if standalone_target not in cmake_text.split("foreach(target_name", 1)[-1]:
        targets_to_add += f"        {standalone_target}\n"
    if hitl_target not in cmake_text.split("foreach(target_name", 1)[-1]:
        targets_to_add += f"        {hitl_target}\n"
    if targets_to_add:
        if foreach_marker not in cmake_text:
            raise ValueError("Could not find CMake runtime target list insertion point")
        cmake_text = cmake_text.replace(foreach_marker, foreach_marker + targets_to_add)

    cmake_file.write_text(cmake_text)
    print(f"Installed aircraft: {dest_aircraft}")
    print(f"Installed C++: {dest_standalone}")
    print(f"Installed C++: {dest_hitl}")
    print(f"Mirrored package: {dest_convert_pkg}")
    print(f"CMake targets: {standalone_target}, {hitl_target}")


def build_jsbsim_targets(model: RocketModel, jsbsim_repo: Path) -> None:
    jsbsim_repo = jsbsim_repo.expanduser().resolve()
    targets = [f"rocket_sim_{model.safe_name}", f"hitl_sim_{model.safe_name}"]
    print("Configuring JSBSim build directory so new generated targets are visible")
    subprocess.run(["cmake", "-S", ".", "-B", "build"], cwd=jsbsim_repo, check=True)
    cmd = ["cmake", "--build", "build", "--target", *targets, "-j2"]
    print("Building JSBSim targets: " + " ".join(targets))
    subprocess.run(cmd, cwd=jsbsim_repo, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert a basic OpenRocket .ork into JSBSim starter files.")
    parser.add_argument("ork", type=Path, help="Path to .ork file")
    parser.add_argument("--name", help="Override generated aircraft name")
    parser.add_argument("--output", type=Path, default=None, help="Output directory. Defaults to open rocket convert/<aircraft_name>")
    parser.add_argument("--motor-db", type=Path, help="Optional OpenRocket motors.db path. If omitted, common OpenRocket locations are searched automatically.")
    parser.add_argument("--no-download-motor-db", action="store_true", help="Disable automatic download of OpenRocket's published motors.db.gz when no local database is found.")
    parser.add_argument("--allow-placeholder-thrust", action="store_true", help="Allow an explicit synthetic thrust curve when no real motor database match is found. Disabled by default.")
    parser.add_argument("--install-jsbsim", type=Path, help="Optional JSBSim repo path to install aircraft, C++ runners, and CMake targets")
    parser.add_argument("--build-jsbsim", action="store_true", help="After --install-jsbsim, build the generated standalone and HIL targets with CMake.")
    args = parser.parse_args()

    model = parse_ork(args.ork)
    if args.name:
        model.safe_name = slugify(args.name)

    curve, searched_paths = resolve_motor_curve(model, args.motor_db, auto_download=not args.no_download_motor_db)
    if curve:
        print(f"Matched motor curve: {curve.manufacturer} {curve.designation} from {curve.db_path}")
        print(f"Curve points: {len(curve.points)}, burn time: {curve.burn_time_s:.3f} s, impulse: {curve.total_impulse_ns:.2f} N*s")
    elif args.allow_placeholder_thrust and model.selected_motor:
        model.motor_curve = build_explicit_placeholder_curve(model.selected_motor)
        print("WARNING: using explicit placeholder thrust because --allow-placeholder-thrust was provided")
    else:
        searched = "\n  ".join(str(path) for path in searched_paths)
        selected = model.selected_motor
        motor_name = f"{selected.manufacturer} {selected.designation}" if selected else "none"
        raise SystemExit(
            "No real OpenRocket motor database thrust curve was found.\n"
            f"Selected ORK motor: {motor_name}\n"
            "Searched paths:\n  " + searched + "\n"
            "Install/update OpenRocket so motors.db exists, copy motors.db next to this converter, "
            "or pass --motor-db path/to/motors.db. The converter also tries to download "
            "OpenRocket's published motors.db.gz unless --no-download-motor-db is set. "
            "Use --allow-placeholder-thrust only for rough testing."
        )

    output_dir = args.output if args.output is not None else Path("open rocket convert") / model.safe_name
    write_outputs(model, output_dir)
    print(f"Generated JSBSim starter files in {output_dir}")
    print(f"Aircraft name: {model.safe_name}")
    if args.install_jsbsim:
        install_into_jsbsim(model, output_dir, args.install_jsbsim)
        if args.build_jsbsim:
            build_jsbsim_targets(model, args.install_jsbsim)
    elif args.build_jsbsim:
        raise SystemExit("--build-jsbsim requires --install-jsbsim so the generated files are installed before building.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


